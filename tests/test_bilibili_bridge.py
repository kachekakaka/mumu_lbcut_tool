"""本机桥接集成，用合成 Core 控制丢包点与会话结果。"""

import asyncio
import sqlite3
import threading
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from src.orchestrator import bilibili_api as api
from src.orchestrator.account_login_store import LoginStore
from src.orchestrator.upload_core_client import BridgeError, session_view
from src.orchestrator.upload_core_runtime import boot_id, state


def synthetic(sid, outcome=None):
    return {
        "session_id": sid,
        "operation_id": sid,
        "account_id": None,
        "revision": 2 if outcome else 1,
        "phase": "finished" if outcome else "waiting_confirmation",
        "outcome": outcome,
        "created_at": "2026-11-18T00:00:00Z",
        "updated_at": "2026-11-18T00:00:02Z",
        "can_cancel": not outcome,
        "can_retry": False,
        "can_resume": False,
        "challenge": None,
        "account_summary": None,
        "error": None,
    }


class Fake:
    def __init__(self):
        self.sid = str(uuid4())
        self.available = True
        self.keys = []
        self.outcome = None
        self.drop = False
        self.reject = False

    async def call(self, port, path, *, body=None, key=None, **kwargs):
        if path.endswith("/health"):
            return {"api": "ready", "database": "ready"}
        if path.endswith("/capabilities"):
            return {
                "gui_login": {
                    "version": 1,
                    "available": self.available,
                    "platforms": ["bilibili"],
                }
            }
        if path.endswith("view=summary"):
            return {"items": []}
        if "active=true" in path:
            return {"items": [] if self.outcome else [synthetic(self.sid)]}
        if body is not None:
            self.keys.append(key)
            if self.reject:
                raise BridgeError("engine_interaction_incompatible", "能力变化")
            if self.drop:
                self.drop = False
                raise BridgeError("core_unreachable", "响应丢失", 503)
        return synthetic(self.sid, self.outcome)


@pytest.fixture
def service(monkeypatch, tmp_path):
    fake = Fake()
    monkeypatch.setattr(api, "call", fake.call)
    monkeypatch.setattr(
        api.CoreRuntime,
        "inspect",
        lambda self, ensure=False: {
            "state": "online",
            "port": 8765,
            "deployment": "test",
        },
    )
    monkeypatch.setattr(api.CoreRuntime, "profile", lambda self: {"id": "test"})
    app = FastAPI()
    api.install_accounts(
        app, SimpleNamespace(core_profile=None, data_dir=tmp_path, port=8000)
    )
    return app, fake


def client(app, peer="127.0.0.1"):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(peer, 51000)),
        base_url="http://127.0.0.1:8000" + api.BASE + "/",
    )


async def connect(c):
    c.headers["x-mumu-page"] = (await c.get("bootstrap")).json()["token"]


def test_unknown_replay_and_account_lock(service):
    app, fake = service

    async def run():
        async with client(app) as c:
            await connect(c)
            key = str(uuid4())
            body = {"action": "create", "target": None, "deployment": "test"}

            async def send(k=key):
                return await c.post(
                    "operations", json=body, headers={"Idempotency-Key": k}
                )

            fake.drop = True
            assert (await send()).status_code == 503
            assert (await send(str(uuid4()))).json()["key"] == key
            fake.reject = True
            assert (await send()).status_code == 409
            assert (await c.get("operations")).json()["items"][0]["status"] == "unknown"
            fake.reject = False
            first = (await send()).json()
            assert first["session"] == fake.sid and first["status"] == "linked"
            fake.available = False
            assert (await c.get("accounts")).status_code == 200
            assert (await c.get("sessions/" + fake.sid)).json()["can_cancel"]
            assert (await send(str(uuid4()))).json()["session"] == fake.sid
            fake.outcome = "succeeded"
            assert (await c.get("sessions/" + fake.sid)).json()[
                "outcome"
            ] == "succeeded"
            assert (await c.get("operations")).json()["items"][0]["status"] == "done"
            assert (await send(str(uuid4()))).status_code == 409
            assert (await c.get("operations")).json()["items"][0][
                "status"
            ] == "rejected"
            assert fake.keys == [key, key, key]

    asyncio.run(run())


@pytest.mark.parametrize(
    "header",
    [
        {"Host": "example.test"},
        {"X-Forwarded-For": "127.0.0.1"},
        {"Origin": "http://other.test"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_bootstrap_rejects_untrusted_sources(service, header):
    async def run():
        async with client(service[0]) as c:
            assert (await c.get("bootstrap", headers=header)).status_code == 403
            assert (await c.get("operations")).status_code == 401
            await connect(c)
            assert (await c.get("operations")).headers["Cache-Control"] == "no-store"
        async with client(service[0], "192.168.1.7") as c:
            assert (await c.get("bootstrap")).status_code == 403

    asyncio.run(run())


def test_challenge_must_match_exact_session():
    sid = str(uuid4())
    v = synthetic(sid)
    v["challenge"] = {
        "challenge_id": "abc",
        "deadline_at": "2026-11-18T00:03:00Z",
        "image_path": "http://other/qr",
    }
    with pytest.raises(BridgeError):
        session_view(v)
    v["challenge"]["image_path"] = f"{api.PREFIX}/{sid}/qr?challenge_id=abc"
    assert "image_path" not in session_view(v)["challenge"]


def test_journal_reopens_and_different_actions_do_not_alias(tmp_path):
    path = tmp_path / "bridge.db"
    a = LoginStore(path)
    key = str(uuid4())
    a.begin("d", key, "create", None)
    a.finish(key, "unknown")
    b = LoginStore(path)
    assert b.begin("d", str(uuid4()), "create", None)[0]["key"] == key
    with pytest.raises(BridgeError):
        b.begin("d", key, "resume", "other")
    assert b.begin("d", key, "create", None)[1] is True
    b.finish(key, "rejected")
    assert not b.pending("d")


def test_crash_window_requires_proof(monkeypatch):
    import src.orchestrator.upload_core_runtime as rt

    monkeypatch.setattr(rt, "owners", lambda port: set())
    row = {
        "boot": boot_id(),
        "port": 9999,
        "stage": "creating",
        "host_pid": None,
        "host_time": None,
        "child_pid": None,
        "child_time": None,
        "listener": None,
        "listener_time": None,
    }
    assert state(row) == "check"
    assert state(row | {"stage": "intent"}) == "stopped"
    assert state(row | {"boot": "earlier"}) == "stopped"
    monkeypatch.setattr(rt, "owners", lambda port: {1234})
    assert state(row | {"boot": "earlier"}) == "occupied"


def test_request_survives_sender_process_exit(tmp_path):
    """发送者硬退出后，新进程只取回原编号，其他标签页仍不能占第二个位置。"""
    import subprocess
    import sys

    path = tmp_path / "crash.sqlite3"
    key = str(uuid4())
    source = (
        "import os,sys; from pathlib import Path; "
        "from src.orchestrator.account_login_store import LoginStore; "
        "LoginStore(Path(sys.argv[1])).begin('deployment',sys.argv[2],'create',None); "
        "os._exit(29)"
    )
    result = subprocess.run(
        [sys.executable, "-c", source, str(path), key], timeout=10, check=False
    )
    assert result.returncode == 29
    reopened = LoginStore(path)
    assert reopened.begin("deployment", str(uuid4()), "create", None)[0]["key"] == key
    assert reopened.begin("deployment", key, "create", None) == ({"key": key}, True)


@pytest.mark.parametrize("pause", ["before_begin", "after_begin", "after_remote", "confirmed"])
def test_cancel_http_keeps_single_owner_until_login_log_has_settled(service, monkeypatch, pause):
    app, fake = service
    entered, resume = threading.Event(), threading.Event()
    finishing, finish_gate = threading.Event(), threading.Event()
    first = [True]
    begin = LoginStore.begin
    finish = LoginStore.finish
    original_call = fake.call
    sends = set()

    def delayed_begin(store, *args):
        gate_this_time, first[0] = first[0], False
        if gate_this_time and pause == "before_begin":
            entered.set()
            assert resume.wait(4)
        result = begin(store, *args)
        if gate_this_time and pause == "after_begin":
            entered.set()
            assert resume.wait(4)
        return result

    def delayed_finish(store, *args, **kwargs):
        if args[1] in ("unknown", "not_sent") or pause == "confirmed" and args[1] == "linked":
            finishing.set()
            assert finish_gate.wait(4)
        return finish(store, *args, **kwargs)

    async def delayed_call(port, path, *, body=None, key=None, **kwargs):
        if body is not None:
            new_key = key not in sends
            sends.add(key)
            if new_key and pause == "after_remote":
                entered.set()
                await asyncio.Event().wait()
        return await original_call(port, path, body=body, key=key, **kwargs)

    monkeypatch.setattr(LoginStore, "begin", delayed_begin)
    monkeypatch.setattr(LoginStore, "finish", delayed_finish)
    monkeypatch.setattr(api, "call", delayed_call)

    async def scenario():
        async with client(app) as c:
            await connect(c)
            key = str(uuid4())
            def send():
                return c.post("operations", json={"action": "create", "target": None, "deployment": "test"},
                              headers={"Idempotency-Key": key})
            try:
                request = asyncio.create_task(send())
                assert await asyncio.to_thread((finishing if pause == "confirmed" else entered).wait, 3)
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
                resume.set()
                assert await asyncio.to_thread(finishing.wait, 3)
                repeated = asyncio.create_task(send())
                await asyncio.sleep(.03)
                assert not repeated.done()
                assert len(sends) == int(pause in ("after_remote", "confirmed"))
                finish_gate.set()
                result = (await asyncio.wait_for(repeated, 4)).json()
                assert result == {"key": key, "status": "linked", "session": fake.sid} or result == {
                    "key": key, "status": "linked", "session": fake.sid, "error": None,
                }
                entries = (await c.get("operations")).json()["items"]
                assert len(entries) == 1 and entries[0]["status"] == "linked" and sends == {key}
                assert fake.keys == [key]
            finally:
                resume.set()
                finish_gate.set()
                await app.state.finish_login_actions()

    asyncio.run(scenario())


def test_login_write_lock_does_not_block_asgi_or_timer(service, monkeypatch, tmp_path):
    app, fake = service
    started = threading.Event()
    begin = LoginStore.begin
    def track(store, *args):
        started.set()
        return begin(store, *args)
    monkeypatch.setattr(LoginStore, 'begin', track)

    @app.get('/test-light')
    async def light():
        return {'ready': True}

    async def scenario():
        async with client(app) as c:
            await connect(c)
            assert (await c.get('operations')).status_code == 200
            path = tmp_path / 'upload-core' / 'account-login-bridge.sqlite3'
            key = str(uuid4())
            lock = sqlite3.connect(path)
            lock.execute('BEGIN IMMEDIATE')
            try:
                request = asyncio.create_task(c.post('operations',
                    json={'action': 'create', 'target': None, 'deployment': 'test'}, headers={'Idempotency-Key': key}))
                assert await asyncio.to_thread(started.wait, 2)
                timer = asyncio.create_task(asyncio.sleep(.025))
                response = await c.get('http://127.0.0.1:8000/test-light')
                await timer
                assert response.json() == {'ready': True} and not request.done() and not fake.keys
                await asyncio.sleep(.32)
            finally:
                lock.rollback()
                lock.close()
            assert (await request).json()['session'] == fake.sid
            assert fake.keys == [key]
    asyncio.run(scenario())

"""切片投递的本地事务、跨代次写回及只读恢复。"""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
import httpx
import pytest

from src.orchestrator.bilibili_api import install_accounts
from src.orchestrator.segment_upload_store import ArrangementStore
from src.orchestrator.segment_uploads import SegmentUploads
from src.orchestrator.upload_core_client import Account, BridgeError


PARAMETERS = dict(title="切片测试", description="", tags=["测试"], category_id=171,
                  copyright=1, source="", publish_mode="self_only")
ACCOUNT = Account(id="account", display_name="测试账号", platform="bilibili", reference="bilibili:42",
                  remote_user_id="42", enabled=True, availability="ready", block_reasons=[])


class Core:
    def __init__(self):
        self.deployment = "A"
        self.trace = []
        self.tasks = {}
        self.register_action = None
        self.submit_action = None
        self.extra_pages = []

    async def binding(self, deployment=None):
        if deployment and deployment != self.deployment:
            raise BridgeError("binding", "部署已变")
        return {"deployment": self.deployment, "port": 8765}

    async def verify(self, binding):
        await self.binding(binding["deployment"])

    async def capabilities(self, binding):
        return ["self_only"], dict(title=80, part_title=80, description=500, tag=20, tag_count=10)

    async def health(self, binding):
        return False

    async def accounts(self, binding):
        return [ACCOUNT]

    async def register(self, binding, body, key):
        self.trace.append(("POST register", key, deepcopy(body)))
        if self.register_action:
            await self.register_action()
        return {"work_id": body["manifest"]["work"]["id"]}

    async def submit(self, binding, work, body, key):
        self.trace.append(("POST submit", key, deepcopy(body)))
        video = body["manifest"]["assets"]["videos"][0]["path"]
        if work not in self.tasks:
            self.tasks[work] = {
                "work_id": work, "id": str(uuid4()), "account_id": ACCOUNT.id, "version": 1,
                "status": "QUEUED", "snapshot": {
                    "mode": "independent", "unit_id": "segment", "item_key": "video",
                    "directory": body["directory"],
                    "target": {"account_id": ACCOUNT.id, "expected_mid": "42", "platform": "bilibili"},
                    "parts": [{"video_id": "video"}],
                    "assets": [{"asset_id": "video", "role": "video", "video_id": "video", "relative_path": video}],
                },
            }
        if self.submit_action:
            await self.submit_action()
        current = self.tasks[work]
        return {"work_id": work, "tasks": [
            {"task_id": current["id"], "created": False, "status": "QUEUED", "snapshot": current["snapshot"]}
        ]}

    async def work_tasks(self, binding, work, cursor):
        self.trace.append(("GET work tasks", work, cursor))
        if self.extra_pages and not cursor:
            return {"items": self.extra_pages, "next_cursor": self.extra_pages[-1]["id"]}
        task = self.tasks.get(work)
        return {"items": [task] if task else [], "next_cursor": None}

    async def task(self, binding, task_id):
        self.trace.append(("GET task", task_id))
        return next(t for t in self.tasks.values() if t["id"] == task_id)


@pytest.fixture
def setup(tmp_path):
    core = Core()
    output = tmp_path / "output"
    output.mkdir()
    media = output / "000001.mp4"
    media.write_bytes(b"test video")
    source = dict(status="finished", started_at=1, file_path=str(media), quality_flags=[],
                  nickname="测试", target_raw="room", duration_sec=2)
    now = [100.]

    def new_mumu():
        store = ArrangementStore(tmp_path / "arrangements.sqlite3", clock=lambda: now[0])
        return SegmentUploads(store, core, lambda job, index: source, output)

    return new_mumu, core, media, source, now


async def confirm(mumu, ctx, *, values=PARAMETERS, key=None, quality=False):
    return await mumu.confirm(key or str(uuid4()), ctx["ticket"], ACCOUNT.id, values, quality)


def test_old_dialog_cannot_place_order_on_other_deployment(setup):
    new_mumu, core, _, _, _ = setup

    async def scenario():
        mumu = new_mumu()
        dialog = await mumu.context("job", 1)
        core.deployment = "B"
        with pytest.raises(BridgeError, match="部署已变"):
            await confirm(mumu, dialog)
        assert not core.trace and not mumu.store.for_segments([("job", 1)])

    asyncio.run(scenario())


def test_concurrent_confirm_conflict_and_restart_preserve_one_order(setup):
    new_mumu, core, _, _, _ = setup

    async def scenario():
        mumu = new_mumu()
        first, second = await mumu.context("job", 1), await mumu.context("job", 1)
        registered, release = asyncio.Event(), asyncio.Event()

        async def in_flight():
            registered.set()
            await release.wait()

        core.register_action = in_flight
        key = str(uuid4())
        pending = asyncio.create_task(confirm(mumu, first, key=key))
        await registered.wait()
        duplicate = await confirm(mumu, second)
        assert duplicate["task_id"] is None
        for candidate in (key, str(uuid4())):
            with pytest.raises(BridgeError) as exc:
                await confirm(mumu, first, key=candidate, values=PARAMETERS | {"title": "其他"})
            assert exc.value.code in ("confirmation_conflict", "arrangement_conflict")
        release.set()
        original = await pending
        row = await new_mumu().refresh(original["id"], force=True)
        assert row["id"] == duplicate["id"] == original["id"]
        assert row["task_id"] == original["task_id"] == core.tasks[original["work_id"]]["id"]
        assert [r[0] for r in core.trace].count("POST register") == 1
        assert [r[0] for r in core.trace].count("POST submit") == 1

    asyncio.run(scenario())


def test_submit_commit_lost_after_video_moved_checks_every_page_without_post(setup):
    new_mumu, core, media, _, _ = setup

    async def scenario():
        async def drop():
            raise BridgeError("core_unreachable", "超时", 503)

        core.submit_action = drop
        mumu = new_mumu()
        original = await confirm(mumu, await mumu.context("job", 1))
        assert original["task_id"] is None
        work = original["work_id"]
        actual = core.tasks[work]
        actual.update(status="SUCCEEDED", version=2)
        other = deepcopy(actual)
        other["id"] = "00000000-0000-0000-0000-000000000001"
        other["snapshot"]["item_key"] = "another-video"
        core.extra_pages = [other]
        media.rename(media.with_name("moved.mp4"))
        original_writes = list(core.trace)
        reopened = new_mumu()
        recovered = await reopened.drive(original["id"], original["revision"])
        assert recovered["task_id"] == actual["id"] and recovered["status"] == "SUCCEEDED"
        assert recovered["work_id"] == work and len(core.tasks) == 1
        assert core.trace[len(original_writes):] == [
            ("GET work tasks", work, None), ("GET work tasks", work, other["id"]), ("GET task", actual["id"]),
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["timeout", "cancel", "expired"])
def test_same_process_can_continue_original_and_late_generation_cannot_write(setup, fault):
    new_mumu, core, _, _, now = setup

    async def scenario():
        mumu = new_mumu()
        first_dispatch, late = asyncio.Event(), asyncio.Event()

        async def delay():
            first_dispatch.set()
            if fault == "timeout":
                raise TimeoutError()
            await late.wait()
            return None

        core.register_action = delay
        initial = asyncio.create_task(confirm(mumu, await mumu.context("job", 1)))
        await first_dispatch.wait()
        original = mumu.store.for_segments([("job", 1)])[0]
        if fault == "cancel":
            initial.cancel()
            with pytest.raises(asyncio.CancelledError):
                await initial
        elif fault == "timeout":
            assert (await initial)["problem"] == "core_timeout"
        else:
            now[0] += 19
        core.register_action = None
        linked = await mumu.drive(original["id"], original["revision"])
        assert linked["task_id"] and linked["problem"] is None
        if fault == "expired":
            late.set()
            assert (await initial)["task_id"] == linked["task_id"]
        assert core.trace[0] == core.trace[1]  # 同一个登记 Key 和正文。
        current = mumu.store.get(original["id"])
        assert current["generation"] > original["generation"]
        assert current["task_id"] == linked["task_id"] and current["problem"] is None

    asyncio.run(scenario())


def test_first_definite_rejection_allows_revision_and_stales_old_context(setup):
    new_mumu, core, _, _, _ = setup

    async def scenario():
        mumu = new_mumu()
        old, duplicate = await mumu.context("job", 1), await mumu.context("job", 1)

        async def reject():
            raise BridgeError("invalid_path", "首次参数被拒绝", 409)

        core.register_action = reject
        key = str(uuid4())
        initial = await confirm(mumu, old, key=key)
        assert initial["rejected"] and initial["can_revise"]
        revise = await mumu.context("job", 1, initial["id"])
        core.register_action = None
        updated = await confirm(mumu, revise, values=PARAMETERS | {"title": "新标题"})
        assert updated["id"] == initial["id"] and updated["work_id"] == initial["work_id"]
        assert updated["revision"] == 2 and updated["task_id"]
        for ctx in (old, duplicate):
            with pytest.raises(BridgeError) as exc:
                await confirm(mumu, ctx, key=key)
            assert exc.value.code == "stale_dialog"
        assert core.trace[0][1] != core.trace[1][1]
        with mumu.store.connect() as db:
            history = db.execute("SELECT * FROM revisions WHERE arrangement_id=?", (initial["id"],)).fetchone()
        assert json.loads(history["frozen"])["parameters"]["title"] == PARAMETERS["title"]

    asyncio.run(scenario())


def test_replay_rejection_stays_uncertain_and_replayed_receipt_gets_current_status(setup):
    new_mumu, core, _, _, _ = setup

    async def scenario():
        async def drop():
            raise TimeoutError

        async def reject():
            raise BridgeError("invalid_path", "后来拒绝", 409)

        mumu = new_mumu()
        core.submit_action = drop
        initial = await confirm(mumu, await mumu.context("job", 1))
        task = core.tasks[initial["work_id"]]
        task.update(status="SUCCEEDED", version=3)
        # 模拟 Core 的只读页还未见到原任务，下一次重放先拒绝，再拿到原始回执。
        async def empty(binding, work, cursor):
            core.trace.append(("GET work tasks", work, cursor))
            return {"items": [], "next_cursor": None}

        core.work_tasks = empty
        core.submit_action = reject
        replay = await mumu.drive(initial["id"], initial["revision"])
        assert replay["problem"] == "invalid_path" and not replay["can_revise"] and not replay["rejected"]
        with pytest.raises(BridgeError) as exc:
            await mumu.context("job", 1, initial["id"])
        assert exc.value.code == "revision_forbidden"
        core.submit_action = None
        recovered = await mumu.drive(initial["id"], initial["revision"])
        assert recovered["task_id"] == task["id"] and recovered["status"] == "SUCCEEDED"
        writes = [r for r in core.trace if r[0] == "POST submit"]
        assert len(writes) == 3 and writes[0] == writes[1] == writes[2]
        assert core.trace[-1] == ("GET task", task["id"])

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["unfinished", "aborted", "outside", "missing", "quality_changed"])
def test_preflight_blocks_unsuitable_media_and_changed_quality(setup, tmp_path, failure):
    new_mumu, core, media, source, _ = setup

    async def scenario():
        mumu = new_mumu()
        if failure == "quality_changed":
            dialog = await mumu.context("job", 1)
            source["quality_flags"] = [{"flag": "low_frame_rate", "suspected_start_time": 1}]
            with pytest.raises(BridgeError) as exc:
                await confirm(mumu, dialog, quality=True)
            assert exc.value.code == "segment_changed"
        else:
            if failure == "unfinished":
                source["status"] = "started"
            elif failure == "aborted":
                source["status"] = "aborted"
            elif failure == "outside":
                destination = tmp_path / media.name
                media.rename(destination)
                source["file_path"] = str(destination)
            else:
                media.rename(media.with_name("missing.mp4"))
            with pytest.raises(BridgeError) as exc:
                await mumu.context("job", 1)
            assert exc.value.code == ("segment_unfinished" if failure in ("unfinished", "aborted")
                                      else "segment_file_unavailable")
        assert not core.trace

    asyncio.run(scenario())


@pytest.mark.parametrize("second_page", ["ambiguous", "failed", "conflict"])
def test_lookup_never_associates_partial_or_ambiguous_pagination(setup, second_page):
    new_mumu, core, _, _, _ = setup

    async def scenario():
        async def drop():
            raise TimeoutError

        core.submit_action = drop
        mumu = new_mumu()
        initial = await confirm(mumu, await mumu.context("job", 1))
        work = initial["work_id"]
        task = core.tasks[work]
        first = deepcopy(task)
        first["id"] = "00000000-0000-0000-0000-000000000001"
        if second_page == "conflict":
            first["snapshot"]["target"]["expected_mid"] = "99"

        async def pages(binding, work_id, cursor):
            core.trace.append(("GET work tasks", work_id, cursor))
            if not cursor:
                return {"items": [first], "next_cursor": first["id"]}
            if second_page == "failed":
                raise BridgeError("core_unreachable", "第 2 页超时", 503)
            return {"items": [task], "next_cursor": None}

        core.work_tasks = pages
        since = len(core.trace)
        result = await mumu.drive(initial["id"], initial["revision"])
        assert result["task_id"] is None and not result["can_revise"]
        assert result["problem"] == {
            "ambiguous": "task_ambiguous", "failed": "core_unreachable", "conflict": "task_mismatch",
        }[second_page]
        assert core.trace[since:] == [("GET work tasks", work, None), ("GET work tasks", work, first["id"])]

    asyncio.run(scenario())


def test_late_read_does_not_replace_latest_observation(setup):
    new_mumu, core, _, _, now = setup

    async def scenario():
        mumu = new_mumu()
        submitted = await confirm(mumu, await mumu.context("job", 1))
        early, release = asyncio.Event(), asyncio.Event()
        actual = core.tasks[submitted["work_id"]]

        async def delayed_get(binding, task_id):
            old = deepcopy(actual)
            early.set()
            await release.wait()
            return old

        normal = core.task
        core.task = delayed_get
        slow = asyncio.create_task(mumu.refresh(submitted["id"], force=True))
        await early.wait()
        now[0] += 19
        actual.update(status="SUCCEEDED", version=2)
        core.task = normal
        latest = await mumu.refresh(submitted["id"], force=True)
        release.set()
        assert (await slow)["observed"] == latest["observed"]
        assert json.loads(latest["observed"])["status"] == "SUCCEEDED"

    asyncio.run(scenario())


def test_linked_uncertain_task_keeps_id_and_status_if_core_no_longer_finds_it(setup):
    new_mumu, core, _, _, _ = setup

    async def scenario():
        mumu = new_mumu()
        original = await confirm(mumu, await mumu.context("job", 1))
        task = core.tasks[original["work_id"]]
        task.update(status="UNCERTAIN", version=2)
        uncertain = await mumu.drive(original["id"], original["revision"])
        assert uncertain["status"] == "UNCERTAIN" and not uncertain["can_continue"]

        async def missing(binding, task_id):
            core.trace.append(("GET task", task_id))
            raise BridgeError("not_found", "原任务找不到", 404)

        core.task = missing
        since = len(core.trace)
        queried = await mumu.drive(original["id"], original["revision"])
        assert queried["task_id"] == uncertain["task_id"] and queried["status"] == "UNCERTAIN"
        assert queried["problem"] == "not_found" and queried["observed_at"] == uncertain["observed_at"]
        assert not queried["can_revise"]
        assert core.trace[since:] == [("GET task", original["task_id"])]

    asyncio.run(scenario())


def test_upload_endpoints_share_local_only_and_page_credentials(tmp_path):
    app = FastAPI()
    install_accounts(app, SimpleNamespace(data_dir=tmp_path, core_profile=None, port=8000, output_dir=tmp_path),
                     SimpleNamespace(state="running"))
    root = "/api/v1/bilibili"
    params = {"keys": "a" * 32 + ":1"}

    async def scenario():
        for peer in ("192.168.0.10", "127.0.0.1"):
            transport = httpx.ASGITransport(app=app, client=(peer, 50000))
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as c:
                summary = await c.get(root + "/segment-uploads", params=params)
                assert summary.status_code == (401 if peer.startswith("127") else 403)
                if summary.status_code == 401:
                    token = (await c.get(root + "/bootstrap")).json()["token"]
                    headers = {"X-Mumu-Page": token}
                    assert (await c.get(root + "/segment-uploads", params=params, headers=headers)).json() == {"items": []}
                    assert (await c.post(root + "/segment-uploads", json={}, headers=headers)).status_code == 400
                    assert (await c.get(root + "/segment-uploads", params=params,
                                        headers=headers | {"Origin": "http://other"})).status_code == 403

    asyncio.run(scenario())

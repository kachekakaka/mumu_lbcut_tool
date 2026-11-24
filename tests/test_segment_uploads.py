"""本机实际 HTTP 入口、YAML 与平台回执恢复。"""

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest

from src.orchestrator.server import ServerConfig, create_app
from src.orchestrator.storage import Store
from src.orchestrator.upload_manifest import manifest_path, version
from src.orchestrator.upload_platform import BridgeError
from src.orchestrator import recording_archive
from test_session_deliveries import FIELDS, STARTED, add_job, binding_settings, capture, finish_clip, room, start_clip
from upload_platform_fixture import DEPLOYMENT, Platform


ORIGIN = "http://127.0.0.1:8000"
UPLOADS = "/api/uploads"
BASE = UPLOADS + "/segment-uploads"


@pytest.fixture
def host(tmp_path):
    data = tmp_path / "runtime"
    root = tmp_path / "videos"
    store = Store(data / "orchestrator.sqlite3")
    target = store.add_target("live-a")
    job = add_job(store, target, room("测试"), STARTED - 1)
    clip = capture(store, job, root) / "part_000001.mp4"
    start_clip(store, job, 1, clip, STARTED)
    finish_clip(store, job, 1, clip, STARTED + 2)
    recording_archive.publish(store)
    clip = Path(recording_archive.location(store, job, 1)["file_path"])
    binding_settings(data)
    platform = Platform()

    def app():
        runtime = SimpleNamespace(store=store, state="running", start=lambda: None, close=lambda: None)
        return create_app(ServerConfig(data, root), runtime,
                          platform=platform.client)

    def client():
        return TestClient(app(), base_url=ORIGIN, client=("127.0.0.1", 50000))

    yield client, platform, store, job, clip
    store.close()


def preview(client, platform, job, fields=FIELDS, **extra):
    response = client.post(f"{BASE}/preview/{job}/1", json={
        "deployment": platform.deployment, "account_id": platform.account["id"],
        "remote_user_id": platform.account["remote_user_id"], "parameters": fields, **extra,
    })
    return response


def confirm(client, stage):
    return client.post(BASE + "/confirm/" + stage["ticket"], json={"quality_confirmed": True})


def test_preview_only_writes_yaml_and_repeat_confirmation_links_one_task(host):
    client, platform, store, job, clip = host
    with client() as browser:
        data = browser.get(f"{BASE}/context/{job}/1").json()
        assert data["filename"] == clip.name
        first = preview(browser, platform, job).json()
        second = preview(browser, platform, job, FIELDS | {"description": "同一安排重新预览"}).json()
        assert first["work"]["id"] == second["work"]["id"]
        assert not platform.tasks
        assert store.rows("SELECT * FROM delivery_segments") == []
        assert browser.get(second["media_url"], headers={"Range": "bytes=0-3"}).content == clip.read_bytes()[:4]
        assert confirm(browser, first).json()["code"] == "version_conflict"
        accepted = confirm(browser, second).json()
        again = confirm(browser, second).json()
        assert accepted["task_id"] == again["task_id"]
        assert len(platform.tasks) == 1
        assert accepted["work_id"] == second["work"]["id"]
        path = platform.trace[1][2]
        assert Path(path["manifest_path"]).is_file()
        saved = store.row("SELECT * FROM deliveries")
        assert Path(saved["manifest_path"]) == clip.parent / "投稿/001/upload.yaml"
        assert platform.trace[0][2]["asset_root"] == str(clip.parent)
        assert saved["config_version"] == version(Path(saved["manifest_path"]).read_bytes())
        result = browser.get(UPLOADS + "/panel", params={"page": "tasks", "task_id": accepted["task_id"]})
        assert result.status_code == 200
    with client() as restarted:
        records = restarted.get(BASE, params={"keys": job + ":1"}).json()["items"]
        assert [item["task_id"] for item in records] == [accepted["task_id"]]
        assert restarted.get(UPLOADS + "/tasks/" + accepted["task_id"]).json()["observed"] == "QUEUED"
    assert len(platform.tasks) == 1


def test_loopback_gate_and_offline_panel(host):
    client, platform, _, job, _ = host
    with client() as browser:
        path = f"{BASE}/context/{job}/1"
        for headers in ({"Host": "localhost:8000"}, {"Host": "127.0.0.1:8001"}, {"Origin": "http://example.test"},
                        {"X-Forwarded-For": "127.0.0.1"}, {"Sec-Fetch-Site": "cross-site"}):
            assert browser.get(path, headers=headers).status_code == 403
        assert browser.post(f"{BASE}/preview/{job}/1", data={"category": "171"}).status_code == 415
        platform.available = False
        assert browser.get(UPLOADS + "/panel?page=accounts").json()["code"] == "platform_offline"


@pytest.mark.parametrize("change,code", [
    ("mid", "invalid_account"), ("deployment", "binding"), ("file", "segment_changed"),
])
def test_confirmation_needs_original_deployment_mid_and_file(host, change, code):
    client, platform, _, job, clip = host
    with client() as browser:
        stage = preview(browser, platform, job).json()
        if change == "mid":
            platform.account["remote_user_id"] = "123"
        elif change == "deployment":
            platform.deployment = "B"
        else:
            clip.write_bytes(b"changed")
        assert confirm(browser, stage).json()["code"] == code
        assert not platform.tasks


@pytest.mark.parametrize("category,private,code", [(999, True, "invalid_metadata"), (171, False, "unsupported_option")])
def test_platform_previews_reject_invalid_options(host, category, private, code):
    client, platform, _, job, _ = host
    platform.private = private
    with client() as browser:
        assert preview(browser, platform, job, FIELDS | {"category_id": category}).json()["code"] == code
    assert not platform.tasks


@pytest.mark.parametrize("change", ["duplicate", "unknown", "asset_escape"])
def test_tampered_yaml_invalidates_preview_without_rewriting_file(host, change):
    client, platform, store, job, _ = host
    with client() as browser:
        stage = preview(browser, platform, job).json()
        path = Path(platform.trace[0][2]["manifest_path"])
        content = path.read_bytes()
        if change == "duplicate":
            modified = content + b"version: 1\n"
        elif change == "unknown":
            modified = content + b"unknown_option: true\n"
        else:
            relative = b"001.mp4"
            assert relative in content
            modified = content.replace(relative, b"../outside.mp4")
        path.write_bytes(modified)
        assert confirm(browser, stage).json()["code"] == "version_conflict"
        assert preview(browser, platform, job).json()["code"] == "version_conflict"
        assert path.read_bytes() == modified
    assert not platform.tasks and not store.rows("SELECT * FROM deliveries")


def test_accepted_manual_request_recovers_without_yaml_or_resubmit(host):
    client, platform, store, job, _ = host

    async def lose_response(request):
        raise httpx.ReadError("已受理但连接断开", request=request)

    platform.after_accept = lose_response
    with client() as browser:
        accepted = confirm(browser, preview(browser, platform, job).json()).json()
        assert accepted["task_id"] is None
    row = store.row("SELECT * FROM deliveries")
    Path(row["manifest_path"]).unlink()
    platform.previews.clear()
    with client() as restarted:
        current = restarted.get(BASE + "/" + accepted["id"]).json()
        assert current["task_id"] == platform.tasks[row["request_key"]][1]["task_ids"][0]
        assert [entry[0] for entry in platform.trace] == ["PREVIEW", "POST", "GET"]
        with pytest.raises(BridgeError, match="未完成"):
            asyncio.run(platform.client.bound_request(DEPLOYMENT, "/submissions", body={"preview_id": current["task_id"]},
                                                      key=row["request_key"]))


@pytest.mark.parametrize("edit", [False, True])
def test_expired_unaccepted_manual_request_reuses_work_and_requires_confirmation(host, edit):
    client, platform, store, job, _ = host

    async def lose_request(request):
        raise httpx.ConnectError("首次请求未到达", request=request)

    platform.on_submit = lose_request
    with client() as browser:
        stage = preview(browser, platform, job).json()
        original = confirm(browser, stage).json()
    row = store.row("SELECT * FROM deliveries")
    path = Path(row["manifest_path"])
    original_yaml = path.read_bytes()
    original_mtime = path.stat().st_mtime_ns
    assert row["sent"] and not row["rejected"] and not platform.tasks
    platform.on_submit = None
    platform.previews.clear()
    route = BASE + "/" + original["id"]
    with client() as browser:
        # 只读查不到回执时仍须保留原请求。
        assert not browser.get(route).json()["can_revise"]
        recovered = browser.post(route + "/continue", json={}).json()
        assert recovered["can_revise"] and not recovered["can_continue"]
        assert [item[0] for item in platform.trace[-3:]] == ["GET", "POST", "GET"]
        assert platform.trace[-2] == ("POST", row["request_key"], json.loads(row["request"]))
        assert not platform.tasks
        fields = browser.get(f"{BASE}/context/{job}/1", params={"revise": original["id"]}).json()["defaults"]
        assert fields == FIELDS
        if edit:
            fields["description"] = "用户明确修改的简介"
        first = preview(browser, platform, job, fields, revise=original["id"]).json()
        updated = preview(browser, platform, job, fields, revise=original["id"]).json()
        assert first["work"]["id"] == updated["work"]["id"] == row["work_id"]
        assert len(store.rows("SELECT id FROM deliveries")) == 1 and not platform.tasks
        if not edit:
            assert path.read_bytes() == original_yaml and path.stat().st_mtime_ns == original_mtime
        accepted = confirm(browser, updated).json()
        assert confirm(browser, updated).json()["task_id"] == accepted["task_id"]
        assert confirm(browser, first).json()["task_id"] == accepted["task_id"]
    with client() as restarted:
        current = restarted.get(BASE, params={"keys": f"{job}:1"}).json()["items"]
        assert [item["task_id"] for item in current] == [accepted["task_id"]]
    new = store.row("SELECT * FROM deliveries WHERE task_id IS NOT NULL")
    assert new["manifest_path"] == row["manifest_path"]
    assert (new["config_version"] != row["config_version"]) == edit
    assert new["request_key"] != row["request_key"] and new["request"] != row["request"]
    assert len(platform.tasks) == 1


def test_receipt_found_after_expired_replay_keeps_original_task(host):
    client, platform, store, job, _ = host

    async def lose_request(request):
        raise httpx.ConnectError("首次请求未到达", request=request)

    platform.on_submit = lose_request
    with client() as browser:
        original = confirm(browser, preview(browser, platform, job).json()).json()
        row = store.row("SELECT * FROM deliveries")
        platform.previews.clear()
        task = str(uuid4())

        async def late_acceptance(request):
            platform.tasks[row["request_key"]] = (json.loads(row["request"]), {
                "work_id": row["work_id"], "config_version": row["config_version"],
                "status": "accepted", "task_ids": [task],
            })

        platform.on_submit = late_acceptance
        current = browser.post(BASE + "/" + original["id"] + "/continue", json={}).json()
        assert current["task_id"] == task and not current["can_revise"]
        assert [item[0] for item in platform.trace[-3:]] == ["GET", "POST", "GET"]
        assert len(store.rows("SELECT id FROM deliveries")) == 1


def test_prior_data_is_never_opened_as_current_or_rewritten(tmp_path):
    old = tmp_path / "orchestrator.sqlite3"
    with sqlite3.connect(old) as db:
        db.executescript("CREATE TABLE old_records (value); INSERT INTO old_records VALUES (123); PRAGMA user_version=5;")
    original = old.read_bytes()
    with pytest.raises(ValueError, match="拒绝覆盖"):
        Store(old)
    new = Store(tmp_path / "independent" / old.name)
    try:
        assert new.rows("SELECT * FROM deliveries") == []
    finally:
        new.close()
    assert old.read_bytes() == original


def test_manifest_directory_cannot_point_outside_output_root(tmp_path):
    root, other = tmp_path / "recordings", tmp_path / "outside"
    root.mkdir()
    other.mkdir()
    if os.name == "nt":
        subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-CommandWithArgs",
                        "New-Item -ItemType Junction -Path $args[0] -Target $args[1] | Out-Null",
                        str(root / "投稿"), str(other)], check=True)
    else:
        (root / "投稿").symlink_to(other, target_is_directory=True)
    with pytest.raises(BridgeError, match="越界"):
        manifest_path(root, 1)


def test_preflight_rejects_frozen_neighbor_with_same_filename(host):
    client, platform, store, job, clip = host

    async def offline(request):
        raise httpx.ConnectError("模拟平台未收到请求", request=request)

    platform.on_submit = offline
    with client() as browser:
        stage = preview(browser, platform, job).json()
        record = confirm(browser, stage).json()
    neighbor = clip.parent.with_name(clip.parent.name + "-2")
    neighbor.mkdir()
    (neighbor / clip.name).write_bytes(clip.read_bytes())
    row = store.row("SELECT * FROM deliveries WHERE id=?", (record["id"],))
    snapshot = json.loads(row["frozen"])
    snapshot["clips"][0]["file"]["path"] = str(neighbor / clip.name)
    with store.transaction() as db:
        db.execute("UPDATE deliveries SET frozen=? WHERE id=?", (json.dumps(snapshot), row["id"]))
    platform.on_submit = None
    with client() as browser:
        result = browser.post(f"{BASE}/{row['id']}/continue", json={}).json()
        assert "已变化" in result["problem"]
    assert not platform.tasks and [r[0] for r in platform.trace].count("POST") == 1

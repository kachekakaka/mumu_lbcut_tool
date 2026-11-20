"""整场交付的标题、台账、排他和恢复。Core 网络回包可以受控。"""

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from console_helpers import DeferredQueries, observe_room
from src.mvp.room_resolver import ResolvedRoom, RoomStatus
from src.orchestrator.room_query_process import QueryResult
from src.orchestrator.scheduler import Scheduler
from src.orchestrator.segment_uploads import SegmentUploads
from src.orchestrator.session_deliveries import SessionDeliveries
from src.orchestrator.storage import Store
from upload_platform_fixture import ACCOUNT, Platform


STARTED = datetime.fromisoformat("2026-11-20T20:09:00+08:00").timestamp()
BINDING = {"deployment": "A", "category_id": 171,
           "account": ACCOUNT}
FIELDS = {"title": "测试主播｜2026-11-20 20:09｜直播回放", "description": "", "tags": ["直播回放", "测试主播"],
          "category_id": 171, "copyright": 2, "source": "转载", "publish_mode": "self_only"}


def room(title=None, number="1200"):
    return ResolvedRoom("live-a", RoomStatus.LIVE, number, title, "测试主播", "9001")


def add_job(store, target, observed, now):
    observe_room(store, target, observed, now)
    job_id = store.create_pending(target, now)
    with store.transaction() as db:
        db.execute("UPDATE jobs SET status='starting' WHERE id=?", (job_id,))
        db.execute("INSERT INTO recording_admissions VALUES (?,?,?,?,?,?)",
                   (job_id, observed.room_id, observed.anchor_uid, observed.nickname, observed.web_rid, now))
    return job_id


def emit(store, job, kind, stamp, **fields):
    seq = store.row("SELECT COUNT(*) AS n FROM events WHERE job_id=?", (job,))["n"] + 1
    event = dict(event=kind, job_id=job, run_id=job, event_seq=seq, event_id=f"{job}:{seq}",
                 timestamp=stamp, **fields)
    store.record_event(job, event, stamp)
    return event


def start_clip(store, job, index, path, stamp, identity="verified"):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(b"synthetic clip")
    return emit(store, job, "segment_started", stamp, segment_index=index,
                file_path=str(path), room_identity_status=identity)


def finish_clip(store, job, index, path, stamp, identity="verified"):
    emit(store, job, "segment_finished", stamp, segment_index=index, file_path=str(path),
         room_identity_status=identity, duration_sec=2, is_final=index == 1)


def finish_worker(store, job, stamp, code=0):
    emit(store, job, "worker_stopped", stamp, exit_code=code)
    store.finish_worker(job, stamp, code)


def current(store, target):
    return store.row("SELECT * FROM recording_sessions WHERE target_id=? ORDER BY started_at DESC LIMIT 1", (target,))


def test_opening_title_is_frozen_and_replay_and_reused_rooms_stay_separate(tmp_path):
    store = Store(tmp_path / "schedule.sqlite3")
    queries = DeferredQueries()
    scheduler = Scheduler(store, tmp_path, pool=SimpleNamespace(refresh=lambda: []), auto_scan=False,
                          query_executor=queries, clock=lambda: STARTED)
    try:
        target = store.add_target("live-a")
        a = add_job(store, target, room("复核时标题"), STARTED - 1)
        event = start_clip(store, a, 1, tmp_path / "a.mp4", STARTED)
        scheduler._session_titles()
        assert [(kind, raw) for kind, raw, _ in queries.calls] == [("title", "live-a")]
        queries.calls[-1][2].set_result(QueryResult(room("首片开录标题")))
        scheduler._session_titles()
        store.record_event(a, event, STARTED + 4, replay=True)
        assert current(store, target)["title"] == "首片开录标题"
        assert current(store, target)["started_at"] == STARTED
        finish_clip(store, a, 1, tmp_path / "a.mp4", STARTED + 5)
        finish_worker(store, a, STARTED + 6, code=20)
        assert current(store, target)["status"] == "recording"
        assert current(store, target)["close_reason"] is None
        # 同一房间内换实例的开录沿用本场资料。
        b = add_job(store, target, room("已改题"), STARTED + 7)
        start_clip(store, b, 1, tmp_path / "b.mp4", STARTED + 8)
        scheduler._session_titles()
        assert len(queries.calls) == 1
        finish_clip(store, b, 1, tmp_path / "b.mp4", STARTED + 9)
        finish_worker(store, b, STARTED + 10)
        old = current(store, target)["session_key"]
        observe_room(store, target, ResolvedRoom("live-a", RoomStatus.ENDED), STARTED + 11)
        observe_room(store, target, ResolvedRoom("live-a", RoomStatus.ENDED), STARTED + 13)
        assert current(store, target)["status"] == "only_record"
        c = add_job(store, target, room(), STARTED + 14)
        start_clip(store, c, 1, tmp_path / "c.mp4", STARTED + 15)
        assert current(store, target)["session_key"] != old
        assert current(store, target)["title"] is None
    finally:
        scheduler.close()
        store.close()


@pytest.mark.parametrize("result, expected", [
    (QueryResult(room(" 收尾的标题 ")), "收尾的标题"),
    (QueryResult(room("另一场标题", "1300")), None),
    (QueryResult(room(" ")), None),
    (QueryResult(ResolvedRoom("live-a", RoomStatus.ENDED)), None),
])
def test_missing_title_falls_back_only_after_manual_close(tmp_path, result, expected):
    store = Store(tmp_path / "schedule.sqlite3")
    queries = DeferredQueries()
    stamp = [STARTED]
    scheduler = Scheduler(store, tmp_path, pool=SimpleNamespace(refresh=lambda: []), auto_scan=False,
                          query_executor=queries, clock=lambda: stamp[0])
    try:
        target = store.add_target("live-a")
        job = add_job(store, target, room(), STARTED - 1)
        path = tmp_path / "a.mp4"
        start_clip(store, job, 1, path, STARTED)
        scheduler._session_titles()
        store.request_stop(job, "manual_stop")
        finish_clip(store, job, 1, path, STARTED + 1)
        finish_worker(store, job, STARTED + 2)
        scheduler._session_titles()
        assert len(queries.calls) == 1
        stamp[0] += 31
        scheduler._session_titles()
        assert len(queries.calls) == 2
        queries.calls[-1][2].set_result(result)
        scheduler._session_titles()
        queries.calls[0][2].set_result(QueryResult(room("迟到的开录标题")))
        scheduler._session_titles()
        assert current(store, target)["title"] == expected
        assert store.row("SELECT trusted_status FROM targets WHERE id=?", (target,))["trusted_status"] == "live"
        assert len(queries.calls) == 2
    finally:
        scheduler.close()
        store.close()


@pytest.fixture
def delivery(tmp_path):
    store = Store(tmp_path / "schedule.sqlite3")
    target = store.add_target("live-a")
    store.save_posting_settings(target, True, BINDING)
    root = tmp_path / "recordings"
    platform = Platform()

    def services(**options):
        runtime = SimpleNamespace(store=store, state="running")
        uploads = SegmentUploads(runtime, platform.client, root)
        return uploads, SessionDeliveries(runtime, uploads,
                                         verify_media=lambda path: SimpleNamespace(is_valid=path.read_bytes() != b"broken",
                                                                                   reject_reason="损坏"), **options)

    yield store, target, root, platform, services
    store.close()


def close_recording(store, target, root, *, multipart=False, flag=None, identity="verified", broken=False, unfinished=False):
    a = add_job(store, target, room(), STARTED - 1)
    path = root / a / "000001.mp4"
    start_clip(store, a, 1, path, STARTED, identity)
    if flag:
        emit(store, a, "segment_quality_warning", STARTED, segment_index=1, flag=flag)
    if broken:
        path.write_bytes(b"broken")
    if not unfinished:
        finish_clip(store, a, 1, path, STARTED + 2, identity)
    if multipart:
        second = root / a / "000002.mp4"
        start_clip(store, a, 2, second, STARTED + 2)
        finish_clip(store, a, 2, second, STARTED + 4)
        finish_worker(store, a, STARTED + 5, code=20)
        assert current(store, target)["status"] == "recording"
        b = add_job(store, target, room(), STARTED + 6)
        last = root / b / "000001.mp4"
        start_clip(store, b, 1, last, STARTED + 7)
        finish_clip(store, b, 1, last, STARTED + 9)
    else:
        b = a
    store.request_stop(b, "manual_stop")
    assert current(store, target)["status"] == "recording"
    finish_worker(store, b, STARTED + 10)
    with store.transaction() as db:
        db.execute("UPDATE recording_sessions SET title_step='done' WHERE target_id=?", (target,))
    return a, b


@pytest.mark.parametrize("multipart", [False, True])
def test_order_only_includes_session_and_overlay_keeps_parts(delivery, multipart):
    store, target, root, core, services = delivery
    a, b = close_recording(store, target, root, multipart=multipart, flag="overlay_panel")
    neighbor = root / "neighbor" / "000001.mp4"
    neighbor.parent.mkdir()
    neighbor.write_bytes(b"adjacent stream")

    async def scenario():
        uploads, service = services()
        await service.deliver(current(store, target))
        await service.deliver(current(store, target))
        session = current(store, target)
        assert session["status"] == "delivered"
        row = uploads.store.get(session["arrangement_id"])
        assert row["task_id"]
        path = Path(row["manifest_path"])
        assert path == root / "sessions" / row["work_id"] / "upload.yaml"
        manifest = yaml.safe_load(path.read_bytes())
        paths = [a + "/000001.mp4"] + ([a + "/000002.mp4", b + "/000001.mp4"] if multipart else [])
        assert [video["path"] for video in manifest["assets"]["videos"]] == paths
        assert manifest["upload_units"][0]["mode"] == ("multipart" if multipart else "independent")
        assert manifest["work"]["title"] == "测试主播｜2026-11-20 20:09｜直播回放"
        keys = [(a, 1)] + ([(a, 2), (b, 1)] if multipart else [])
        records = uploads.store.for_segments(keys)
        assert {item["task_id"] for item in records} == {row["task_id"]}
        assert len(records) == len(keys)
        assert all((root / name).is_file() for name in paths)
        assert neighbor.read_bytes() == b"adjacent stream"
        assert len(core.tasks) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("clip, reason", [
    ({"unfinished": True}, "未正常封口"),
    ({"flag": "identity_mismatch"}, "录错房"),
    ({"identity": "unconfirmed"}, "身份未核实"),
    ({"broken": True}, "损坏"),
])
def test_bad_clip_keeps_original_and_never_creates_arrangement(delivery, clip, reason):
    store, target, root, core, services = delivery
    close_recording(store, target, root, **clip)
    _, service = services()
    if current(store, target)["status"] == "checking":
        asyncio.run(service.deliver(current(store, target)))
    session = current(store, target)
    assert session["status"] == "review" and reason in session["problem"]
    assert not core.trace and len(list(root.glob("*/*.mp4"))) == 1


@pytest.mark.parametrize("when", ["before_creation", "before_send", "in_flight"])
def test_disabling_posting_stops_sends_and_tracks_in_flight_order(delivery, when):
    store, target, root, core, services = delivery
    a, _ = close_recording(store, target, root, multipart=when == "in_flight")

    async def disable(request=None):
        store.save_posting_settings(target, False)
        if when == "in_flight":
            raise httpx.ReadError("提交回包中断", request=request)

    async def scenario():
        uploads, service = services()
        await service.deliver(current(store, target))
        if when == "before_creation":
            await disable()
        elif when == "before_send":
            await disable()
        else:
            core.after_accept = disable
        if current(store, target)["status"] == "pending":
            await service.deliver(current(store, target))
        uploads, service = services()
        if current(store, target)["status"] == "pending":
            await service.deliver(current(store, target))
        saved = current(store, target)
        if when == "in_flight":
            assert saved["status"] == "delivered"
            assert len(core.tasks) == 1 and any(step[0] == "GET" for step in core.trace)
        else:
            assert saved["status"] == "only_record" and not core.tasks
            assert (await uploads.context(a, 1))["accounts"]
        assert sum(step[0] == "POST" for step in core.trace) == int(when == "in_flight")
        store.save_posting_settings(target, True, BINDING)
        assert current(store, target)["status"] == saved["status"]

    asyncio.run(scenario())


def test_manual_confirm_after_session_freeze_links_original_task(delivery):
    store, target, root, core, services = delivery
    a, _ = close_recording(store, target, root)

    async def scenario():
        uploads, service = services()
        preview = await uploads.preview(a, 1, "A", ACCOUNT, FIELDS)
        await service.deliver(current(store, target))
        await service.deliver(current(store, target))
        session = current(store, target)
        assert session["status"] == "delivered"
        manual = await uploads.confirm(preview["ticket"], True)
        assert manual["task_id"] == uploads.store.get(session["arrangement_id"])["task_id"]
        assert len([step for step in core.trace if step[0] == "POST"]) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("disable", [False, True])
def test_missing_submit_uses_frozen_request_only_if_still_enabled(delivery, disable):
    store, target, root, core, services = delivery
    close_recording(store, target, root, multipart=True)

    async def scenario():
        uploads, service = services()
        await service.deliver(current(store, target))
        async def fail_before_accept(request):
            raise httpx.ConnectError("未知提交结果", request=request)

        core.on_submit = fail_before_accept
        await service.deliver(current(store, target))
        assert not core.tasks
        if disable:
            store.save_posting_settings(target, False)
        core.on_submit = None
        uploads, service = services()
        await service.deliver(current(store, target))
        assert any(step[0] == "GET" for step in core.trace)
        replays = [step for step in core.trace if step[0] == "POST"]
        saved = current(store, target)
        if disable:
            assert len(replays) == 1 and not core.tasks
            assert "暂未查到原任务" in saved["problem"]
        else:
            assert len(replays) == 2 and replays[0] == replays[1]
            assert len(core.tasks) == 1 and saved["status"] == "delivered"

    asyncio.run(scenario())


def test_automatic_delivery_finds_concurrent_identical_manual_order(delivery):
    store, target, root, core, services = delivery
    a, _ = close_recording(store, target, root)

    async def scenario():
        uploads, service = services()
        dialog = await uploads.preview(a, 1, "A", ACCOUNT, FIELDS)
        started, release = asyncio.Event(), asyncio.Event()

        async def block_receipt(request):
            started.set()
            await release.wait()

        core.after_accept = block_receipt
        manual = asyncio.create_task(uploads.confirm(dialog["ticket"], True))
        try:
            await asyncio.wait_for(started.wait(), 5)
            await service.deliver(current(store, target))
            saved = current(store, target)
            assert saved["status"] == "pending" and saved["arrangement_id"]
        finally:
            release.set()
        manual_result = await manual
        await service.deliver(current(store, target))
        saved = current(store, target)
        assert manual_result["task_id"] == uploads.store.get(saved["arrangement_id"])["task_id"]
        assert len(core.tasks) == 1 and len([step for step in core.trace if step[0] == "POST"]) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("file_written", [False, True])
def test_frozen_manifest_resumes_with_original_work_and_colon_session(delivery, monkeypatch, file_written):
    store, target, root, platform, services = delivery
    close_recording(store, target, root, multipart=True)
    session = current(store, target)
    assert ":" in session["session_key"]
    platform.available = False

    class Interrupted(BaseException):
        pass

    async def scenario():
        uploads, service = services()
        with monkeypatch.context() as patch:
            if file_written:
                patch.setattr(uploads.store, "save_session_manifest", lambda *_: interrupt())
            else:
                patch.setattr(service, "write_manifest", lambda *_: interrupt())
            with pytest.raises(Interrupted):
                await service.deliver(session)
        saved = current(store, target)
        original = uploads.store.get(saved["arrangement_id"])
        path = root / "sessions" / original["work_id"] / "upload.yaml"
        assert path.is_file() == file_written
        content = path.read_bytes() if file_written else None
        uploads, service = services()
        await service.deliver(saved)
        await service.deliver(current(store, target))
        record = uploads.store.get(saved["arrangement_id"])
        assert record["work_id"] == original["work_id"]
        assert str(path) == record["manifest_path"]
        assert content is None or path.read_bytes() == content
        assert current(store, target)["status"] == "pending" and not platform.tasks

    def interrupt():
        raise Interrupted

    asyncio.run(scenario())


def test_partial_overlap_points_to_conflicting_clip(delivery):
    store, target, root, platform, services = delivery
    a, _ = close_recording(store, target, root, multipart=True)

    async def scenario():
        uploads, service = services()
        preview = await uploads.preview(a, 1, "A", ACCOUNT, FIELDS)
        result = await uploads.confirm(preview["ticket"], True)
        assert result["task_id"]
        await service.deliver(current(store, target))
        saved = current(store, target)
        assert saved["status"] == "review"
        assert a in saved["problem"] and "第 1 片" in saved["problem"]
        assert len(platform.tasks) == 1

    asyncio.run(scenario())

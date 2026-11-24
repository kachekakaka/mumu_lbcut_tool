"""用真实文件和重开的 SQLite 验证场内编号、无覆盖及中断原片。"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.orchestrator import recording_archive as archive
from src.orchestrator.scheduler import Scheduler
from src.orchestrator.storage import Store
from src.mvp.room_resolver import ResolvedRoom, RoomStatus
from console_helpers import DeferredQueries
from test_session_deliveries import STARTED, add_job, capture, current, finish_clip, finish_worker, room, start_clip


def segment(store, job, directory, number, at):
    path = directory / f"part_{number:06d}.mp4"
    start_clip(store, job, number, path, at)
    finish_clip(store, job, number, path, at + 1)
    return path


@pytest.mark.parametrize("ending", ["no_start", "no_terminal", "terminal"])
def test_recover_third_clip_and_resume_same_session_after_midnight(tmp_path, ending):
    database, root = tmp_path / "data.sqlite3", tmp_path / "录像 盘"
    store = Store(database)
    target = store.add_target("live-a")
    job = add_job(store, target, room(), STARTED - 5)
    directory = capture(store, job, root)
    for index in (1, 2):
        segment(store, job, directory, index, STARTED + index * 2)
    archive.publish(store)
    third = directory / "part_000003.mp4"
    original = b"\0\0\0\0sample raw third clip\x80"
    third.write_bytes(original)
    if ending != "no_start":
        start_clip(store, job, 3, third, STARTED + 9)
    if ending == "terminal":
        finish_clip(store, job, 3, third, STARTED + 10)
    uncertain = directory / "part_000005.mp4"
    uncertain.write_bytes("未识别".encode("utf-8"))
    store.close()

    store = Store(database)
    for _ in range(2):
        archive.recover_unfinished(store, job, STARTED + 11)
        store.finish_worker(job, STARTED + 11, 20)
        archive.publish(store)
    session = Path(current(store, target)["recording_directory"])
    third_name = "003.mp4" if ending == "terminal" else "003-中断.mp4"
    assert sorted(path.name for path in session.glob("*.mp4")) == ["001.mp4", "002.mp4", third_name]
    assert (session / third_name).read_bytes() == original
    assert not third.exists() and uncertain.exists()
    next_day = STARTED + 6 * 3600
    retry = add_job(store, target, room(), next_day)
    segment(store, retry, capture(store, retry, root, first_media=next_day), 1, next_day)
    archive.publish(store)
    assert Path(current(store, target)["recording_directory"]) == session
    assert (session / "004.mp4").is_file()
    store.close()


@pytest.mark.parametrize("destination", ["absent", "same_file", "collision"])
def test_replay_archive_intent_never_replaces_destination(tmp_path, destination):
    database, root = tmp_path / "data.sqlite3", tmp_path / "录像"
    store = Store(database)
    target = store.add_target("live-a")
    job = add_job(store, target, room(), STARTED)
    source = segment(store, job, capture(store, job, root), 1, STARTED)
    clip = archive.location(store, job, 1)
    final = Path(clip["archive_path"])
    assert clip["archive_state"] == "pending" and source.is_file()
    if destination == "same_file":
        final.hardlink_to(source)
    elif destination == "collision":
        final.write_bytes(b"existing")
    store.close()

    store = Store(database)
    archive.publish(store)
    archive.publish(store)
    clip = archive.location(store, job, 1)
    assert clip["session_index"] == 1
    if destination == "collision":
        assert clip["archive_state"] == "review" and source.is_file()
        assert final.read_bytes() == b"existing"
    else:
        assert clip["archive_state"] == "archived" and not source.exists()
        assert final.read_bytes() == b"synthetic clip"
    store.close()


def test_same_name_same_minute_and_rename_keep_distinct_directories(tmp_path):
    store = Store(tmp_path / "data.sqlite3")
    root = tmp_path / "录像"
    root.mkdir()
    (root / "_CON").mkdir()
    first = store.add_target("live-a")
    second = store.add_target("live-b")

    def make(target, room_id, uid, nickname, at):
        web_rid = store.row("SELECT raw FROM targets WHERE id=?", (target,))["raw"]
        observed = ResolvedRoom(web_rid, RoomStatus.LIVE, room_id, None, nickname, uid)
        job = add_job(store, target, observed, at)
        segment(store, job, capture(store, job, root, first_media=at), 1, at)
        archive.publish(store)
        finish_worker(store, job, at + 2)
        return Path(current(store, target)["recording_directory"])

    a = make(first, "1200", "9001", "CON", STARTED)
    b = make(second, "1300", "9002", "con", STARTED)
    c = make(first, "1400", "9001", "改名了", STARTED + 10)
    assert a.parent.name == "_CON-2" and b.parent.name == "_con-3"
    assert a.parent == c.parent and c.name == a.name + "-2"
    assert (root / "_CON").is_dir()
    store.close()


def test_replay_waits_for_old_writer_and_uses_saved_normal_terminal_event(tmp_path):
    database, root = tmp_path / "data.sqlite3", tmp_path / "录像"
    store = Store(database)
    target = store.add_target("live-a")
    job = add_job(store, target, room(), STARTED)
    directory = capture(store, job, root)
    source = directory / "part_000001.mp4"
    start = start_clip(store, job, 1, source, STARTED)
    end = {**start, "event": "segment_finished", "event_id": job + ":2", "event_seq": 2,
           "timestamp": STARTED + 1, "duration_sec": 1, "is_final": True}
    journal = tmp_path / "worker.ndjson"
    journal.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in (start, end)), encoding="utf-8")
    with store.transaction() as db:
        db.execute("UPDATE jobs SET event_log_path=? WHERE id=?", (str(journal), job))
    store.close()

    store = Store(database)
    clock, process = [STARTED + 2], ["alive"]
    scheduler = Scheduler(store, root, auto_scan=False, query_executor=DeferredQueries(),
                          pool=SimpleNamespace(refresh=lambda: []), clock=lambda: clock[0],
                          process_probe=lambda _: process[0])
    try:
        scheduler._watch_old_workers()
        assert not scheduler._replays and source.is_file()
        assert not list(Path(current(store, target)["recording_directory"]).glob("*.mp4"))
        process[0] = "absent"
        clock[0] += 3
        scheduler._watch_old_workers()
        scheduler._finish_orphan(store.row("SELECT * FROM jobs WHERE id=?", (job,)))
        archive.publish(store)
        archive.publish(store)
        final = Path(current(store, target)["recording_directory"]) / "001.mp4"
        assert final.read_bytes() == b"synthetic clip"
        assert not source.exists() and not final.with_name("001-中断.mp4").exists()
    finally:
        scheduler.close()
        store.close()

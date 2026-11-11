"""最小调度接入前的主流程边界回归，不启动真实模拟器。"""
import io
import threading
from pathlib import Path
from unittest.mock import Mock

from src.mvp.room_resolver import ResolvedRoom, RoomStatus
from src.orchestrator.storage import Store
from console_helpers import observe_room
from src.orchestrator.models import WorkerLaunchSpec
from src.orchestrator.worker_process import WorkerProcessHandle
from src.worker.navigator import WorkerNavigator


def test_unknown_must_not_erase_last_confirmed_live(tmp_path):
    store = Store(tmp_path / 'state.sqlite3')
    try:
        target = store.add_target('123')
        for n, status in enumerate((RoomStatus.LIVE, RoomStatus.UNKNOWN, RoomStatus.ENDED, RoomStatus.ENDED)):
            observe_room(store, target, ResolvedRoom('123', status), 10 + n * 2)
            expected = 'ended' if n == 3 else 'live'
            assert store.row('SELECT trusted_status FROM targets WHERE id=?', (target,))['trusted_status'] == expected
    finally:
        store.close()


def test_unknown_then_live_must_not_start_duplicate(tmp_path):
    store = Store(tmp_path / 'state.sqlite3')
    try:
        target = store.add_target('123')
        ids = []
        for n, status in enumerate((RoomStatus.LIVE, RoomStatus.UNKNOWN, RoomStatus.LIVE)):
            observe_room(store, target, ResolvedRoom('123', status), 10 + n * 2)
            ids.append(store.create_pending(target, 10 + n * 2))
        assert len(set(ids)) == 1
    finally:
        store.close()


def test_short_id_refresh_uses_same_web_route_as_launch(tmp_path):
    adb = tmp_path / "adb.exe"
    adb.touch()
    nav = WorkerNavigator("test-device", adb_exe=adb)
    nav._adb = Mock(return_value="Starting: Intent")
    assert nav.refresh_current_room("1234")
    calls = [tuple(c.args) for c in nav._adb.call_args_list]
    assert any("https://live.douyin.com/1234" in c for c in calls)
    assert not any("snssdk1128://live?room_id=1234" in c for c in calls)


def test_stop_of_exited_worker_waits_for_final_event(tmp_path, monkeypatch):
    release = threading.Event()

    class DelayedStream:
        def __iter__(self):
            assert release.wait(2)
            yield '{"event":"segment_finished","segment_index":1,"file_path":"tail.mp4","is_final":true}\n'
        def close(self):
            pass

    class Process:
        pid = 99999999
        stdout = DelayedStream()
        stderr = io.StringIO("")
        def poll(self):
            return 0
        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr("src.orchestrator.worker_process.subprocess.Popen", lambda *a, **kw: Process())
    handle = WorkerProcessHandle(WorkerLaunchSpec(1, "123", tmp_path))
    handle.start()
    timer = threading.Timer(0.05, release.set)
    timer.start()
    try:
        assert handle.stop(timeout_sec=2) == 0
        assert handle.finished_segments == ["tail.mp4"]
    finally:
        release.set()
        timer.join()
        handle.wait(timeout=2)

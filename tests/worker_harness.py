"""保留真实 Controller/状态机/归档器，只替换设备与录制进程边界。"""
import io
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

from src.worker.controller import WorkerConfig, WorkerController
from src.worker.events import WorkerEventEmitter
from src.worker.room_locator import RoomEvidence


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.on_sleep = None
    def monotonic(self):
        return self.now
    def time(self):
        return 1800000000.0 + self.now
    def sleep(self, seconds):
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(self.now)


class PassiveWatchdog:
    """Controller 集成测试的外部采样边界；实际采样算法另有真实图像测试。"""
    def __init__(self, **kwargs):
        self.kwargs = kwargs
    def start(self):
        pass
    def stop(self):
        pass
    def notify_segment_reset(self):
        pass
    def snapshot(self):
        return None


def build_worker(monkeypatch, tmp_path, valid_media, *, total=4, segment=2, overlap=0,
                 gate=True, gate_reason="render_timeout", uid="900001", expected_uid="900001",
                 allow_unconfirmed=False, crashes=(), broken_segments=()):
    import src.worker.controller as cm
    import src.worker.pipeline as pm
    clock = FakeClock()
    pipelines = []
    launched = []

    class RecordingProcess:
        def __init__(self, serial, output_dir, **kwargs):
            self.output_dir = Path(output_dir)
            self.current_file_path = None
            self.current_segment_index = 0
            self.alive = False
            self.exit_code = None
            self.started = 0
            self.wait_hook = None
            pipelines.append(self)
        def start_segment(self, index):
            self.current_segment_index = index
            self.current_file_path = self.output_dir / f"raw_{index}.mp4"
            self.output_dir.mkdir(parents=True, exist_ok=True)
            if self.current_file_path.exists():
                raise FileExistsError(str(self.current_file_path))
            if index in broken_segments:
                self.current_file_path.write_bytes(b"bad media")
            else:
                shutil.copyfile(valid_media, self.current_file_path)
            self.alive = True
            self.exit_code = None
            self.started = clock.monotonic()
            launched.append(index)
            return self.current_file_path
        def is_recording(self):
            if self.alive and self.current_segment_index in crashes and clock.monotonic() - self.started >= .5:
                self.alive = False
                self.exit_code = 9
            return self.alive
        def get_exit_code(self):
            return self.exit_code
        def stop_segment(self, timeout_sec=8):
            if self.alive:
                self.alive = False
                self.exit_code = 0
            return self.current_file_path is not None and self.current_file_path.exists()
        def get_current_file_size_mb(self):
            return self.current_file_path.stat().st_size / 1048576 if self.current_file_path and self.current_file_path.exists() else 0
        def get_current_segment_elapsed(self):
            return clock.monotonic() - self.started
        def teardown(self):
            self.stop_segment()

    class Navigator:
        last_gate_reason = gate_reason
        adb_client = object()
        def __init__(self, **kwargs):
            self.route = None
        def configure_target(self, kind, web_rid):
            self.route = (kind, web_rid)
        def navigate_route(self, target, web_rid, route, deadline, stop_requested):
            return not stop_requested()
        def wait_live_page(self, **kwargs):
            return gate
        def observe_live_room(self, **kwargs):
            return RoomEvidence(anchor_uid=uid, nickname='synthetic anchor', source='synthetic-device-observation')
        def wait_live_picture(self, **kwargs):
            return gate
        def get_current_focus_activity(self):
            return 'com.ss.android.ugc.aweme.LivePlayActivity'
        def launch_room(self, **kwargs):
            return True
        def dismiss_system_dialogs(self):
            pass
        def detect_current_room_identity(self):
            return uid, "synthetic anchor"
        def verify_preflight_content_gate(self, timeout_sec):
            return gate
        def refresh_current_room(self, room_id):
            return True

    monkeypatch.setattr(cm, "time", clock)
    monkeypatch.setattr(cm, "WorkerNavigator", Navigator)
    monkeypatch.setattr(cm, "DependencyManager", lambda: SimpleNamespace(
        ensure_scrcpy=lambda: Path("scrcpy.exe"), get_adb_path=lambda: Path("adb.exe")))
    monkeypatch.setattr(cm, "MuMuClient", lambda **kw: SimpleNamespace(
        launch_player=lambda index: True, connect_adb=lambda index: "synthetic-device",
        optimize_performance_for_recording=lambda *args, **kw: None))
    monkeypatch.setattr(cm, "PopupBypasser", lambda **kw: SimpleNamespace(detect_and_dismiss_popups=lambda: False))
    monkeypatch.setattr(cm, "StallWatchdog", PassiveWatchdog)
    monkeypatch.setattr(cm, "WorkerPipeline", RecordingProcess)
    monkeypatch.setattr(pm, "WorkerPipeline", RecordingProcess)
    stream = io.StringIO()
    config = WorkerConfig(recording_mode="restart", vmindex=321, target="7704666294802123554", target_kind="room_id",
        expected_anchor_uid=expected_uid, allow_unconfirmed_identity=allow_unconfirmed,
        output_dir=tmp_path / "outputs", lock_dir=tmp_path / "locks", total_duration_sec=total,
        segment_duration_sec=segment, overlap_duration_sec=overlap, heartbeat_interval_sec=1)
    controller = WorkerController(config, WorkerEventEmitter(stream))
    return SimpleNamespace(controller=controller, clock=clock, stream=stream, launched=launched,
        pipelines=pipelines, events=lambda: [json.loads(line) for line in stream.getvalue().splitlines()])


def assert_paired(events):
    starts = [e for e in events if e['event'] == 'segment_started']
    terminals = [e for e in events if e['event'] in ('segment_finished', 'segment_aborted')]
    assert len(starts) == len(terminals)
    assert len({e['segment_index'] for e in starts}) == len(starts)
    for start in starts:
        matching = [e for e in terminals if e['segment_index'] == start['segment_index']]
        assert len(matching) == 1
        assert Path(matching[0]['file_path']).exists()
    return terminals

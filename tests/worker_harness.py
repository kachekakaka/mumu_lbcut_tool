"""保留真实 Controller/状态机/采集会话/封口校验，只替换设备与采集进程边界。"""
import io
import json
from pathlib import Path
import shutil
import time
from types import SimpleNamespace

import numpy as np

from src.worker.continuous_pipeline import Fragment
from src.worker.stream_monitor import FRAME_H, FRAME_W, MonitorView
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


class FakeStreamMonitor:
    """录像流分析替身：每次查询给出一帧新的灰度画面（默认画面不变）。"""
    def __init__(self, value=80):
        self.available = True
        self.unavailable = False
        self.new_frames = True
        self.frozen_since = None
        self.seq = 0
        self.set_value(value)
    def set_value(self, value):
        self.frame = np.full((FRAME_H, FRAME_W), value, dtype=np.uint8)
    def view(self):
        if self.new_frames:
            self.seq += 1
        return MonitorView(self.available, self.seq, self.frame, None, self.frozen_since)
    def fps(self, window_sec, now=None):
        return None
    def drain_events(self):
        return []


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


def build_worker(monkeypatch, tmp_path, valid_media, *, total=4, segment=2,
                 gate=True, gate_reason="render_timeout", uid="900001", expected_uid="900001",
                 allow_unconfirmed=False, crashes=(), broken_segments=(), watchdog_factory=PassiveWatchdog):
    """crashes 为采集会话序号（从 1 起），对应会话启动 0.5 秒后采集进程退出；
    broken_segments 为写入损坏内容的分段序号。"""
    import src.worker.controller as cm
    import src.worker.continuous_session as cs
    clock = FakeClock()
    pipelines = []
    launched = []

    class CapturePipe:
        """每 segment 秒产生一个分段文件；停止或退出后全部分段视为封口。"""
        def __init__(self, output_dir, start_index, **kwargs):
            self.output_dir = Path(output_dir)
            self.base = start_index
            self.epoch = len(pipelines) + 1
            self.start_count = self.stop_count = 0
            self.rows = {}
            self.alive = False
            self.exit_code = None
            self.wait_hook = None
            self._proc = SimpleNamespace(pid=12345)
            self._mux = SimpleNamespace(pid=12346)
            pipelines.append(self)
        def start(self):
            self.start_count += 1
            self.alive = True
            self.begin = clock.monotonic()
        def is_recording(self):
            if self.alive and self.epoch in crashes and clock.monotonic() - self.begin >= .5:
                self.alive = False
                self.exit_code = 9
            return self.alive
        def fragments(self):
            if self.alive:
                n = max(1, int((clock.monotonic() - self.begin) // segment) + 1)
                for off in range(n):
                    idx = self.base + off
                    if idx not in self.rows:
                        path = self.output_dir / f'raw_cont_{idx}.mp4'
                        if idx in broken_segments:
                            path.write_bytes(b'bad media')
                        else:
                            shutil.copyfile(valid_media, path)
                        self.rows[idx] = path
                        launched.append(idx)
            latest = max(self.rows, default=0)
            return [Fragment(i, p, not self.alive or i < latest, (i - self.base) * segment,
                             (i - self.base + 1) * segment) for i, p in sorted(self.rows.items())]
        def stop(self, *args, **kwargs):
            if self.alive:
                self.stop_count += 1
                self.alive = False
                self.exit_code = 0
            return self.exit_code == 0
        teardown = stop
        def get_exit_code(self):
            return self.exit_code
        def get_current_file_size_mb(self):
            return clock.monotonic()
        def failure_details(self):
            return f'exit={self.exit_code}'

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
        def inspect_live_surface(self, **kwargs):
            return {'activity': 'com.ss.android.ugc.aweme.LivePlayActivity', 'overlay': None,
                    'ui_available': True, 'observed_at': time.time()}

    monkeypatch.setattr(cm, "time", clock)
    monkeypatch.setattr(cm, "WorkerNavigator", Navigator)
    monkeypatch.setattr(cm, "DependencyManager", lambda: SimpleNamespace(
        ensure_scrcpy=lambda: Path("scrcpy.exe"), get_adb_path=lambda: Path("adb.exe")))
    monkeypatch.setattr(cm, "MuMuClient", lambda **kw: SimpleNamespace(
        launch_player=lambda index: True, connect_adb=lambda index: "synthetic-device",
        optimize_performance_for_recording=lambda *args, **kw: None))
    original_init = cs.ContinuousSession.__init__
    def init(self, *args, **kwargs):
        # 真实封口校验在线程池中执行；每次假睡眠让出少量真实时间供其完成。
        original_init(self, *args, pipeline_factory=CapturePipe, watchdog_factory=watchdog_factory,
                      clock=clock.monotonic, sleeper=lambda sec: (clock.sleep(sec), time.sleep(.003)))
    monkeypatch.setattr(cs.ContinuousSession, '__init__', init)
    stream = io.StringIO()
    config = WorkerConfig(vmindex=321, target="7704666294802123554", target_kind="room_id",
        expected_anchor_uid=expected_uid, allow_unconfirmed_identity=allow_unconfirmed,
        output_dir=tmp_path / "outputs", lock_dir=tmp_path / "locks", total_duration_sec=total,
        segment_duration_sec=segment, heartbeat_interval_sec=1)
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

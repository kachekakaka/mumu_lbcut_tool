"""保留真实 Controller/状态机/采集会话/封口校验，只替换设备与采集进程边界。"""
from concurrent import futures
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
                 allow_unconfirmed=False, crashes=(), broken_segments=(), watchdog_factory=PassiveWatchdog,
                 device_online=lambda: True):
    """crashes 为采集会话序号（从 1 起），对应会话启动 0.5 秒后采集进程退出；
    broken_segments 为写入损坏内容的分段序号。总时长按分装器截止模拟：
    每代采集录满传入的 total_duration_sec 后分装器正常退出。"""
    import src.worker.controller as cm
    import src.worker.continuous_session as cs
    clock = FakeClock()
    pipelines = []
    launched = []
    adb_calls = []
    adb_exe = tmp_path / 'synthetic-adb.exe'
    adb_exe.write_bytes(b'synthetic-command-boundary')
    def adb_command(cmd, **kwargs):
        from src.worker.executor import CommandResult
        adb_calls.append(dict(cmd=cmd, options=kwargs, pipelines_stopped=all(p.stopped for p in pipelines),
                              events=[json.loads(line) for line in stream.getvalue().splitlines()]))
        return CommandResult(exit_code=0, stdout='', stderr='', duration=0)
    monkeypatch.setattr('src.worker.adb_client.run_command', adb_command)

    class CapturePipe:
        """每 segment 秒产生一个分段文件；停止或退出后全部分段视为封口。"""
        def __init__(self, output_dir, start_index, total_duration_sec=0, **kwargs):
            self.output_dir = Path(output_dir)
            self.base = start_index
            self.limit = total_duration_sec
            self.ended_at = None
            self.stopped = False
            self.epoch = len(pipelines) + 1
            self.directory=self.output_dir / f'capture_{self.epoch}'
            self.directory.mkdir()
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
            self.capture_launch_at=clock.time()
            try:
                if self.wait_hook and not self.wait_hook():raise InterruptedError('启动取消')
            except BaseException:
                self.stop()
                raise
            self.first_media_observed_at=clock.time()
        def span(self):
            end = self.ended_at if self.ended_at is not None else clock.monotonic()
            return min(end - self.begin, self.limit) if self.limit else end - self.begin
        def _end(self, code):
            self.alive = False
            self.exit_code = code
            self.ended_at = clock.monotonic()
        def is_recording(self):
            if self.alive and self.epoch in crashes and clock.monotonic() - self.begin >= .5:
                self._end(9)
            return self.alive
        def reached_total_duration(self):
            if self.alive and self.limit and clock.monotonic() - self.begin >= self.limit:
                self._end(0)
            return bool(self.limit) and self.exit_code == 0 and self.span() >= self.limit
        def media_time(self):
            return self.span()
        def get_current_segment_elapsed(self):
            return self.span() % segment
        def fragments(self):
            if self.alive:
                # 截止点上的关键帧不会开出新片：录满时按总时长前一刻计算片数。
                span = min(clock.monotonic() - self.begin, self.limit - 1e-6) if self.limit else clock.monotonic() - self.begin
                n = max(1, int(span // segment) + 1)
                for off in range(n):
                    idx = self.base + off
                    if idx not in self.rows:
                        path = self.directory / f'raw_cont_{idx}.mp4'
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
            if not self.stopped:
                self.stopped = True
                self.stop_count += 1
            if self.alive:
                self._end(0)
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
        def device_online(self):
            return device_online()
        def navigate_route(self, target, web_rid, route, deadline, stop_requested):
            return not stop_requested()
        def wait_live_page(self, **kwargs):
            return gate
        def observe_live_room(self, **kwargs):
            return RoomEvidence(room_id='7704666294802123554', anchor_uid=uid, nickname='synthetic anchor', source='synthetic-device-observation')
        def wait_live_picture(self, **kwargs):
            return gate
        def get_current_focus_activity(self):
            return 'com.ss.android.ugc.aweme.LivePlayActivity'
        def inspect_live_surface(self, **kwargs):
            return {'activity': 'com.ss.android.ugc.aweme.LivePlayActivity', 'overlay': None,
                    'ui_available': True, 'observed_at': time.time()}

    monkeypatch.setattr(cm, "time", clock)
    import src.worker.room_locator as locator
    monkeypatch.setattr(locator, 'time', clock)
    monkeypatch.setattr(cm, "WorkerNavigator", Navigator)
    media_tools = cm.DependencyManager().get_media_tools()
    monkeypatch.setattr(cm, "DependencyManager", lambda: SimpleNamespace(
        ensure_scrcpy=lambda: Path("scrcpy.exe"), get_adb_path=lambda: adb_exe,
        get_media_tools=lambda: media_tools))
    monkeypatch.setattr(cm, "MuMuClient", lambda **kw: SimpleNamespace(
        launch_player=lambda index: True, connect_adb=lambda index: "synthetic-device",
        optimize_performance_for_recording=lambda *args, **kw: None))
    original_init = cs.ContinuousSession.__init__
    def init(self, *args, **kwargs):
        session = self
        def sleeper(sec):
            # 封口校验是线程池里的真实 ffprobe，耗时按真实时间计；先等它完成再推进假时钟，
            # 否则机器繁忙时假时间会先耗尽 24 秒校验期限，误报 validation_deadline_exceeded。
            pending = list(getattr(session, 'pending', {}).values())
            if pending:
                futures.wait(pending, timeout=10)
            clock.sleep(sec)
            time.sleep(.003)
        original_init(self, *args, pipeline_factory=CapturePipe, watchdog_factory=watchdog_factory,
                      clock=clock.monotonic, sleeper=sleeper, device_probe=device_online)
    monkeypatch.setattr(cs.ContinuousSession, '__init__', init)
    stream = io.StringIO()
    config = WorkerConfig(vmindex=321, target="7704666294802123554", target_kind="room_id",
        expected_anchor_uid=expected_uid, allow_unconfirmed_identity=allow_unconfirmed,
        output_dir=tmp_path / "outputs", lock_dir=tmp_path / "locks", total_duration_sec=total,
        segment_duration_sec=segment, heartbeat_interval_sec=1)
    controller = WorkerController(config, WorkerEventEmitter(stream))
    return SimpleNamespace(controller=controller, clock=clock, stream=stream, launched=launched,
        pipelines=pipelines, adb_calls=adb_calls, adb_exe=adb_exe,
        pipeline_type=CapturePipe, navigator_type=Navigator,
        events=lambda: [json.loads(line) for line in stream.getvalue().splitlines()])


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

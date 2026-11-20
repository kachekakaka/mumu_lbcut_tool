"""真实 ffmpeg 解码的录像流分析离线集成测试；不是 MuMu 实机测试。"""
import io
import json
import subprocess
import time
from types import SimpleNamespace

import pytest

from src.worker.continuous_session import ContinuousSession
from src.worker.events import WorkerEventEmitter
from src.worker.state_machine import ActionType
from src.worker.stream_monitor import MonitorView, StreamMonitor
from src.worker.watchdog import StallWatchdog

FFMPEG = None


@pytest.fixture(autouse=True)
def pinned_media_tools(monkeypatch, media_tools):
    monkeypatch.setattr(__name__ + '.FFMPEG', str(media_tools.ffmpeg))


def make_mkv(rate, duration, vf=None, total=None):
    """输出到管道（不可寻址），与 scrcpy 写命名管道时的 MKV 结构一致。"""
    total = total or duration
    cmd = [FFMPEG, '-v', 'error', '-f', 'lavfi', '-i', f'testsrc2=size=360x640:rate={rate}:duration={duration}',
           '-f', 'lavfi', '-i', f'sine=frequency=440:duration={total}']
    if vf:
        cmd += ['-vf', vf]
    cmd += ['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '32', '-g', str(rate * 2),
            '-c:a', 'aac', '-f', 'matroska', 'pipe:1']
    return subprocess.run(cmd, capture_output=True, check=True, timeout=60).stdout


def feed_paced(monitor, data, seconds, on_step=None, chunk=32768):
    steps = max(1, len(data) // chunk + 1)
    start = time.monotonic()
    for k in range(steps):
        monitor.feed(data[k * chunk:(k + 1) * chunk])
        if on_step:
            on_step(time.monotonic() - start)
        delay = start + seconds * (k + 1) / steps - time.monotonic()
        if delay > 0:
            time.sleep(delay)


def wait_for(predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not predicate():
        time.sleep(.05)
    return predicate()


def test_still_picture_with_real_frames_is_static_not_frozen():
    data = make_mkv(30, 10, vf='tpad=stop_mode=clone:stop_duration=30', total=40)
    monitor = StreamMonitor(FFMPEG)
    per_second = {}
    original = monitor._on_frame
    def keep(analyzer, frame):
        original(analyzer, frame)
        if (monitor.frame_seq - 1) % 30 == 0:
            per_second[(monitor.frame_seq - 1) // 30] = monitor.latest_frame
    monitor._on_frame = keep
    monitor.start()
    try:
        feed_paced(monitor, data, 4)
        # 每一帧都解出，且不补帧：静止段仍是编码器输出的真实帧。
        assert wait_for(lambda: monitor.frame_seq >= 1200, 20)
        time.sleep(.3)
        assert monitor.frame_seq == 1200
        assert [e['action'] for e in monitor.drain_events()] == ['analyzer_started']
    finally:
        monitor.stop()

    class Replay:
        second = 0
        def view(self):
            return MonitorView(True, self.second + 1, per_second[self.second], None, None)
    pipe = SimpleNamespace(is_recording=lambda: True, monitor=Replay(), size=0)
    pipe.get_current_file_size_mb = lambda: pipe.size
    nav = SimpleNamespace(get_current_focus_activity=lambda: 'LivePlayActivity')
    actions, motions, static = [], {}, {}
    dog = StallWatchdog(pipe, nav, 'room', generation_provider=lambda: 1,
                        on_post_action=lambda a: actions.append((pipe.monitor.second, a)))
    for second in range(40):
        pipe.monitor.second, pipe.size = second, second + 1
        dog.check_once(float(second))
        motions[second] = dog.snapshot().video_motion
        static[second] = dog.snapshot().picture_static_sec
    assert all(motions[s] > .003 for s in (5, 10))
    # 静止画面每秒仍有真实解码帧：只计静止时长，不投递冻结恢复动作。
    assert actions == []
    assert static[10] == 0 and static[25] >= 10 and static[39] >= 24
    assert dog.snapshot().video_frozen_sec == 0


def test_killed_analyzer_rejoins_from_next_cluster_without_counting_decode_noise(tmp_path):
    data = make_mkv(30, 20)
    log = tmp_path / 'analysis.stderr.log'
    monitor = StreamMonitor(FFMPEG, stderr_path=log)
    monitor.start()
    killed = {}
    def kill_once(elapsed):
        if elapsed >= 3 and not killed:
            killed['pid'] = monitor.analyzer_pid
            killed['frames'] = monitor.frame_seq
            monitor.analyzer.proc.kill()
    try:
        feed_paced(monitor, data, 10, on_step=kill_once)
        time.sleep(1)
        events = monitor.drain_events()
    finally:
        monitor.stop()
    actions = [e['action'] for e in events]
    assert actions.count('analyzer_failed') == 1
    assert actions.count('analyzer_started') == 2
    assert monitor.restarts == 1 and not monitor.unavailable
    assert monitor.frame_seq - killed['frames'] > 200
    assert monitor.analyzer_pid == 0 and killed['pid']


class QualitySession:
    def __init__(self, monitor):
        self.stream = io.StringIO()
        controller = SimpleNamespace(emitter=WorkerEventEmitter(self.stream), current_generation_id=1,
                                     target_room_id='123',target_uid='456',config=SimpleNamespace(allow_unconfirmed_identity=False))
        self.session = ContinuousSession(controller, 'serial', 'scrcpy', 'adb',
                                         SimpleNamespace(ffmpeg='ffmpeg', ffprobe='ffprobe'))
        self.session.pipe = SimpleNamespace(monitor=monitor)
        self.session.ledger[1] = {'epoch': self.session.epoch, 'terminal': False}
        self.session.latest_segment_index = 1

    def flags(self):
        return [e['flag'] for e in map(json.loads, self.stream.getvalue().splitlines())
                if e['event'] == 'segment_quality_warning']

    def close(self):
        self.session.pool.shutdown()
        self.session.probes.shutdown()


@pytest.mark.parametrize('rate,expected', [(8, ['low_frame_rate']), (15, [])])
def test_decoded_frame_rate_drives_low_frame_rate_flag(rate, expected):
    # 时间压缩 10 倍：70 秒录像 7 秒送完，帧率按压缩后的时钟统计。
    data = make_mkv(rate, 70)
    start = time.monotonic()
    monitor = StreamMonitor(FFMPEG, clock=lambda: (time.monotonic() - start) * 10, no_frame_sec=100)
    quality = QualitySession(monitor)
    measured = []
    def step(elapsed):
        quality.session.check_stream_quality()
        fps = monitor.fps(60)
        if fps is not None:
            measured.append(fps)
    monitor.start()
    try:
        feed_paced(monitor, data, 7, on_step=step)
        assert measured
        assert abs(measured[-1] - rate) <= rate * .15
        assert quality.flags() == expected
    finally:
        monitor.stop()
        quality.close()

def test_stuck_analyzer_never_blocks_the_capture_bridge(monkeypatch):
    import sys
    import src.worker.stream_monitor as stream_monitor
    monkeypatch.setattr(stream_monitor, 'build_command',
                        lambda exe: [sys.executable, '-c', 'import time;time.sleep(120)'])
    data = make_mkv(30, 20)
    monitor = StreamMonitor(FFMPEG)
    monitor.start()
    try:
        slowest, total = 0.0, 0
        while total < 20 << 20:
            for k in range(0, len(data), 65536):
                began = time.perf_counter()
                monitor.feed(data[k:k + 65536])
                slowest = max(slowest, time.perf_counter() - began)
                total += len(data[k:k + 65536])
        monitor.tick()
        events = monitor.drain_events()
    finally:
        monitor.stop()
    assert slowest < .05
    assert {'action': 'analyzer_failed', 'reason': 'queue_overflow'} in [
        {k: e[k] for k in ('action', 'reason')} for e in events]

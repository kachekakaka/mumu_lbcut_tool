"""录像流分析的 MKV 结构解析与失效/无帧核实规则；分析进程用替身，时钟可控。"""
import io
import itertools
import json
from types import SimpleNamespace

import pytest

from src.worker.continuous_session import ContinuousSession
from src.worker.events import WorkerEventEmitter
from src.worker.stream_monitor import FRAME_BYTES, MkvClusterTracker, StreamMonitor

CLUSTER = bytes.fromhex('1F43B675')


def size(n):
    return (0x10000000 | n).to_bytes(4, 'big')


UNKNOWN = b'\x01' + b'\xff' * 7


def element(eid, payload):
    return bytes.fromhex(eid) + size(len(payload)) + payload


def synthetic_stream(clusters=8, unknown_clusters=False):
    header = (element('1A45DFA3', b'\x42\x86\x81\x01') + bytes.fromhex('18538067') + UNKNOWN
              + element('1549A966', b'i' * 10) + element('1654AE6B', b't' * 20))
    body, offsets = b'', []
    for i in range(clusters):
        # 负载里故意放入 Cluster 标识字节，结构解析不能误中。
        tag = bytes([i % 256])
        block = element('A3', b'\x81\x00\x00\x80' + CLUSTER * 3 + tag * 50)
        content = element('E7', tag) + block + element('A3', tag * 30)
        if unknown_clusters and i == clusters // 2:
            body += element('1254C367', b'tags')
        offsets.append(len(header) + len(body))
        body += (CLUSTER + UNKNOWN + content) if unknown_clusters else element('1F43B675', content)
    return header, header + body, offsets


@pytest.mark.parametrize('unknown', [False, True])
@pytest.mark.parametrize('chunk', [1, 3, 17, 10**6])
def test_cluster_starts_follow_ebml_structure(unknown, chunk):
    header, data, offsets = synthetic_stream(unknown_clusters=unknown)
    tracker, found = MkvClusterTracker(), []
    for base in range(0, len(data), chunk):
        piece = data[base:base + chunk]
        for offset, prefix in tracker.feed(piece):
            # 起点跨块时，前缀加本块剩余部分必须能还原 Cluster 开头。
            assert prefix + piece[max(0, offset - base):] == data[offset:base + len(piece)]
            found.append(offset)
    assert tracker.error is None
    assert found == offsets
    assert bytes(tracker.header) == header


def test_invalid_stream_marks_tracker_broken_without_raising():
    tracker = MkvClusterTracker()
    assert tracker.feed(b'\x00' * 32) == []
    assert tracker.error
    assert tracker.feed(b'more') == []


class FakeAnalyzer:
    pids = itertools.count(100)

    def __init__(self, monitor):
        self.monitor = monitor
        self.pid = next(self.pids)
        self.failed = None
        self.fed_at = None
        self.bytes_at_fed = 0
        self.frames = 0
        self.first_frame_at = None
        self.started_at = 0.0
        self.accept = True
        self.received = bytearray()
        self.killed = False

    def put(self, payload):
        if not self.accept:
            return False
        self.received += payload
        return True

    def fail(self, reason):
        self.failed = self.failed or reason

    def kill(self):
        self.killed = True

    close = kill

    def frame(self, value=80):
        self.monitor._on_frame(self, bytes([value]) * FRAME_BYTES)


class Rig:
    """每 0.5 秒：送一个 Cluster 的录像字节、当前分析进程按 fps 出帧、监督一次。"""

    def __init__(self, clusters=2000, **kwargs):
        self.now = 0.0
        self.analyzers = []
        self.monitor = StreamMonitor('ffmpeg', clock=lambda: self.now,
                                     analyzer_factory=self.factory, **kwargs)
        self.header, self.data, self.offsets = synthetic_stream(clusters)
        self.offsets.append(len(self.data))
        self.cluster = 0
        with self.monitor._lock:
            self.monitor._launch(self.now, 'initial')

    def factory(self):
        analyzer = FakeAnalyzer(self.monitor)
        self.analyzers.append(analyzer)
        return analyzer

    @property
    def current(self):
        return self.monitor.analyzer

    def run(self, seconds, fps=30, frames=True, bytes_flow=True, failing=False):
        for _ in range(int(seconds * 2)):
            self.now = round(self.now + .5, 3)
            if bytes_flow:
                start, end = self.offsets[self.cluster], self.offsets[self.cluster + 1]
                if self.cluster == 0:
                    start = 0
                self.monitor.feed(self.data[start:end])
                self.cluster += 1
            analyzer = self.current
            if analyzer is not None and failing:
                analyzer.fail('analyzer_exited')
            elif analyzer is not None and frames and analyzer.fed_at is not None:
                for _ in range(int(fps / 2)):
                    analyzer.frame()
            self.monitor.tick(self.now)

    def actions(self):
        return [e['action'] for e in self.monitor.drain_events()]


def test_healthy_stream_reports_fps_only_after_window_is_covered():
    rig = Rig()
    rig.run(5)
    assert rig.monitor.fps(10) is None
    rig.run(10)
    assert rig.monitor.fps(10) == 30
    assert rig.actions() == ['analyzer_started']
    assert bytes(rig.analyzers[0].received[:len(rig.header)]) == rig.header


def test_stuck_analyzer_is_restarted_and_not_treated_as_freeze():
    rig = Rig()
    rig.run(10)
    stuck = rig.current
    stuck.frame = lambda value=80: None
    rig.run(11, frames=True)
    events = rig.actions()
    assert 'verify_no_frames' in events and 'analyzer_stalled' in events
    assert 'no_new_frames_confirmed' not in events
    assert stuck.killed and rig.current is not stuck
    assert rig.monitor.frozen_since is None
    assert rig.monitor.restarts == 1 and not rig.monitor.unavailable
    # 新进程从 Cluster 起点接入，先收到缓存的文件头。
    received = bytes(rig.current.received)
    assert received.startswith(rig.header) and received[len(rig.header):].startswith(CLUSTER)


def test_no_new_frames_with_bytes_flowing_is_confirmed_from_first_missing_frame():
    rig = Rig()
    rig.run(10)
    last_frame = rig.monitor.last_frame_at
    rig.run(20, frames=False)
    assert 'no_new_frames_confirmed' in rig.actions()
    assert rig.monitor.frozen_since == last_frame
    assert rig.monitor.view().frozen_since == last_frame
    assert rig.monitor.restarts == 0
    rig.run(1)
    assert rig.monitor.frozen_since is None


def test_no_frames_and_no_bytes_is_left_to_file_growth_detection():
    rig = Rig()
    rig.run(10)
    rig.run(30, frames=False, bytes_flow=False)
    events = rig.actions()
    assert 'verify_inconclusive' in events and 'no_new_frames_confirmed' not in events
    assert rig.monitor.frozen_since is None and not rig.monitor.unavailable


def test_three_failed_restarts_mark_unavailable_then_retry_every_minute():
    rig = Rig()
    rig.run(10)
    rig.run(2, failing=True)
    assert rig.monitor.unavailable and rig.monitor.analyzer is None
    assert rig.actions().count('analyzer_started') == 4
    assert rig.monitor.view().available is False and rig.monitor.fps(10) is None
    rig.run(55, failing=True)
    assert rig.analyzers.__len__() == 4
    rig.run(10, failing=True)
    assert len(rig.analyzers) == 5 and 'retry_failed' in rig.actions()
    rig.run(70)
    assert not rig.monitor.unavailable and rig.monitor.restarts == 0
    assert 'recovered' in rig.actions()
    rig.run(10)
    assert rig.monitor.fps(10) == 30


def test_queue_overflow_drops_analysis_data_and_rejoins_at_next_cluster():
    rig = Rig()
    rig.run(3)
    first = rig.current
    first.accept = False
    rig.run(2)
    assert first.failed == 'queue_overflow' and first.killed
    received = bytes(rig.current.received)
    assert received.startswith(rig.header) and received[len(rig.header):].startswith(CLUSTER)
    assert rig.monitor.restarts == 1


def test_feed_never_raises_into_the_capture_bridge():
    rig = Rig()
    rig.monitor.tracker.feed = lambda data: 1 / 0
    rig.monitor.feed(b'x' * 10)


class SessionRig:
    def __init__(self, monitor):
        self.stream = io.StringIO()
        controller = SimpleNamespace(emitter=WorkerEventEmitter(self.stream), current_generation_id=1)
        self.session = ContinuousSession(controller, 'serial', 'scrcpy', 'adb')
        self.session.pipe = SimpleNamespace(monitor=monitor)
        self.add_segment(1)

    def add_segment(self, index):
        self.session.ledger[index] = {'epoch': self.session.epoch, 'terminal': False}

    def warnings(self):
        return [(e['segment_index'], e['flag']) for e in map(json.loads, self.stream.getvalue().splitlines())
                if e['event'] == 'segment_quality_warning']

    def close(self):
        self.session.pool.shutdown()
        self.session.probes.shutdown()


@pytest.mark.parametrize('fps,expected', [(8, [(1, 'low_frame_rate')]), (12, []), (15, [])])
def test_low_frame_rate_is_flagged_once_per_episode(fps, expected):
    rig = Rig()
    session = SessionRig(rig.monitor)
    try:
        for _ in range(140):
            rig.run(.5, fps=fps)
            session.session.check_stream_quality()
        assert session.warnings() == expected
    finally:
        session.close()


def test_low_frame_rate_flags_new_segment_and_rearms_after_recovery():
    rig = Rig()
    session = SessionRig(rig.monitor)
    try:
        def run(seconds, fps):
            for _ in range(int(seconds * 2)):
                rig.run(.5, fps=fps)
                session.session.check_stream_quality()
        run(70, 8)
        session.add_segment(2)
        run(5, 8)
        run(90, 30)
        run(70, 8)
        assert session.warnings() == [(1, 'low_frame_rate'), (2, 'low_frame_rate'), (2, 'low_frame_rate')]
    finally:
        session.close()


def test_low_frame_rate_hovering_at_threshold_is_flagged_once():
    rig = Rig()
    session = SessionRig(rig.monitor)
    try:
        def run(seconds, fps):
            for _ in range(int(seconds * 2)):
                rig.run(.5, fps=fps)
                session.session.check_stream_quality()
        run(70, 8)
        # 60 秒平均在 12 上下来回越线，但从未持续达标满 60 秒。
        crossings, below = 0, True
        for _ in range(20):
            for fps in (16, 8):
                for _ in range(8):
                    run(.5, fps)
                    now_below = rig.monitor.fps(60) < 12
                    crossings += now_below != below
                    below = now_below
        assert crossings >= 10
        assert session.warnings() == [(1, 'low_frame_rate')]
        run(150, 30)
        run(70, 8)
        assert session.warnings() == [(1, 'low_frame_rate'), (1, 'low_frame_rate')]
    finally:
        session.close()


def test_visual_check_unavailable_flags_segments_until_recovered():
    rig = Rig()
    session = SessionRig(rig.monitor)
    try:
        rig.run(10)
        rig.run(2, failing=True)
        session.session.check_stream_quality()
        session.add_segment(2)
        session.session.check_stream_quality()
        session.session.check_stream_quality()
        rig.run(70)
        session.add_segment(3)
        session.session.check_stream_quality()
        assert session.warnings() == [(1, 'visual_check_unavailable'), (2, 'visual_check_unavailable')]
        monitor_events = [json.loads(l) for l in session.stream.getvalue().splitlines()
                          if json.loads(l)['event'] == 'stream_monitor']
        assert {'unavailable', 'recovered'} <= {e['action'] for e in monitor_events}
    finally:
        session.close()

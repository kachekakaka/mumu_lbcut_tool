"""通过真实 WorkerController.run 验证录制边界，所有产物只在 tmp_path 内。"""
import pytest
from worker_harness import build_worker, assert_paired


def test_normal_rotations_and_verified_small_media(monkeypatch, tmp_path, valid_media):
    h = build_worker(monkeypatch, tmp_path, valid_media)
    assert h.controller.run() == 0
    terminals = assert_paired(h.events())
    assert h.launched[:2] == [1, 2]
    assert [e['is_final'] for e in terminals] == [False] * (len(terminals) - 1) + [True]
    assert all(e['event'] == 'segment_finished' for e in terminals)
    assert all(e['duration_sec'] > 0 for e in terminals)
    assert h.events()[-1]['event'] == 'worker_stopped'
    assert len(h.pipelines) == 1 and not h.pipelines[0].alive


@pytest.mark.parametrize('reason,code', [('login_blocked', 15), ('navigation_failed', 17),
                                        ('render_timeout', 16), ('device_unavailable', 20)])
def test_preflight_classification(monkeypatch, tmp_path, valid_media, reason, code):
    h = build_worker(monkeypatch, tmp_path, valid_media, gate=False, gate_reason=reason)
    assert h.controller.run() == code
    assert not h.launched
    assert any(e['event'] == 'login_blocked' for e in h.events()) == (code == 15)
    assert h.events()[-1]['exit_code'] == code


def test_two_capture_exits_restart_capture_and_preserve_indices(monkeypatch, tmp_path, valid_media):
    h = build_worker(monkeypatch, tmp_path, valid_media, total=4, segment=30, crashes=(1, 2))
    assert h.controller.run() == 0
    terminals = assert_paired(h.events())
    assert h.launched == [1, 2, 3]
    assert len(h.pipelines) == 3
    assert [e['event'] for e in terminals] == ['segment_aborted', 'segment_aborted', 'segment_finished']
    assert h.events()[-1]['had_segment_errors']


def test_final_validation_failure_never_claims_success(monkeypatch, tmp_path, valid_media):
    h = build_worker(monkeypatch, tmp_path, valid_media, total=1, segment=30, broken_segments=(1,))
    assert h.controller.run() == 30
    terminals = assert_paired(h.events())
    assert terminals[0]['event'] == 'segment_aborted'
    assert not any(e['event'] == 'segment_finished' for e in h.events())
    assert h.events()[-1]['exit_code'] == 30


def test_expected_identity_is_not_filled_from_room_id(monkeypatch, tmp_path, valid_media):
    h = build_worker(monkeypatch, tmp_path, valid_media, uid='999999', expected_uid='900001', allow_unconfirmed=True)
    assert h.controller.run() == 18
    assert not h.launched


def test_strict_identity_without_evidence_is_blocked(monkeypatch, tmp_path, valid_media):
    h = build_worker(monkeypatch, tmp_path, valid_media, uid=None, expected_uid=None)
    assert h.controller.run() == 18
    assert not h.launched


def test_unconfirmed_requires_explicit_allow_and_reports_risk(monkeypatch, tmp_path, valid_media):
    h = build_worker(monkeypatch, tmp_path, valid_media, uid=None, expected_uid=None, allow_unconfirmed=True)
    assert h.controller.run() == 0
    assert_paired(h.events())
    assert all(e['room_identity_status'] == 'unconfirmed' for e in h.events() if e['event'] == 'segment_finished')


def tick_watchdog(holder, monitor_factory):
    from src.worker.watchdog import StallWatchdog
    class TickWatchdog(StallWatchdog):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            h = holder['h']
            # 录制进程边界模拟字节持续增长；视觉信号来自实际 ndarray 差分。
            self.pipeline.get_current_file_size_mb = lambda: h.clock.monotonic() / 10
            self.pipeline.monitor = monitor_factory(h)
        def start(self):
            self._stop.clear()
            holder['h'].clock.on_sleep = lambda now: self.check_once(now)
        def stop(self):
            self._stop.set()
            holder['h'].clock.on_sleep = None
    return TickWatchdog


def test_real_watchdog_freeze_actions_reach_controller_and_budget(monkeypatch, tmp_path, valid_media):
    from worker_harness import FakeStreamMonitor
    holder = {}
    def frozen(h):
        monitor = FakeStreamMonitor()
        monitor.new_frames = False
        monitor.frozen_since = h.clock.monotonic()
        return monitor
    h = build_worker(monkeypatch, tmp_path, valid_media, total=110, segment=1800,
                     watchdog_factory=tick_watchdog(holder, frozen))
    holder['h'] = h
    assert h.controller.run() == 30
    terminals = assert_paired(h.events())
    assert len(h.launched) == 3
    assert h.controller._failure_reason == 'recovery_budget_exhausted'
    assert [e['reason'] for e in h.events() if e['event'] == 'stall_detected'] == ['video_frozen', 'video_frozen']
    assert any(e['event'] == 'segment_aborted' for e in terminals)
    assert all(not p.alive for p in h.pipelines)


def test_static_picture_is_marked_and_relocated_once_per_episode(monkeypatch, tmp_path, valid_media):
    from worker_harness import FakeStreamMonitor
    holder = {}
    class Scripted(FakeStreamMonitor):
        """1000～1150 秒静止，1150～1160 秒画面变化，之后再次静止；每次查询都有新帧。"""
        def __init__(self, h):
            super().__init__()
            self.h = h
        def view(self):
            now = self.h.clock.monotonic()
            self.set_value(80 + int(now) % 7 * 20 if 1150 <= now < 1160 else 80)
            return super().view()
    h = build_worker(monkeypatch, tmp_path, valid_media, total=400, segment=60,
                     watchdog_factory=tick_watchdog(holder, Scripted))
    holder['h'] = h
    assert h.controller.run() == 0
    events = h.events()
    assert_paired(events)
    stalls = [e for e in events if e['event'] == 'stall_detected']
    assert [e['reason'] for e in stalls] == ['static_picture_timeout', 'static_picture_timeout']
    assert [e['action'] for e in stalls] == ['relocate_target'] * 2
    # 第一次重新定位后画面仍静止：继续录制并标记，直到画面恢复变化后才重新计时。
    first, second = (p.begin for p in h.pipelines[1:])
    assert 1120 <= first < 1150 and second >= 1270
    marked = {e['segment_index'] for e in events
              if e['event'] == 'segment_quality_warning' and e['flag'] == 'static_picture'}
    assert len(marked) >= 4
    assert len(h.pipelines) == 3


def test_device_offline_during_recovery_exits_20(monkeypatch, tmp_path, valid_media):
    h = build_worker(monkeypatch, tmp_path, valid_media, total=30, segment=2, crashes=(1,),
                     device_online=lambda: False)
    assert h.controller.run() == 20
    events = h.events()
    assert_paired(events)
    assert len(h.pipelines) == 1
    assert not any(e['event'] == 'stall_detected' for e in events)
    assert events[-1]['exit_code'] == 20 and events[-1]['reason'] == 'device_offline'


def test_total_duration_is_cut_by_muxer_without_tail(monkeypatch, tmp_path, valid_media):
    h = build_worker(monkeypatch, tmp_path, valid_media, total=6, segment=2)
    assert h.controller.run() == 0
    terminals = assert_paired(h.events())
    assert h.launched == [1, 2, 3]
    assert [e['is_final'] for e in terminals] == [False, False, True]
    elapsed = [e['segment_elapsed_sec'] for e in h.events() if e['event'] == 'heartbeat']
    assert max(elapsed) >= 1 and 0 in elapsed[1:]


def test_lost_event_stream_seals_segments_into_journal(monkeypatch, tmp_path, valid_media):
    import json
    h = build_worker(monkeypatch, tmp_path, valid_media, total=0, segment=2)
    journal = tmp_path / 'job.ndjson'
    journal.touch()
    emitter = h.controller.emitter
    emitter.fallback_path = journal
    original = emitter.stream
    class Pipe:
        def write(self, text):
            if h.clock.monotonic() >= 1005:
                raise OSError(22, 'Invalid argument')
            original.write(text)
        def flush(self):
            pass
    emitter.stream = Pipe()
    assert h.controller.run() == 0
    seen, events = set(), []
    for line in original.getvalue().splitlines() + journal.read_text(encoding='utf-8').splitlines():
        event = json.loads(line)
        if event['event_id'] not in seen:
            seen.add(event['event_id']); events.append(event)
    terminals = assert_paired(events)
    assert terminals[-1]['is_final'] and all(e['event'] == 'segment_finished' for e in terminals)
    assert events[-1]['event'] == 'worker_stopped' and events[-1]['reason'] == 'event_stream_lost'
    assert not any(p.alive for p in h.pipelines)


def test_total_duration_counts_media_across_capture_restarts(monkeypatch, tmp_path, valid_media):
    h = build_worker(monkeypatch, tmp_path, valid_media, total=4, segment=30, crashes=(1,))
    assert h.controller.run() == 0
    assert [round(p.limit, 3) for p in h.pipelines] == [4, 3.5]
    assert round(h.pipelines[1].media_time(), 3) == 3.5

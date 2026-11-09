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


def test_real_watchdog_freeze_actions_reach_controller_and_budget(monkeypatch, tmp_path, valid_media):
    from src.worker.watchdog import StallWatchdog
    from worker_harness import FakeStreamMonitor
    holder = {}
    class TickWatchdog(StallWatchdog):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            h = holder['h']
            # 录制进程边界模拟字节持续增长；视觉信号来自实际 ndarray 差分。
            self.pipeline.get_current_file_size_mb = lambda: h.clock.monotonic() / 10
            self.pipeline.monitor = FakeStreamMonitor()
        def start(self):
            self._stop.clear()
            holder['h'].clock.on_sleep = lambda now: self.check_once(now)
        def stop(self):
            self._stop.set()
            holder['h'].clock.on_sleep = None
    h = build_worker(monkeypatch, tmp_path, valid_media, total=110, segment=1800, watchdog_factory=TickWatchdog)
    holder['h'] = h
    assert h.controller.run() == 30
    terminals = assert_paired(h.events())
    assert len(h.launched) <= 3
    assert h.controller._failure_reason == 'recovery_budget_exhausted'
    assert any(e['event'] == 'segment_aborted' for e in terminals)
    assert all(not p.alive for p in h.pipelines)

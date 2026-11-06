"""通过真实 WorkerController.run 验证录制边界，所有产物只在 tmp_path 内。"""
import pytest
from worker_harness import build_worker, assert_paired


def test_normal_rotations_and_verified_small_media(monkeypatch, tmp_path, valid_media):
    h = build_worker(monkeypatch, tmp_path, valid_media)
    assert h.controller.run() == 0
    terminals = assert_paired(h.events())
    assert h.launched == [1, 2]
    assert [e['is_final'] for e in terminals] == [False, True]
    assert all(e['event'] == 'segment_finished' for e in terminals)
    assert all(e['duration_sec'] > 0 for e in terminals)
    assert h.events()[-1]['event'] == 'worker_stopped'
    assert all(not pipe.alive for pipe in h.pipelines)


@pytest.mark.parametrize('reason,code', [('login_blocked', 15), ('navigation_failed', 17),
                                        ('render_timeout', 16), ('device_unavailable', 20)])
def test_preflight_classification(monkeypatch, tmp_path, valid_media, reason, code):
    h = build_worker(monkeypatch, tmp_path, valid_media, gate=False, gate_reason=reason)
    assert h.controller.run() == code
    assert not h.launched
    assert any(e['event'] == 'login_blocked' for e in h.events()) == (code == 15)
    assert h.events()[-1]['exit_code'] == code


def test_two_crashes_in_run_preserve_indices_and_events(monkeypatch, tmp_path, valid_media):
    h = build_worker(monkeypatch, tmp_path, valid_media, total=4, segment=30, crashes=(1, 2))
    assert h.controller.run() == 0
    terminals = assert_paired(h.events())
    assert h.launched == [1, 2, 3]
    assert [e['event'] for e in terminals] == ['segment_aborted', 'segment_aborted', 'segment_finished']
    assert h.events()[-1]['had_segment_errors']


def test_final_validation_failure_never_claims_success(monkeypatch, tmp_path, valid_media):
    h = build_worker(monkeypatch, tmp_path, valid_media, total=1, segment=30, broken_segments=(1,))
    assert h.controller.run() == 30
    terminals = assert_paired(h.events())
    assert terminals[0]['event'] == 'segment_aborted'
    assert not any(e['event'] == 'segment_finished' for e in h.events())
    assert h.events()[-1]['exit_code'] == 30


def test_stop_during_overlap_has_one_final_and_both_closed(monkeypatch, tmp_path, valid_media):
    h = build_worker(monkeypatch, tmp_path, valid_media, total=0, segment=10, overlap=3)
    h.clock.on_sleep = lambda now: h.controller.request_stop() if now >= 1008 else None
    assert h.controller.run() == 0
    terminals = assert_paired(h.events())
    assert h.launched == [1, 2]
    assert sum(e.get('is_final', False) for e in terminals) == 1
    assert terminals[-1]['segment_index'] == 2
    assert all(not p.alive for p in h.pipelines)


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


def test_primary_crashes_during_overlap_preserves_all_terminals(monkeypatch, tmp_path, valid_media):
    h = build_worker(monkeypatch, tmp_path, valid_media, total=12, segment=10, overlap=3)
    injected = [False]
    def crash(now):
        if now >= 1007.5 and h.controller._overlapping_started and not injected[0]:
            injected[0] = True
            h.pipelines[0].alive = False
            h.pipelines[0].exit_code = 9
    h.clock.on_sleep = crash
    assert h.controller.run() == 0
    terminals = assert_paired(h.events())
    assert h.launched == [1, 2, 3]
    assert [e['event'] for e in terminals] == ['segment_aborted', 'segment_aborted', 'segment_finished']
    assert not any(p.alive for p in h.pipelines)


def test_secondary_failure_falls_back_without_reusing_index(monkeypatch, tmp_path, valid_media):
    h = build_worker(monkeypatch, tmp_path, valid_media, total=12, segment=10, overlap=3)
    injected = [False]
    def crash(now):
        if now >= 1007.5 and h.controller._overlapping_started and not injected[0]:
            injected[0] = True
            h.pipelines[1].alive = False
            h.pipelines[1].exit_code = 9
    h.clock.on_sleep = crash
    assert h.controller.run() == 0
    terminals = assert_paired(h.events())
    assert h.launched == [1, 2, 3]
    assert len([e for e in terminals if e['event'] == 'segment_aborted']) == 1
    assert h.controller._overlap_disabled
    assert not any(p.alive for p in h.pipelines)


def test_real_watchdog_freeze_actions_reach_controller_and_budget(monkeypatch, tmp_path, valid_media):
    import numpy as np
    import src.worker.controller as cm
    from src.worker.watchdog import StallWatchdog
    h = build_worker(monkeypatch, tmp_path, valid_media, total=110, segment=1800)
    monkeypatch.setattr(cm.WorkerNavigator, 'get_current_focus_activity',
                        lambda self: 'com.ss.android.ugc.aweme.LivePlayActivity', raising=False)
    monkeypatch.setattr(cm.WorkerNavigator, '_capture_frame_array',
                        lambda self: np.full((100, 100, 3), 80, dtype=np.uint8), raising=False)
    class TickWatchdog(StallWatchdog):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            # 录制进程边界模拟字节持续增长；视觉信号来自实际 ndarray 差分。
            self.pipeline.get_current_file_size_mb = lambda: h.clock.monotonic() / 10
        def start(self):
            self._stop.clear()
            h.clock.on_sleep = lambda now: self.check_once(now)
        def stop(self):
            self._stop.set()
            h.clock.on_sleep = None
    monkeypatch.setattr(cm, 'StallWatchdog', TickWatchdog)
    assert h.controller.run() == 30
    terminals = assert_paired(h.events())
    assert len(h.launched) <= 3
    assert h.controller._failure_reason == 'recovery_budget_exhausted'
    assert any(e['event'] == 'segment_aborted' for e in terminals)
    assert all(not p.alive for p in h.pipelines)

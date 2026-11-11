"""恢复次数只受 5 分钟滚动窗口约束，不设整场上限。"""
import io
from types import SimpleNamespace

from src.worker.controller import WorkerController, WorkerConfig
from src.worker.events import WorkerEventEmitter


def make_controller(tmp_path):
    return WorkerController(WorkerConfig(0, '123', tmp_path), WorkerEventEmitter(io.StringIO()))


def test_rolling_window_blocks_third_immediate_recovery(tmp_path):
    ctrl = make_controller(tmp_path)
    assert ctrl._consume_recovery_budget()
    assert ctrl._consume_recovery_budget()
    assert not ctrl._consume_recovery_budget()
    assert ctrl._failure_reason == 'recovery_budget_exhausted'
    assert ctrl.stop_requested


def test_no_session_wide_limit_across_windows(tmp_path, monkeypatch):
    ctrl = make_controller(tmp_path)
    value = [1000.0]
    monkeypatch.setattr('src.worker.controller.time', SimpleNamespace(
        monotonic=lambda: value[0], time=lambda: 1800000000 + value[0]))
    for _ in range(10):
        value[0] += 301
        assert ctrl._consume_recovery_budget()
    assert not ctrl._fatal_error and not ctrl.stop_requested
    value[0] += 10
    assert ctrl._consume_recovery_budget()
    value[0] += 10
    assert not ctrl._consume_recovery_budget()
    assert ctrl._fatal_error

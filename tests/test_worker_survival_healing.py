"""恢复次数同时受滚动窗口和任务全局预算约束。"""
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
    assert ctrl._total_crash_count == 2
    assert ctrl._failure_reason == 'recovery_budget_exhausted'
    assert ctrl.stop_requested


def test_global_budget_persists_across_windows(tmp_path, monkeypatch):
    ctrl = make_controller(tmp_path)
    value = [1000.0]
    monkeypatch.setattr('src.worker.controller.time', SimpleNamespace(
        monotonic=lambda: value[0], time=lambda: 1800000000 + value[0]))
    for _ in range(3):
        value[0] += 301
        assert ctrl._consume_recovery_budget()
    value[0] += 301
    assert not ctrl._consume_recovery_budget()
    assert ctrl._total_crash_count == 3
    assert ctrl._fatal_error

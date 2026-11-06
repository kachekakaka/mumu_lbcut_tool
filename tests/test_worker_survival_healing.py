"""恢复次数同时受滚动窗口和任务全局预算约束。"""
from unittest.mock import Mock
from src.worker.controller import WorkerController, WorkerConfig
from src.worker.events import WorkerEventEmitter
from src.worker.state_machine import WorkerState
import io


def make_controller(tmp_path):
    ctrl = WorkerController(WorkerConfig(0, '123', tmp_path), WorkerEventEmitter(io.StringIO()))
    pipe = Mock()
    pipe.is_recording.return_value = False
    pipe.get_exit_code.return_value = 9
    pipe.current_file_path = tmp_path / 'missing.mp4'
    pipe.get_current_file_size_mb.return_value = 0.0
    pipe.start_segment.side_effect = lambda idx: tmp_path / f'seg_{idx}.mp4'
    ctrl._pipeline = pipe
    ctrl.state_machine.transition_to(WorkerState.RECORDING)
    return ctrl


def test_survival_probe_dispatches_actual_exit(tmp_path):
    ctrl = make_controller(tmp_path)
    heal = Mock(return_value=True)
    ctrl._handle_pipeline_crash = heal
    assert ctrl._check_pipeline_survival(segment_idx=1)
    heal.assert_called_once_with(segment_idx=1, exit_code=9)


def test_rolling_window_blocks_third_immediate_restart(tmp_path):
    ctrl = make_controller(tmp_path)
    assert ctrl._handle_pipeline_crash(segment_idx=1, exit_code=9)
    assert ctrl._handle_pipeline_crash(exit_code=9)
    assert not ctrl._handle_pipeline_crash(exit_code=9)
    assert ctrl._total_crash_count == 2
    assert ctrl._failure_reason == 'recovery_budget_exhausted'
    assert ctrl.stop_requested


def test_global_budget_persists_across_windows(tmp_path, monkeypatch):
    from types import SimpleNamespace
    ctrl = make_controller(tmp_path)
    value = [1000.0]
    monkeypatch.setattr('src.worker.controller.time', SimpleNamespace(
        monotonic=lambda: value[0], time=lambda: 1800000000 + value[0]))
    for index in range(3):
        value[0] += 301
        assert ctrl._handle_pipeline_crash(segment_idx=1 if index == 0 else None, exit_code=9)
    value[0] += 301
    assert not ctrl._handle_pipeline_crash(exit_code=9)
    assert ctrl._total_crash_count == 3
    assert ctrl._fatal_error

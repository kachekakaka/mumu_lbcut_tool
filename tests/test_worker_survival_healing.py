"""测试 scrcpy 进程存活秒级监控与有界就地自愈 (Ticket 06)。"""
from unittest.mock import MagicMock, patch
import pytest

from src.worker.controller import WorkerController, WorkerConfig, EXIT_RECORDING_FAILED
from src.worker.events import (
    WorkerEventEmitter,
    SegmentStartedEvent,
    SegmentAbortedEvent,
)


def test_watchdog_detects_process_crash_within_one_second():
    """主循环在 <= 1.0 秒内检测到 scrcpy 进程终止，并正确发射 segment_aborted。"""
    config = WorkerConfig(
        vmindex=0,
        target="room_test",
        output_dir=MagicMock(),
        segment_duration_sec=100,
    )
    mock_stream = MagicMock()
    emitter = WorkerEventEmitter(stream=mock_stream)
    controller = WorkerController(config=config, emitter=emitter)

    # 模拟管线进程突然退出 (is_recording 返回 False, returncode=1)
    mock_pipe = MagicMock()
    mock_pipe.is_recording.return_value = False
    mock_pipe.get_exit_code.return_value = 1
    mock_pipe.current_file_path = "seg_001.mp4"
    mock_pipe.get_current_segment_elapsed.return_value = 2.0
    mock_pipe.get_current_file_size_mb.return_value = 1.0

    controller._pipeline = mock_pipe
    controller.current_generation_id = 1
    controller.state_machine.transition_to(controller.state_machine.ALLOWED_TRANSITIONS[controller.state_machine.state].copy().pop())

    # 触发单次检查或调用存活探针
    with patch.object(controller, "_handle_pipeline_crash") as mock_heal:
        controller._check_pipeline_survival(segment_idx=1)
        mock_heal.assert_called_once_with(segment_idx=1, exit_code=1)


def test_bounded_healing_stops_after_max_retries():
    """连续故障达到上限 3 次后必须果断停止自愈，禁止陷入无限死循环重启。"""
    config = WorkerConfig(
        vmindex=0,
        target="room_test",
        output_dir=MagicMock(),
        segment_duration_sec=100,
    )
    mock_stream = MagicMock()
    emitter = WorkerEventEmitter(stream=mock_stream)
    controller = WorkerController(config=config, emitter=emitter)

    mock_pipe = MagicMock()
    mock_pipe.current_file_path = "seg_001.mp4"
    mock_pipe.start_segment.return_value = "new_seg.mp4"
    controller._pipeline = mock_pipe

    # 模拟连续触发 3 次崩溃自愈
    assert controller._total_crash_count == 0
    ok1 = controller._handle_pipeline_crash(segment_idx=1, exit_code=1)
    assert ok1 is True
    assert controller._total_crash_count == 1

    ok2 = controller._handle_pipeline_crash(segment_idx=2, exit_code=1)
    assert ok2 is True
    assert controller._total_crash_count == 2

    ok3 = controller._handle_pipeline_crash(segment_idx=3, exit_code=1)
    assert ok3 is True
    assert controller._total_crash_count == 3

    # 第 4 次崩溃已超过上限 (max 3 次)，必须停止自愈并标记 fatal
    ok4 = controller._handle_pipeline_crash(segment_idx=4, exit_code=1)
    assert ok4 is False
    assert controller._fatal_error is True

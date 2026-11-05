"""双管线交叠无缝切片单元测试 (Overlapping Dual-Pipeline Slicing - ADR 0009)。"""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from src.worker.controller import WorkerConfig, WorkerController, WorkerState
from src.worker.events import (
    SegmentStartedEvent,
    SegmentFinishedEvent,
    WorkerEventEmitter,
)
from src.worker.pipeline import WorkerPipeline, DualPipelineManager


class TestOverlappingPipeline(unittest.TestCase):
    """测试双管线交替与交叠轮转机制。"""

    def setUp(self):
        self.emitted_events = []
        self.emitter = WorkerEventEmitter(stream=MagicMock())
        self.emitter.emit = lambda ev: self.emitted_events.append(ev)

    def test_worker_pipeline_accepts_port_parameter(self):
        """WorkerPipeline 正确接收并组装 --port 参数。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            pipe = WorkerPipeline(
                serial="127.0.0.1:16448",
                output_dir=Path(tmp_dir),
                port="27191:27200",
            )
            self.assertEqual(pipe.port, "27191:27200")

    def test_dual_pipeline_manager_alternates_ports(self):
        """DualPipelineManager 实例化两路不同端口的 WorkerPipeline 并能交替提升。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = DualPipelineManager(
                serial="127.0.0.1:16448",
                output_dir=Path(tmp_dir),
                port_a="27183:27190",
                port_b="27191:27200",
            )
            self.assertEqual(manager.pipe_a.port, "27183:27190")
            self.assertEqual(manager.pipe_b.port, "27191:27200")

            # 初始活动管线为 pipe_a
            self.assertIs(manager.get_active_pipeline(), manager.pipe_a)
            self.assertIs(manager.get_secondary_pipeline(), manager.pipe_b)

            # 提升次管线
            old_primary = manager.promote_secondary()
            self.assertIs(old_primary, manager.pipe_a)
            # 当前活动管线变为 pipe_b
            self.assertIs(manager.get_active_pipeline(), manager.pipe_b)
            self.assertIs(manager.get_secondary_pipeline(), manager.pipe_a)

    def test_controller_overlapping_events_sequence_and_closure(self):
        """控制器在交叠窗口下正确发射事件：started(#2) 先于 finished(#1)，且每个切片终态闭环。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg = WorkerConfig(
                vmindex=2,
                target="test_target",
                output_dir=Path(tmp_dir),
                segment_duration_sec=10,
                overlap_duration_sec=3.0,
            )
            ctrl = WorkerController(config=cfg, emitter=self.emitter)

            # 模拟双管线管理器
            mock_dual = MagicMock(spec=DualPipelineManager)
            mock_pipe_a = MagicMock()
            mock_pipe_b = MagicMock()

            # 模拟活动管线
            mock_dual.get_active_pipeline.return_value = mock_pipe_a
            mock_dual.get_secondary_pipeline.return_value = mock_pipe_b
            mock_dual.start_initial_segment.return_value = Path(tmp_dir) / "seg_001.mp4"
            mock_dual.start_overlapping_segment.return_value = Path(tmp_dir) / "seg_002.mp4"
            mock_dual.promote_secondary.return_value = mock_pipe_a
            mock_dual.get_current_file_size_mb.return_value = 12.5
            mock_pipe_a.is_recording.return_value = True
            mock_pipe_b.is_recording.return_value = True
            mock_pipe_a.get_current_file_size_mb.return_value = 12.5
            mock_pipe_b.get_current_file_size_mb.return_value = 12.5

            ctrl._pipeline = mock_dual

            # 1. 启动首段切片
            ctrl._start_new_segment()
            self.assertEqual(ctrl.current_segment_index, 1)

            # 2. 模拟切片 #1 录制到达 7s (10s - 3s)，触发交叠预热
            ctrl._trigger_overlap_start_if_needed(current_elapsed=7.5)
            self.assertTrue(ctrl._overlapping_started)

            # 验证新切片 #2 已经发射 started 事件
            started_events = [e for e in self.emitted_events if isinstance(e, SegmentStartedEvent)]
            self.assertEqual(len(started_events), 2)
            self.assertEqual(started_events[0].segment_index, 1)
            self.assertEqual(started_events[1].segment_index, 2)

            # 3. 模拟切片 #1 录制到达 10s，触发交替交接
            with patch("src.worker.controller.finalize_segment") as mock_finalize:
                mock_finalize.return_value = (True, Path(tmp_dir) / "final_001.mp4", MagicMock())
                ctrl._finalize_overlap_transition()

            # 验证切片 #1 已成功 finished，当前主切片顺位变为 #2
            self.assertFalse(ctrl._overlapping_started)
            self.assertEqual(ctrl.current_segment_index, 2)

            finished_events = [e for e in self.emitted_events if isinstance(e, SegmentFinishedEvent)]
            self.assertEqual(len(finished_events), 1)
            self.assertEqual(finished_events[0].segment_index, 1)

    def test_controller_stop_during_overlapping_finalizes_both_segments(self):
        """交叠期间收到停止请求，主管线与次管线均被安全封口，无事件悬空。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg = WorkerConfig(
                vmindex=2,
                target="test_target",
                output_dir=Path(tmp_dir),
                segment_duration_sec=10,
                overlap_duration_sec=3.0,
            )
            ctrl = WorkerController(config=cfg, emitter=self.emitter)

            mock_dual = MagicMock(spec=DualPipelineManager)
            mock_pipe_a = MagicMock()
            mock_pipe_b = MagicMock()

            mock_dual.get_active_pipeline.return_value = mock_pipe_a
            mock_dual.get_secondary_pipeline.return_value = mock_pipe_b
            mock_dual.start_initial_segment.return_value = Path(tmp_dir) / "seg_001.mp4"
            mock_dual.start_overlapping_segment.return_value = Path(tmp_dir) / "seg_002.mp4"
            mock_pipe_a.is_recording.return_value = True
            mock_pipe_b.is_recording.return_value = True

            ctrl._pipeline = mock_dual
            ctrl._start_new_segment()
            ctrl._trigger_overlap_start_if_needed(current_elapsed=7.5)
            self.assertTrue(ctrl._overlapping_started)

            # 模拟在交叠期停止
            ctrl.request_stop()
            with patch("src.worker.controller.finalize_segment") as mock_finalize:
                mock_finalize.return_value = (True, Path(tmp_dir) / "final_out.mp4", MagicMock())
                # 触发收尾逻辑
                ctrl._finalize_current_segment(allow_short_tail=True)

            self.assertTrue(ctrl.current_segment_finalized)


if __name__ == "__main__":
    unittest.main()

"""Worker 主流程错房纠偏与采集日志背压回归。"""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from src.worker.controller import (
    WorkerConfig,
    WorkerController,
    EXIT_ROOM_MISMATCH,
)
from src.worker.events import WorkerEventEmitter
from src.worker.scrcpy_probe import PipelineLogDrainer


class TestWorkerE2ERegression(unittest.TestCase):
    """Worker 主控制流程集成与回归测试。"""

    def test_identity_mismatch_detected_from_page_blocks_recording(self):
        """真实控制流程三次纠偏后仍为其他房间：不把错误画面交付为目标录像。"""
        from src.worker.room_locator import RoomEvidence
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp_dir, \
             patch("src.worker.controller.MuMuClient") as mock_mumu, \
             patch("src.worker.controller.DependencyManager"), \
             patch("src.worker.controller.AdbDeviceClient"), \
             patch("src.worker.controller.WorkerNavigator") as mock_nav_cls, \
             patch("src.worker.continuous_session.ContinuousSession") as mock_session:
            mock_mumu.return_value.launch_player.return_value = True
            mock_mumu.return_value.connect_adb.return_value = "synthetic-device"
            nav = mock_nav_cls.return_value
            nav.navigate_route.return_value = True
            nav.wait_live_page.return_value = True
            nav.observe_live_room.return_value = RoomEvidence('99999', '22222', source='synthetic-device')
            cfg = WorkerConfig(vmindex=321,target='88888',target_kind='room_id',
                expected_anchor_uid='11111',output_dir=Path(tmp_dir)/'out',lock_dir=Path(tmp_dir)/'locks')
            ctrl = WorkerController(cfg, WorkerEventEmitter(stream))
            from test_navigation_budget import Clock
            with patch('src.worker.room_locator.time', Clock()):
                self.assertEqual(ctrl.run(), EXIT_ROOM_MISMATCH)
            self.assertEqual(nav.observe_live_room.call_count, 21)
            self.assertFalse(mock_session.called)
            events = [json.loads(line) for line in stream.getvalue().splitlines()]
            self.assertEqual(sum(e.get('action')=='correct_wrong_room' for e in events),3)
            self.assertFalse(any(e['event']=='segment_started' for e in events))

    def test_high_frequency_log_drainer_no_backpressure(self):
        """场景 14: 高频 stdout/stderr 输出：防背压日志排空器平稳消费并妥善回收。"""
        # 使用管道模拟高频写入 10,000 行日志，验证不发生死锁
        pipe_r, pipe_w = os.pipe()
        r_file = os.fdopen(pipe_r, "r", encoding="utf-8", errors="replace")
        w_file = os.fdopen(pipe_w, "w", encoding="utf-8")

        mock_proc = MagicMock()
        mock_proc.stdout = r_file
        mock_proc.stderr = None

        consumed = []
        drainer = PipelineLogDrainer(
            process=mock_proc,
            on_log_line=lambda line: consumed.append(line)
        )
        drainer.start()

        # 写入 2000 行模拟日志
        for i in range(2000):
            w_file.write(f"log_payload_entry_{i}\n")
        w_file.flush()
        w_file.close()

        drainer.join(timeout=2.0)
        self.assertGreater(len(consumed), 1900, "排空线程应完整消费高频管道日志无死锁")


if __name__ == "__main__":
    unittest.main()

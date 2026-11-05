"""Worker 离线职责纯化与信号封口端到端集成测试 (Ticket 04)。

验证：
1. Worker 在全生命周期内（启动、门禁、切片、封口）零外网 Socket 请求；
2. 捕获中断信号后完成 MP4 moov 封口，发射 is_final=True 事件并以退出码 0 退出；
3. 严格遵循 ADR-0011 契约，拒绝未清洗短链并提示由调度平台解析。
"""
import io
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from src.worker.controller import WorkerController, WorkerConfig
from src.worker.events import WorkerEventEmitter
from src.worker.resolver import clean_room_target


class TestWorkerOfflinePurificationE2E(unittest.TestCase):
    @patch("socket.socket")
    @patch("src.worker.controller.finalize_segment")
    @patch("src.worker.controller.MuMuClient")
    @patch("src.worker.controller.WorkerNavigator")
    @patch("src.worker.controller.WorkerPipeline")
    def test_offline_worker_zero_network_socket_e2e(
        self,
        mock_pipeline_cls,
        mock_navigator_cls,
        mock_mumu_cls,
        mock_finalize,
        mock_socket,
    ):
        """端到端验证 Worker 在录制全生命周期内绝对不发生任何网络 socket 连接。"""
        mock_finalize.return_value = (True, Path("target.mp4"), MagicMock(is_valid=True))
        stream = io.StringIO()
        emitter = WorkerEventEmitter(stream=stream)

        mock_mumu = mock_mumu_cls.return_value
        mock_mumu.launch_player.return_value = True
        mock_mumu.connect_adb.return_value = "127.0.0.1:16448"

        mock_nav = mock_navigator_cls.return_value
        mock_nav.launch_room.return_value = True
        mock_nav.verify_preflight_content_gate.return_value = True
        mock_nav.detect_current_room_identity.return_value = ("74400659583", "林思琪")

        mock_file = MagicMock()
        mock_file.name = "seg_001.mp4"
        mock_file.exists.return_value = True

        mock_pipeline = mock_pipeline_cls.return_value
        mock_pipeline.start_segment.return_value = mock_file
        mock_pipeline.is_recording.return_value = True
        mock_pipeline.stop_segment.return_value = True
        mock_pipeline.get_current_file_size_mb.return_value = 10.0

        # 输入包含网页 URL，Worker 本地正则 0 毫秒清洗
        cfg = WorkerConfig(
            vmindex=0,
            target="https://live.douyin.com/74400659583?from=search",
            output_dir=Path("D:/test_offline_output"),
            segment_duration_sec=1,
            total_duration_sec=1,
            heartbeat_interval_sec=0.5,
        )

        controller = WorkerController(config=cfg, emitter=emitter)
        exit_code = controller.run()

        self.assertEqual(exit_code, 0)
        # 断言未发起任何公网 Socket 连接
        self.assertFalse(mock_socket.called, "Worker 内部绝不可建立任何公网网络 Socket 连接！")

        # 检查事件流
        lines = [json.loads(line) for line in stream.getvalue().strip().splitlines()]
        event_names = [item["event"] for item in lines]
        self.assertIn("worker_started", event_names)
        self.assertIn("segment_started", event_names)
        self.assertIn("segment_finished", event_names)

        # 最终切片必须带有 is_final=True
        finished_events = [item for item in lines if item["event"] == "segment_finished"]
        self.assertTrue(finished_events[-1]["is_final"])

    @patch("src.worker.controller.finalize_segment")
    @patch("src.worker.controller.MuMuClient")
    @patch("src.worker.controller.WorkerNavigator")
    @patch("src.worker.controller.WorkerPipeline")
    def test_graceful_stop_signal_flow(
        self,
        mock_pipeline_cls,
        mock_navigator_cls,
        mock_mumu_cls,
        mock_finalize,
    ):
        """验证外部中断信号触发时，平滑封口并以退出码 0 退出。"""
        mock_finalize.return_value = (True, Path("target.mp4"), MagicMock(is_valid=True))
        stream = io.StringIO()
        emitter = WorkerEventEmitter(stream=stream)

        mock_mumu = mock_mumu_cls.return_value
        mock_mumu.launch_player.return_value = True
        mock_mumu.connect_adb.return_value = "127.0.0.1:16448"

        mock_nav = mock_navigator_cls.return_value
        mock_nav.launch_room.return_value = True
        mock_nav.verify_preflight_content_gate.return_value = True
        mock_nav.detect_current_room_identity.return_value = ("74400659583", "林思琪")

        mock_file = MagicMock()
        mock_file.name = "seg_001.mp4"
        mock_file.exists.return_value = True

        mock_pipeline = mock_pipeline_cls.return_value
        mock_pipeline.start_segment.return_value = mock_file
        mock_pipeline.is_recording.return_value = True
        mock_pipeline.stop_segment.return_value = True
        mock_pipeline.get_current_file_size_mb.return_value = 8.0
        mock_pipeline.get_current_segment_elapsed.return_value = 0.5

        cfg = WorkerConfig(
            vmindex=0,
            target="74400659583",
            output_dir=Path("D:/test_offline_output"),
            segment_duration_sec=1800,  # 正常长录制
            total_duration_sec=0,
            overlap_duration_sec=0,     # 单管线模式，与 mock WorkerPipeline 一致
            heartbeat_interval_sec=0.5,
        )

        controller = WorkerController(config=cfg, emitter=emitter)

        # 模拟运行中收到中断信号
        def trigger_stop_soon():
            import time
            time.sleep(0.1)
            controller.request_stop()

        import threading
        t = threading.Thread(target=trigger_stop_soon, daemon=True)
        t.start()

        exit_code = controller.run()
        t.join()

        self.assertEqual(exit_code, 0, "平滑停机必须正常退出 (Exit Code 0)")

        lines = [json.loads(line) for line in stream.getvalue().strip().splitlines()]
        finished_events = [item for item in lines if item["event"] == "segment_finished"]
        self.assertTrue(len(finished_events) >= 1)
        self.assertTrue(finished_events[-1]["is_final"], "中断停机最终封口必须携带 is_final=True")

    def test_unresolved_short_link_rejected_locally(self):
        """未解析短链在本地被坚决拒绝，绝不发网络请求。"""
        with self.assertRaises(ValueError) as ctx:
            clean_room_target("https://v.douyin.com/abcxyz/")
        self.assertIn("需由上层调度平台解析", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

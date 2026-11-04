"""Worker 顶层控制器集成测试（最高层测试 Seam）。"""
import io
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from src.worker.controller import WorkerController, WorkerConfig
from src.worker.events import WorkerEventEmitter
from src.worker.resolver import RoomStatus, ResolvedRoom


class TestWorkerController(unittest.TestCase):
    @patch("src.worker.controller.finalize_segment")
    @patch("src.worker.controller.MuMuClient")
    @patch("src.worker.controller.WorkerRoomResolver")
    @patch("src.worker.controller.WorkerNavigator")
    @patch("src.worker.controller.WorkerPipeline")
    def test_worker_run_normal_cycle(
        self,
        mock_pipeline_cls,
        mock_navigator_cls,
        mock_resolver_cls,
        mock_mumu_cls,
        mock_finalize,
    ):
        mock_finalize.return_value = (True, Path("target.mp4"), MagicMock(is_valid=True))
        # 准备 mock
        stream = io.StringIO()
        emitter = WorkerEventEmitter(stream=stream)

        mock_mumu = mock_mumu_cls.return_value
        mock_mumu.launch_player.return_value = True
        mock_mumu.connect_adb.return_value = "127.0.0.1:16448"

        mock_resolver = mock_resolver_cls.return_value
        mock_resolver.resolve.return_value = ResolvedRoom(
            web_rid="737141",
            status=RoomStatus.LIVE,
            room_id="737141",
            title="测试PK",
        )

        mock_nav = mock_navigator_cls.return_value
        mock_nav.launch_room.return_value = True
        mock_nav.verify_preflight_content_gate.return_value = True
        mock_nav.detect_current_room_identity.return_value = ("737141", "测试PK")

        out_dir = Path("D:/test_output")
        mock_file = MagicMock()
        mock_file.name = "test_seg_001.mp4"
        mock_file.exists.return_value = True

        mock_pipeline = mock_pipeline_cls.return_value
        mock_pipeline.start_segment.return_value = mock_file
        mock_pipeline.is_recording.return_value = True
        mock_pipeline.stop_segment.return_value = True
        mock_pipeline.get_current_file_size_mb.return_value = 5.0
        mock_pipeline.get_current_segment_elapsed.return_value = 0.5

        cfg = WorkerConfig(
            vmindex=2,
            target="737141",
            output_dir=Path("D:/test_output"),
            segment_duration_sec=1,  # 1秒快速切片
            total_duration_sec=2,    # 2秒后退出
            heartbeat_interval_sec=0.5,
        )

        controller = WorkerController(config=cfg, emitter=emitter)
        exit_code = controller.run()

        self.assertEqual(exit_code, 0)
        lines = [json.loads(line) for line in stream.getvalue().strip().splitlines()]
        event_names = [item["event"] for item in lines]

        self.assertIn("worker_started", event_names)
        self.assertIn("segment_started", event_names)
        self.assertIn("heartbeat", event_names)
        self.assertIn("segment_finished", event_names)

    @patch("src.worker.controller.MuMuClient")
    @patch("src.worker.controller.WorkerRoomResolver")
    @patch("src.worker.controller.WorkerNavigator")
    @patch("src.worker.controller.WorkerPipeline")
    def test_worker_preflight_gate_blocked_refuses_recording(
        self,
        mock_pipeline_cls,
        mock_navigator_cls,
        mock_resolver_cls,
        mock_mumu_cls,
    ):
        stream = io.StringIO()
        emitter = WorkerEventEmitter(stream=stream)

        mock_mumu = mock_mumu_cls.return_value
        mock_mumu.launch_player.return_value = True
        mock_mumu.connect_adb.return_value = "127.0.0.1:16448"

        mock_resolver = mock_resolver_cls.return_value
        mock_resolver.resolve.return_value = ResolvedRoom(
            web_rid="737141",
            status=RoomStatus.LIVE,
            room_id="737141",
        )

        mock_nav = mock_navigator_cls.return_value
        mock_nav.launch_room.return_value = True
        mock_nav.detect_current_room_identity.return_value = ("737141", "测试PK")
        # 模拟门禁检测失败（如处于登录页且无法绕过）
        mock_nav.verify_preflight_content_gate.return_value = False

        mock_pipeline = mock_pipeline_cls.return_value

        cfg = WorkerConfig(
            vmindex=2,
            target="737141",
            output_dir=Path("D:/test_output"),
        )

        controller = WorkerController(config=cfg, emitter=emitter)
        exit_code = controller.run()

        # 断言：严格返回退出码 15 (EXIT_LOGIN_BLOCKED)
        self.assertEqual(exit_code, 15)
        # 断言：坚决不启动 scrcpy 录制
        self.assertFalse(mock_pipeline.start_segment.called)

        lines = [json.loads(line) for line in stream.getvalue().strip().splitlines()]
        event_names = [item["event"] for item in lines]
        self.assertIn("login_blocked", event_names)
        self.assertNotIn("segment_started", event_names)


if __name__ == "__main__":
    unittest.main()

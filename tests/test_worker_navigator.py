"""Worker 页面导航与 Intent 控制器单元测试。"""
import unittest
from unittest.mock import MagicMock, patch
from src.worker.navigator import WorkerNavigator


class TestWorkerNavigator(unittest.TestCase):
    @patch("src.worker.adb_client.AdbDeviceClient.run_adb")
    def test_launch_live_room(self, mock_run):
        mock_run.side_effect = [
            MagicMock(exit_code=0, stdout="Starting: Intent { act=android.intent.action.VIEW dat=snssdk1128://live/... }", stderr=""),
            MagicMock(exit_code=0, stdout="mCurrentFocus=Window{123 u0 com.ss.android.ugc.aweme/.live.LivePlayActivity}", stderr=""),
            MagicMock(exit_code=0, stdout="mCurrentFocus=Window{123 u0 com.ss.android.ugc.aweme/.live.LivePlayActivity}", stderr=""),
        ]
        nav = WorkerNavigator(serial="127.0.0.1:16448")
        ok = nav.launch_room("737141", wait_sec=0)
        self.assertTrue(ok)

    @patch("src.worker.adb_client.AdbDeviceClient.run_adb")
    def test_refresh_current_room(self, mock_run):
        mock_run.return_value = MagicMock(exit_code=0, stdout="Starting: Intent ...", stderr="")
        nav = WorkerNavigator(serial="127.0.0.1:16448")
        ok = nav.refresh_current_room("737141")
        self.assertTrue(ok)

    @patch("src.worker.navigator.WorkerNavigator._capture_frame_array")
    @patch("src.worker.navigator.WorkerNavigator.get_current_focus_activity")
    def test_preflight_content_gate_success(self, mock_focus, mock_capture):
        import numpy as np
        nav = WorkerNavigator(serial="127.0.0.1:16448")
        mock_focus.return_value = "com.ss.android.ugc.aweme.live.LivePlayActivity"
        
        # 模拟两帧具有动态变化且色彩丰富的真实直播帧
        frame1 = np.full((100, 100, 3), 100, dtype=np.uint8)
        frame2 = np.full((100, 100, 3), 100, dtype=np.uint8)
        frame2[10:30, 10:30, :] = 200  # 像素变化
        mock_capture.side_effect = [frame1, frame2]

        passed = nav.verify_preflight_content_gate(timeout_sec=2.0)
        self.assertTrue(passed)

    @patch("src.worker.adb_client.AdbDeviceClient.run_adb")
    def test_dismiss_center_dialog(self, mock_run):
        mock_run.return_value = MagicMock(exit_code=0, stdout="", stderr="")
        nav = WorkerNavigator(serial="127.0.0.1:16448")
        nav.dismiss_center_dialog()
        # 验证调用了 tap 330 1392 (点击拒绝按钮)
        args_tuple = mock_run.call_args[0]
        self.assertTrue("330" in args_tuple and "1392" in args_tuple)

    @patch("src.worker.navigator.WorkerNavigator.dismiss_center_dialog")
    @patch("src.worker.navigator.WorkerNavigator._capture_frame_array")
    @patch("src.worker.navigator.WorkerNavigator.get_current_focus_activity")
    def test_preflight_content_gate_blocked_by_center_dialog(self, mock_focus, mock_capture, mock_dismiss_center):
        import numpy as np
        nav = WorkerNavigator(serial="127.0.0.1:16448")
        mock_focus.return_value = "com.ss.android.ugc.aweme.splash.SplashActivity"
        
        # 构造居中卡片白底帧（全屏白底不高，但中心卡片区域 [27:76, 11:89] 均为 255）
        frame = np.full((100, 100, 3), 50, dtype=np.uint8)
        frame[27:76, 11:89, :] = 255
        mock_capture.return_value = frame

        passed = nav.verify_preflight_content_gate(timeout_sec=0.5)
        self.assertFalse(passed)
        # 验证触发了 dismiss_center_dialog
        self.assertTrue(mock_dismiss_center.called)


if __name__ == "__main__":
    unittest.main()

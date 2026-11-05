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
    def test_launch_room_with_19digit_internal_id(self, mock_run):
        mock_run.side_effect = [
            MagicMock(exit_code=0, stdout="Starting: Intent ...", stderr=""),
            MagicMock(exit_code=0, stdout="mCurrentFocus=Window{123 u0 com.ss.android.ugc.aweme/.live.LivePlayActivity}", stderr=""),
            MagicMock(exit_code=0, stdout="mCurrentFocus=Window{123 u0 com.ss.android.ugc.aweme/.live.LivePlayActivity}", stderr=""),
        ]
        nav = WorkerNavigator(serial="127.0.0.1:16448")
        internal_room_id = "7704666294802123554"  # 19 位真实内部 ID
        ok = nav.launch_room(internal_room_id, wait_sec=0)
        self.assertTrue(ok)
        
        # 验证调用了 snssdk1128 直达 Intent
        first_call_args = mock_run.call_args_list[0][0]
        expected_uri = f"snssdk1128://live?room_id={internal_room_id}"
        self.assertIn(expected_uri, first_call_args)

    @patch("src.worker.adb_client.AdbDeviceClient.run_adb")
    def test_launch_room_with_short_web_rid_fallback(self, mock_run):
        mock_run.side_effect = [
            MagicMock(exit_code=0, stdout="Starting: Intent ...", stderr=""),
            MagicMock(exit_code=0, stdout="mCurrentFocus=Window{123 u0 com.ss.android.ugc.aweme/.live.LivePlayActivity}", stderr=""),
            MagicMock(exit_code=0, stdout="mCurrentFocus=Window{123 u0 com.ss.android.ugc.aweme/.live.LivePlayActivity}", stderr=""),
        ]
        nav = WorkerNavigator(serial="127.0.0.1:16448")
        short_id = "852703780"  # 展示短号
        ok = nav.launch_room(short_id, wait_sec=0)
        self.assertTrue(ok)
        
        # 验证 fallback 到带包名的 Web Intent
        first_call_args = mock_run.call_args_list[0][0]
        expected_web_uri = f"https://live.douyin.com/{short_id}"
        self.assertIn(expected_web_uri, first_call_args)
        self.assertIn("-p", first_call_args)
        self.assertIn("com.ss.android.ugc.aweme", first_call_args)

    @patch("src.worker.adb_client.AdbDeviceClient.run_adb")
    def test_launch_room_with_full_web_url(self, mock_run):
        mock_run.side_effect = [
            MagicMock(exit_code=0, stdout="Starting: Intent ...", stderr=""),
            MagicMock(exit_code=0, stdout="mCurrentFocus=Window{123 u0 com.ss.android.ugc.aweme/.live.LivePlayActivity}", stderr=""),
            MagicMock(exit_code=0, stdout="mCurrentFocus=Window{123 u0 com.ss.android.ugc.aweme/.live.LivePlayActivity}", stderr=""),
        ]
        nav = WorkerNavigator(serial="127.0.0.1:16448")
        full_url = "https://live.douyin.com/852703780"
        ok = nav.launch_room(full_url, wait_sec=0)
        self.assertTrue(ok)
        
        # 验证没有重复拼接前缀
        first_call_args = mock_run.call_args_list[0][0]
        self.assertIn(full_url, first_call_args)
        self.assertIn("-p", first_call_args)
        self.assertIn("com.ss.android.ugc.aweme", first_call_args)

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


    @patch("src.worker.navigator.WorkerNavigator._capture_frame_array")
    @patch("src.worker.navigator.WorkerNavigator.get_current_focus_activity")
    def test_preflight_content_gate_rejects_main_activity_feed(self, mock_focus, mock_capture):
        import numpy as np
        nav = WorkerNavigator(serial="127.0.0.1:16448")
        # 前台停留在首页推荐短视频 MainActivity
        mock_focus.return_value = "com.ss.android.ugc.aweme.main.MainActivity"
        
        # 即使短视频中画面在动（动态变化）
        frame1 = np.full((100, 100, 3), 100, dtype=np.uint8)
        frame2 = np.full((100, 100, 3), 100, dtype=np.uint8)
        frame2[10:40, 10:40, :] = 220
        mock_capture.side_effect = [frame1, frame2, frame1, frame2]

        passed = nav.verify_preflight_content_gate(timeout_sec=0.8)
        # 坚决拒绝盲录短视频
        self.assertFalse(passed)


if __name__ == "__main__":
    unittest.main()

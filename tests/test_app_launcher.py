"""AppLauncher 模块单元测试。"""
import unittest
from unittest.mock import patch, MagicMock
from src.mvp.app_launcher import AppLauncher


class TestAppLauncher(unittest.TestCase):
    @patch("subprocess.run")
    def test_launch_live_room(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="Starting: Intent ...", stderr=""),
            MagicMock(returncode=0, stdout="mCurrentFocus=Window{123 u0 com.ss.android.ugc.aweme/.live.LivePlayActivity}", stderr=""),
        ]
        launcher = AppLauncher("127.0.0.1:16384")
        success = launcher.launch_live_room("123456789", wait_sec=0)
        self.assertTrue(success)

    @patch("subprocess.run")
    def test_get_current_package(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="mCurrentFocus=Window{123 u0 com.ss.android.ugc.aweme/.live.LivePlayActivity}",
            stderr="",
        )
        launcher = AppLauncher("127.0.0.1:16384")
        pkg = launcher.get_current_focus_package()
        self.assertEqual(pkg, "com.ss.android.ugc.aweme")


if __name__ == "__main__":
    unittest.main()

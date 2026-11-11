"""MuMuClient 模块单元测试。"""
import unittest
from unittest.mock import patch, MagicMock
from src.mvp.mumu_client import MuMuClient


class TestMuMuClient(unittest.TestCase):
    @patch("subprocess.run")
    def test_get_player_info(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"2": {"index": "2", "name": "lb1", "is_android_started": true}}',
            stderr="",
        )
        client = MuMuClient()
        info = client.get_player_info(2)
        self.assertEqual(info["name"], "lb1")
        self.assertTrue(info["is_android_started"])

    @patch("subprocess.run")
    def test_get_all_players(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"2": {"name": "lb1"}, "3": {"name": "lb2"}}',
            stderr="",
        )
        client = MuMuClient()
        players = client.get_all_players()
        self.assertIn("2", players)
        self.assertIn("3", players)

    @patch("subprocess.run")
    def test_get_player_hwnd(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"2": {"main_wnd": "00010B5A"}}',
            stderr="",
        )
        client = MuMuClient()
        with patch("os.name", "posix"):  # 跨平台测试
            hwnd = client.get_player_hwnd(2)
            self.assertEqual(hwnd, int("00010B5A", 16))

    @patch.object(MuMuClient, "hide_player_window")
    @patch.object(MuMuClient, "send_player_to_bottom")
    @patch.object(MuMuClient, "move_player_offscreen")
    @patch.object(MuMuClient, "show_player_window")
    def test_apply_window_display_mode(
        self,
        mock_show,
        mock_offscreen,
        mock_bottom,
        mock_hide,
    ):
        client = MuMuClient()

        # foreground
        client.apply_window_display_mode(2, "foreground")
        mock_show.assert_called_with(2)

        # bottom
        client.apply_window_display_mode(2, "bottom")
        mock_bottom.assert_called_with(2, pos_x=None, pos_y=None)

        client.apply_window_display_mode(2, "bottom", pos_x=50, pos_y=50)
        mock_bottom.assert_called_with(2, pos_x=50, pos_y=50)

        # offscreen
        client.apply_window_display_mode(2, "offscreen", pos_x=-2000, pos_y=100)
        mock_offscreen.assert_called_with(2, pos_x=-2000, pos_y=100)

        # hidden
        client.apply_window_display_mode(2, "hidden")
        mock_hide.assert_called_with(2)

    @patch("subprocess.run")
    def test_optimize_performance_window_modes(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        client = MuMuClient()
        with patch.object(client, "apply_window_display_mode") as mock_apply:
            # 传 window_mode="bottom"
            client.optimize_performance_for_recording(2, window_mode="bottom")
            mock_apply.assert_called_with(2, window_mode="bottom", pos_x=None, pos_y=None)

            # 兼容旧代码 show_window=False
            client.optimize_performance_for_recording(2, show_window=False)
            mock_apply.assert_called_with(2, window_mode="hidden", pos_x=None, pos_y=None)

    def test_launch_retries_while_main_service_not_ready(self):
        client = MuMuClient()
        busy = '{"errcode": -503, "errmsg": "mainnx connect failed"}'
        with patch.object(client, "ensure_main_process_running"), \
             patch.object(client, "_run_manager", side_effect=[busy, busy, '{"errcode": 0}']) as manager, \
             patch.object(client, "is_android_started", side_effect=[False, False, False, False, True]), \
             patch("src.mvp.mumu_client.time.sleep"):
            self.assertTrue(client.launch_player(2))
        self.assertEqual(manager.call_count, 3)
        manager.assert_called_with("control", "-v", "2", "launch")

    def test_launch_gives_up_retrying_after_window(self):
        client = MuMuClient()
        busy = '{"errcode": -503, "errmsg": "mainnx connect failed"}'
        clock = iter(range(0, 10000, 10))
        with patch.object(client, "ensure_main_process_running"), \
             patch.object(client, "_run_manager", return_value=busy) as manager, \
             patch.object(client, "is_android_started", return_value=False), \
             patch("src.mvp.mumu_client.time.sleep"), \
             patch("src.mvp.mumu_client.time.time", side_effect=lambda: next(clock)):
            self.assertFalse(client.launch_player(2, timeout_sec=30))
        self.assertLessEqual(manager.call_count, 6)

    def test_optimize_performance_sets_stable_renderer(self):
        client = MuMuClient()
        with patch.object(client, "_run_manager") as mock_manager, \
             patch.object(client, "mute_player"), patch.object(client, "apply_window_display_mode"):
            client.optimize_performance_for_recording(2, disable_dynamic_fps=False)
        mock_manager.assert_called_once_with("setting", "-v", "2", "-k", "renderer_mode", "-val", "dx")


if __name__ == "__main__":
    unittest.main()

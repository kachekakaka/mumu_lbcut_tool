"""MuMuClient 模块单元测试。"""
import unittest
from unittest.mock import patch, MagicMock, ANY
import subprocess
import threading
from pathlib import Path

import pytest
from src.mvp.mumu_client import MuMuClient


class TestMuMuClient(unittest.TestCase):
    @patch("src.mvp.mumu_client.run_client")
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

    @patch("src.mvp.mumu_client.run_client")
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

    @patch("src.mvp.mumu_client.run_client")
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

    @patch("src.mvp.mumu_client.run_client")
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
        stop = MagicMock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        busy = '{"errcode": -503, "errmsg": "mainnx connect failed"}'
        with patch.object(client, "ensure_main_process_running"), \
             patch.object(client, "_run_manager", side_effect=[busy, busy, '{"errcode": 0}']) as manager, \
             patch.object(client, "is_android_started", side_effect=[False, False, False, False, True]), \
             patch("src.mvp.mumu_client.time.sleep"):
            self.assertTrue(client.launch_player(2, stop_event=stop))
        self.assertEqual(manager.call_count, 3)
        manager.assert_called_with("control", "-v", "2", "launch", timeout=ANY)

    def test_launch_gives_up_retrying_after_window(self):
        client = MuMuClient()
        busy = '{"errcode": -503, "errmsg": "mainnx connect failed"}'
        clock = iter(range(0, 10000, 10))
        stop = MagicMock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        with patch.object(client, "ensure_main_process_running"), \
             patch.object(client, "_run_manager", return_value=busy) as manager, \
             patch.object(client, "is_android_started", return_value=False), \
             patch("src.mvp.mumu_client.time.sleep"), \
             patch("src.mvp.mumu_client.time.monotonic", side_effect=lambda: next(clock)):
            self.assertFalse(client.launch_player(2, timeout_sec=30, stop_event=stop))
        self.assertLessEqual(manager.call_count, 6)

    def test_optimize_performance_sets_stable_renderer(self):
        client = MuMuClient()
        with patch.object(client, "_run_manager") as mock_manager, \
             patch.object(client, "mute_player"), patch.object(client, "apply_window_display_mode"):
            client.optimize_performance_for_recording(2, disable_dynamic_fps=False)
        mock_manager.assert_called_once_with("setting", "-v", "2", "-k", "renderer_mode", "-val", "dx")


@pytest.mark.parametrize('output', ['', 'mainnx connect failed', '[]', '{"errcode":-503}', '{"bad":{"index":"bad"}}'])
def test_instance_discovery_rejects_invalid_snapshot(output, monkeypatch):
    client = MuMuClient.__new__(MuMuClient)
    monkeypatch.setattr(client, '_run_manager', lambda *args, **kwargs: output)
    with pytest.raises(ValueError):
        client.list_available_instances()


def test_discovery_distinguishes_empty_success_and_command_failure(monkeypatch):
    client = MuMuClient.__new__(MuMuClient)
    client.manager_exe = Path('synthetic-manager.exe')
    monkeypatch.setattr('src.mvp.mumu_client.run_client', lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, '{}', ''))
    assert client.list_available_instances() == []
    monkeypatch.setattr('src.mvp.mumu_client.run_client', lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, '{}', 'failed'))
    with pytest.raises(RuntimeError, match='查询失败'):
        client.list_available_instances()


def test_launch_timeout_and_shutdown_cancel_are_bounded(monkeypatch):
    client = MuMuClient.__new__(MuMuClient)
    client.manager_exe = Path('synthetic-manager.exe')
    monkeypatch.setattr(client, 'ensure_main_process_running', lambda **kwargs: True)
    timeouts = []
    def hung_command(args, **kwargs):
        timeouts.append(kwargs['timeout'])
        raise subprocess.TimeoutExpired(args, kwargs['timeout'])
    monkeypatch.setattr('src.mvp.mumu_client.run_client', hung_command)
    assert client.launch_player(1) is False
    assert len(timeouts) == 1 and 0 < timeouts[0] <= 10

    stop = threading.Event()
    stop.set()
    assert client.launch_player(1, stop_event=stop) is False
    assert len(timeouts) == 1  # 已取消时不再运行任何 MuMu 命令。


def test_launch_cancel_during_retry_does_not_send_another_command(monkeypatch):
    client = MuMuClient.__new__(MuMuClient)
    stop = threading.Event()
    monkeypatch.setattr(client, 'ensure_main_process_running', lambda **kwargs: True)
    monkeypatch.setattr(client, 'is_android_started', lambda *args, **kwargs: False)
    commands = []
    def launch(*args, **kwargs):
        commands.append(args)
        stop.set()
        return '{"errcode":-503}'
    monkeypatch.setattr(client, '_run_manager', launch)
    assert client.launch_player(1, stop_event=stop) is False
    assert len(commands) == 1


def test_launch_request_timeout_checks_actual_android_state(monkeypatch):
    client = MuMuClient.__new__(MuMuClient)
    monkeypatch.setattr(client, 'ensure_main_process_running', lambda **kw: True)
    states = iter([False, True])
    monkeypatch.setattr(client, 'is_android_started', lambda *a, **kw: next(states))
    launch = MagicMock(side_effect=subprocess.TimeoutExpired('launch', 5))
    monkeypatch.setattr(client, '_run_manager', launch)
    assert client.launch_player(2)
    assert launch.call_count == 1


@pytest.mark.parametrize('accept_on,expected_elapsed', [(1, 85), (6, 120)])
def test_launch_uses_remaining_budget_for_overall_deadline(monkeypatch, accept_on, expected_elapsed):
    client = MuMuClient.__new__(MuMuClient)
    now = [0.0]
    monkeypatch.setattr('src.mvp.mumu_client.time.monotonic', lambda: now[0])
    def prepare(**kwargs):
        now[0] += 15
        return True
    monkeypatch.setattr(client, 'ensure_main_process_running', prepare)
    stop = MagicMock()
    stop.is_set.return_value = False
    def wait(seconds):
        now[0] += seconds
        return False
    stop.wait.side_effect = wait
    calls = []
    def online(*args, **kwargs):
        if not calls or len(calls) == accept_on:
            now[0] += kwargs['timeout']
        return False
    monkeypatch.setattr(client, 'is_android_started', online)
    def launch(*args, **kwargs):
        calls.append(kwargs['timeout'])
        now[0] += kwargs['timeout']
        return '{"errcode":0}' if len(calls) == accept_on else '{"errcode":-503}'
    monkeypatch.setattr(client, '_run_manager', launch)
    assert not client.launch_player(1, stop_event=stop)
    assert len(calls) == accept_on and all(0 < t <= 10 for t in calls)
    assert now[0] == expected_elapsed  # 慢受理时不能在整体 120 秒之后再等待完整的 Android 时限。


if __name__ == "__main__":
    unittest.main()

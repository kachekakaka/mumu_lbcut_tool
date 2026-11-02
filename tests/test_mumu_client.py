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


if __name__ == "__main__":
    unittest.main()

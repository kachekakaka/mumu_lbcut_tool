"""Worker 离线直播间标识清洗器单元测试 (Ticket 01)。

验证：
1. 纯本地正则清洗（支持纯数字房间号、标准网页 URL）；
2. 彻底剥离 requests，禁止任何公网 HTTP/网络 Socket 行为；
3. 未清洗短链输入时抛出明确异常拦截。
"""
import sys
import unittest
from unittest.mock import patch

import pytest

from src.worker.resolver import (
    clean_room_target,
    OfflineRoomTargetCleaner,
)


class TestOfflineRoomTargetCleaner(unittest.TestCase):
    def test_pure_digits_room_id(self):
        """纯数字房间号直接通过。"""
        self.assertEqual(clean_room_target("74400659583"), "74400659583")
        self.assertEqual(clean_room_target("  67909384752  "), "67909384752")

    def test_standard_live_web_url(self):
        """标准直播间网页 URL 本地正则毫秒级提取。"""
        self.assertEqual(
            clean_room_target("https://live.douyin.com/74400659583"),
            "74400659583"
        )
        self.assertEqual(
            clean_room_target("http://live.douyin.com/74400659583?extra=abc&query=1"),
            "74400659583"
        )

    def test_reject_unresolved_short_link(self):
        """未清洗的短链应被明确拦截，提示由调度平台解析。"""
        with self.assertRaises(ValueError) as ctx:
            clean_room_target("https://v.douyin.com/iANgXXXX/")
        self.assertIn("需由上层调度平台解析", str(ctx.exception))

    def test_reject_empty_or_invalid_target(self):
        """非法或空字符串被拦截。"""
        with self.assertRaises(ValueError):
            clean_room_target("")
        with self.assertRaises(ValueError):
            clean_room_target("   ")

    def test_zero_network_socket_during_cleaning(self):
        """断言清洗过程中绝不导入 requests 亦不发起网络调用。"""
        with patch("socket.socket") as mock_socket:
            result = clean_room_target("https://live.douyin.com/48859132415")
            self.assertEqual(result, "48859132415")
            self.assertFalse(mock_socket.called, "清洗过程绝不应触发任何 Socket 调用")

    def test_module_has_no_requests_dependency(self):
        """断言 src.worker.resolver 自身未导入 requests。"""
        import src.worker.resolver as resolver_mod
        self.assertNotIn("requests", dir(resolver_mod))
        self.assertFalse(hasattr(resolver_mod, "requests"))


if __name__ == "__main__":
    unittest.main()

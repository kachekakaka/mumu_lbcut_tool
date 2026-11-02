"""直播间解析模块单元测试。"""
import unittest
from unittest.mock import patch, MagicMock
from src.mvp.room_resolver import RoomResolver, RoomStatus


class TestRoomResolver(unittest.TestCase):
    def test_extract_web_rid_from_url(self):
        resolver = RoomResolver()
        self.assertEqual(resolver.extract_web_rid("https://live.douyin.com/123456"), "123456")
        self.assertEqual(resolver.extract_web_rid("123456"), "123456")
        self.assertEqual(resolver.extract_web_rid("来看直播 https://live.douyin.com/rourou.004 点击"), "rourou.004")

    @patch("requests.Session.get")
    def test_check_status_mock(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.text = '<html>self.__pace_f.push([1,"\\"status\\":2,\\"id_str\\":\\"7123456789\\",\\"title\\":\\"娱乐PK\\",\\"nickname\\":\\"主播小A\\""])</html>'
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp

        resolver = RoomResolver()
        info = resolver.resolve("https://live.douyin.com/123456")
        self.assertEqual(info.web_rid, "123456")
        self.assertEqual(info.status, RoomStatus.LIVE)
        self.assertEqual(info.room_id, "7123456789")
        self.assertEqual(info.title, "娱乐PK")


if __name__ == "__main__":
    unittest.main()

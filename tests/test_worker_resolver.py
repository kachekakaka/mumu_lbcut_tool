"""Worker 直播间解析器单元测试（基于用户提供的 31 个真实房间样本）。"""
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from src.worker.resolver import WorkerRoomResolver, RoomStatus


class TestWorkerResolver(unittest.TestCase):
    def setUp(self):
        self.fixtures_path = Path(__file__).resolve().parent / "fixtures" / "sample_rooms.json"
        with open(self.fixtures_path, "r", encoding="utf-8") as f:
            self.samples = json.load(f)

    def test_extract_web_rid_from_all_31_samples(self):
        resolver = WorkerRoomResolver()
        for item in self.samples:
            entry = item["entry"]
            url = item["url"]

            # 测试从完整 URL 提取
            parsed_from_url = resolver.extract_web_rid(url)
            self.assertEqual(parsed_from_url, entry, f"URL 解析失败: {url}")

            # 测试从纯标识/文本提取
            parsed_from_raw = resolver.extract_web_rid(f"快来看 {entry} 直播！")
            self.assertEqual(parsed_from_raw, entry, f"文本解析失败: {entry}")

    @patch("requests.Session.get")
    def test_resolve_status(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.text = '<html>self.__pace_f.push([1,"\\"status\\":2,\\"id_str\\":\\"99887766\\",\\"title\\":\\"娱乐PK\\",\\"nickname\\":\\"主播小A\\""])</html>'
        mock_get.return_value = mock_resp

        resolver = WorkerRoomResolver()
        res = resolver.resolve("48859132415")
        self.assertEqual(res.web_rid, "48859132415")
        self.assertEqual(res.status, RoomStatus.LIVE)
        self.assertEqual(res.room_id, "99887766")


if __name__ == "__main__":
    unittest.main()

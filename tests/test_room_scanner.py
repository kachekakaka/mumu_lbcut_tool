"""测试直播间批量状态扫描器 (RoomScanner)。"""
import unittest
from unittest.mock import MagicMock

from src.orchestrator.room_scanner import RoomScanner
from src.mvp.room_resolver import ResolvedRoom, RoomStatus


class TestRoomScanner(unittest.TestCase):
    def test_room_scanner_detects_changes(self):
        mock_resolver = MagicMock()

        # 第一次扫描：room_A 在播，room_B 下播
        mock_resolver.batch_resolve.return_value = {
            "room_A": ResolvedRoom(web_rid="room_A", status=RoomStatus.LIVE, title="直播A"),
            "room_B": ResolvedRoom(web_rid="room_B", status=RoomStatus.ENDED),
        }

        scanner = RoomScanner(resolver=mock_resolver)
        newly_live = []
        newly_ended = []

        res1 = scanner.detect_changes(
            targets=["room_A", "room_B"],
            on_newly_live=lambda r: newly_live.append(r.web_rid),
            on_newly_ended=lambda r: newly_ended.append(r.web_rid),
        )
        self.assertEqual(len(res1.live_rooms), 1)
        self.assertEqual(newly_live, ["room_A"])
        self.assertEqual(newly_ended, [])

        # 第二次扫描：room_A 下播，room_B 开播
        mock_resolver.batch_resolve.return_value = {
            "room_A": ResolvedRoom(web_rid="room_A", status=RoomStatus.ENDED),
            "room_B": ResolvedRoom(web_rid="room_B", status=RoomStatus.LIVE, title="直播B"),
        }
        newly_live.clear()
        newly_ended.clear()

        res2 = scanner.detect_changes(
            targets=["room_A", "room_B"],
            on_newly_live=lambda r: newly_live.append(r.web_rid),
            on_newly_ended=lambda r: newly_ended.append(r.web_rid),
        )
        self.assertEqual(len(res2.live_rooms), 1)
        self.assertEqual(newly_live, ["room_B"])
        self.assertEqual(newly_ended, ["room_A"])


if __name__ == "__main__":
    unittest.main()

"""智能宽容直播间解析与开播检测器。"""
from dataclasses import dataclass
from enum import Enum
from html.parser import HTMLParser
import json
import logging
import re
import requests

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
)

PACE_PUSH = re.compile(r"^\s*self\.__pace_f\.push\((\[.*\])\)\s*;?\s*$", re.DOTALL)


class RoomStatus(Enum):
    UNKNOWN = "unknown"
    LIVE = "live"
    ENDED = "ended"


@dataclass
class ResolvedRoom:
    web_rid: str
    status: RoomStatus
    room_id: str | None = None
    title: str | None = None
    nickname: str | None = None


class _ScriptCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._parts: list[str] | None = None
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "script":
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._parts is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._parts is not None:
            self.scripts.append("".join(self._parts))
            self._parts = None


def _walk(value: object, path: tuple[str, ...] = ()):
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, (*path, str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, (*path, str(index)))


def _pace_payloads(page_html: str):
    collector = _ScriptCollector()
    collector.feed(page_html)
    fragments: list[str] = []
    for script in collector.scripts:
        match = PACE_PUSH.fullmatch(script)
        if match is None:
            continue
        try:
            outer = json.loads(match.group(1))
        except Exception:
            continue
        if not isinstance(outer, list) or len(outer) < 2 or not isinstance(outer[1], str):
            continue
        serialized = outer[1]
        fragments.append(serialized)
        separator = serialized.find(":")
        if separator < 0:
            continue
        try:
            yield json.loads(serialized[separator + 1 :])
        except Exception:
            continue

    for record in "".join(fragments).splitlines():
        _, separator, payload = record.partition(":")
        if not separator:
            continue
        try:
            yield json.loads(payload)
        except Exception:
            continue


class WorkerRoomResolver:
    """负责从用户输入的 URL/短链/文本中提取 web_rid，并检查其当前开播状态。"""

    def __init__(self, timeout: int = 8):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Cookie": "__ac_nonce=067140000000000000000;"
        })

    def extract_web_rid(self, text: str) -> str:
        """从输入的文本或 URL 中提取 web_rid。"""
        text = text.strip()
        m = re.search(r"live\.douyin\.com/([a-zA-Z0-9._~-]+)", text)
        if m:
            return m.group(1).split("?")[0]

        short_m = re.search(r"https?://v\.douyin\.com/[a-zA-Z0-9]+/?", text)
        if short_m:
            try:
                resp = self.session.head(short_m.group(0), allow_redirects=True, timeout=self.timeout)
                target_url = resp.url
                m2 = re.search(r"live\.douyin\.com/([a-zA-Z0-9._~-]+)", target_url)
                if m2:
                    return m2.group(1).split("?")[0]
                m_reflow = re.search(r"reflow/(\d+)", target_url)
                if m_reflow:
                    return m_reflow.group(1)
            except Exception as e:
                logger.warning(f"短链重定向解析失败: {e}")

        # 匹配文本中包含的独立合法 web_rid token
        token_match = re.search(r"[a-zA-Z0-9._~-]+", text)
        if token_match:
            return token_match.group(0)

        return text

    def resolve(self, input_target: str) -> ResolvedRoom:
        """解析目标直播间并查询开播状态。"""
        web_rid = self.extract_web_rid(input_target)
        url = f"https://live.douyin.com/{web_rid}"

        try:
            resp = self.session.get(url, timeout=self.timeout)
            html = resp.text

            room_obj = None
            for payload in _pace_payloads(html):
                for path, val in _walk(payload):
                    if path and path[-1] == "roomInfo" and isinstance(val, dict):
                        candidate_room = val.get("room")
                        if isinstance(candidate_room, dict) and "status" in candidate_room:
                            room_obj = candidate_room
                            break
                if room_obj:
                    break

            if room_obj:
                raw_status = str(room_obj.get("status"))
                status = RoomStatus.LIVE if raw_status == "2" else (RoomStatus.ENDED if raw_status in ("4", "0") else RoomStatus.UNKNOWN)
                room_id = str(room_obj.get("id_str") or room_obj.get("id") or web_rid)
                title = room_obj.get("title")
                owner = room_obj.get("owner") if isinstance(room_obj.get("owner"), dict) else {}
                nickname = owner.get("nickname")
                return ResolvedRoom(
                    web_rid=web_rid,
                    status=status,
                    room_id=room_id,
                    title=title,
                    nickname=nickname
                )

            status = RoomStatus.UNKNOWN
            status_match = re.search(r'\\\"status\\\":\s*(\d+)', html) or re.search(r'"status":\s*(\d+)', html)
            if status_match:
                code = status_match.group(1)
                if code == "2":
                    status = RoomStatus.LIVE
                elif code in ("4", "0"):
                    status = RoomStatus.ENDED

            m_id = re.search(r'\\\"id_str\\\":\s*\\\"(\d+)\\\"', html) or re.search(r'"id_str":\s*"(\d+)"', html)
            room_id = m_id.group(1) if m_id else web_rid

            m_title = re.search(r'\\\"title\\\":\s*\\\"(.*?)\\\"', html) or re.search(r'"title":\s*"(.*?)"', html)
            title = m_title.group(1) if m_title else None

            m_nick = re.search(r'\\\"nickname\\\":\s*\\\"(.*?)\\\"', html) or re.search(r'"nickname":\s*"(.*?)"', html)
            nickname = m_nick.group(1) if m_nick else None

            return ResolvedRoom(web_rid=web_rid, status=status, room_id=room_id, title=title, nickname=nickname)
        except Exception as e:
            logger.error(f"查询直播间 {web_rid} 失败: {e}")
            return ResolvedRoom(web_rid=web_rid, status=RoomStatus.UNKNOWN)

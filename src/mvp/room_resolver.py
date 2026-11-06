"""抖音直播间解析与开播状态检测模块。"""
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
    anchor_uid: str | None = None  # 主播稳定 UID，与场次 room_id 分开


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


class RoomResolver:
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

        if re.fullmatch(r"[a-zA-Z0-9._~-]+", text):
            return text

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

        return text

    def resolve(self, input_target: str) -> ResolvedRoom:
        """解析目标直播间并查询开播状态。"""
        web_rid = self.extract_web_rid(input_target)
        url = f"https://live.douyin.com/{web_rid}"

        try:
            resp = self.session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            html = resp.text

            room_obj = None
            # 优先从 pace_payloads 深度提取真正的 roomInfo.room
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
                    nickname=nickname,
                    anchor_uid=str(owner.get("id_str") or owner.get("id")) if (owner.get("id_str") or owner.get("id")) else None,
                )

            return ResolvedRoom(web_rid=web_rid, status=RoomStatus.UNKNOWN)
        except Exception as e:
            logger.error(f"查询直播间 {web_rid} 失败: {e}")
            return ResolvedRoom(web_rid=web_rid, status=RoomStatus.UNKNOWN)

    def batch_resolve(
        self,
        targets: list[str],
        max_workers: int = 8,
    ) -> dict[str, ResolvedRoom]:
        """高并发批量探测多个直播间的开播状态（供调度平台高频轮询使用）。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        results: dict[str, ResolvedRoom] = {}
        if not targets:
            return results

        with ThreadPoolExecutor(max_workers=min(max_workers, len(targets))) as executor:
            future_to_target = {executor.submit(self.resolve, t): t for t in targets}
            for future in as_completed(future_to_target):
                t = future_to_target[future]
                try:
                    results[t] = future.result()
                except Exception as e:
                    logger.error(f"批量解析房间 {t} 异常: {e}")
                    results[t] = ResolvedRoom(web_rid=t, status=RoomStatus.UNKNOWN)
        return results

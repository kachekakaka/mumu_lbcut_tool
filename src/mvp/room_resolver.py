"""抖音直播间解析与开播状态检测模块。"""
from dataclasses import dataclass
from enum import Enum
import logging
import re
import threading
from urllib.parse import quote, urlencode

import requests

from src.mvp.vendor.abogus import ABogus, BrowserFingerprintGenerator

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
)
LIVE_ORIGIN = "https://live.douyin.com"
ROOM_ENTER_URL = LIVE_ORIGIN + "/webcast/room/web/enter/"
TTWID_URL = "https://ttwid.bytedance.com/ttwid/union/register/"
TTWID_BODY = ('{"region":"cn","aid":1768,"needFid":false,"service":"www.ixigua.com",'
              '"migrate_info":{"ticket":"","source":"node"},"cbUrlProtocol":"https","union":true}')
ROOM_ENDED_CODE = 30003


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


def _room_params(web_rid: str) -> dict[str, object]:
    return dict(aid="6383", app_name="douyin_web", live_id=1, device_platform="web",
                language="zh-CN", cookie_enabled="true", screen_width=1920, screen_height=1080,
                browser_language="zh-CN", browser_platform="Win32", browser_name="Edge",
                browser_version="130.0.0.0", enter_source="", is_need_double_stream="false",
                web_rid=web_rid, room_id_str="")


class RoomResolver:
    """从用户输入的 URL/短链/文本中提取 web_rid，并通过签名接口查询开播状态。

    只使用游客 ttwid，不携带登录态。网页 live.douyin.com/<web_rid> 被风控时会返回验证码页，
    而签名接口在同一网络下仍可用，所以状态只从接口取。
    """

    def __init__(self, timeout: int = 8):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Referer": LIVE_ORIGIN + "/"})
        self._ttwid: str | None = None
        self._ttwid_lock = threading.Lock()

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

    def _guest_ttwid(self, stale: str | None = None) -> str:
        with self._ttwid_lock:
            # 并发线程同时发现旧 ttwid 失效时只重新注册一次。
            if self._ttwid and self._ttwid != stale:
                return self._ttwid
            resp = requests.post(TTWID_URL, data=TTWID_BODY, timeout=self.timeout,
                                 headers={"User-Agent": USER_AGENT,
                                          "Content-Type": "application/json; charset=utf-8"})
            resp.raise_for_status()
            ttwid = resp.cookies.get("ttwid")
            if not ttwid:
                raise RuntimeError(f"游客 ttwid 注册未返回 Cookie（HTTP {resp.status_code}）")
            self._ttwid = ttwid
            return ttwid

    def _enter(self, web_rid: str, ttwid: str) -> requests.Response:
        query = urlencode(list(_room_params(web_rid).items()), quote_via=quote, safe="")
        fingerprint = BrowserFingerprintGenerator.generate_fingerprint("Edge")
        signature = ABogus(fp=fingerprint, user_agent=USER_AGENT).generate_abogus(query)[1]
        url = f"{ROOM_ENTER_URL}?{query}&a_bogus={quote(signature, safe='')}"
        resp = self.session.get(url, timeout=self.timeout, headers={"Cookie": f"ttwid={ttwid}"})
        resp.raise_for_status()
        return resp

    def resolve(self, input_target: str) -> ResolvedRoom:
        """解析目标直播间并查询开播状态。"""
        web_rid = self.extract_web_rid(input_target)
        unknown = ResolvedRoom(web_rid=web_rid, status=RoomStatus.UNKNOWN)
        try:
            ttwid = self._guest_ttwid()
            resp = self._enter(web_rid, ttwid)
            if not resp.content:
                # ttwid 失效时接口返回 200 空正文；换一个再试一次。
                resp = self._enter(web_rid, self._guest_ttwid(stale=ttwid))
            try:
                payload = resp.json()
            except ValueError:
                payload = None
            if not isinstance(payload, dict):
                logger.warning("直播间 %s 状态接口未返回 JSON（HTTP %s，%s 字节），按未知处理",
                               web_rid, resp.status_code, len(resp.content or b""))
                return unknown

            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            code = payload.get("status_code")
            if code == ROOM_ENDED_CODE and data:
                return ResolvedRoom(web_rid=web_rid, status=RoomStatus.ENDED)
            rooms = data.get("data")
            room = rooms[0] if isinstance(rooms, list) and rooms and isinstance(rooms[0], dict) else None
            if code != 0 or room is None:
                logger.warning("直播间 %s 状态接口业务异常（status_code=%r，%s），按未知处理",
                               web_rid, code, payload.get("status_msg") or data.get("prompts") or "无房间数据")
                return unknown

            raw_status = str(room.get("status"))
            status = (RoomStatus.LIVE if raw_status == "2"
                      else RoomStatus.ENDED if raw_status in ("4", "0") else RoomStatus.UNKNOWN)
            # 下播等场景接口可能省略 room.owner，同一房主改在 data.user 给出。
            owner = room.get("owner") if isinstance(room.get("owner"), dict) else data.get("user")
            owner = owner if isinstance(owner, dict) else {}
            anchor_uid = owner.get("id_str") or owner.get("id")
            room_id = room.get("id_str") or room.get("id")
            return ResolvedRoom(
                web_rid=web_rid,
                status=status,
                room_id=str(room_id) if room_id else None,
                title=room.get("title"),
                nickname=owner.get("nickname"),
                anchor_uid=str(anchor_uid) if anchor_uid else None,
            )
        except Exception as e:
            logger.error(f"查询直播间 {web_rid} 失败: {e}")
            return unknown

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

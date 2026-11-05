"""Worker 离线目标标识清洗器 (Ticket 01)。

职责：
1. 纯本地正则清洗房间标识（纯数字房间号、标准直播间网页 URL）；
2. 彻底剥离 requests 与所有公网 HTTP/HTTPS 代码；
3. 严格拦截需要网络 302 重定向的未清洗短链，提示由调度平台解析。
"""
from dataclasses import dataclass
from enum import Enum
import logging
import re

logger = logging.getLogger(__name__)


class RoomStatus(Enum):
    """直播间在播状态枚举。"""
    UNKNOWN = "unknown"
    LIVE = "live"
    ENDED = "ended"


@dataclass
class ResolvedRoom:
    """已清洗的标准直播间标识载荷。"""
    web_rid: str
    status: RoomStatus = RoomStatus.LIVE
    room_id: str | None = None
    title: str | None = None
    nickname: str | None = None


class OfflineRoomTargetCleaner:
    """纯离线直播间目标清洗器。"""

    @staticmethod
    def clean(text: str) -> str:
        """纯离线正则清洗直播间目标标识，提取标准 web_rid。
        
        支持格式：
        - 纯数字房间号：'74400659583'
        - 标准网页 URL：'https://live.douyin.com/74400659583'
        - 带查询参数的网页 URL：'https://live.douyin.com/74400659583?extra=...'
        
        异常情况：
        - 空或仅包含空白字符：抛出 ValueError
        - 包含 v.douyin.com 短链：抛出 ValueError，提示由调度平台解析
        - 无法提取合法标识：抛出 ValueError
        """
        if not text or not text.strip():
            raise ValueError("直播间目标标识不能为空或空白字符")

        cleaned = text.strip()

        # 1. 显式拦截未解析短链 (ADR-0011 契约)
        if "v.douyin.com" in cleaned:
            raise ValueError(
                f"检测到未清洗的短链标识: {cleaned}。遵循 ADR-0011 解耦契约，需由上层调度平台解析为标准房间号后再传入 Worker。"
            )

        # 2. 从 live.douyin.com/xxx 提取
        live_match = re.search(r"live\.douyin\.com/([a-zA-Z0-9._~-]+)", cleaned)
        if live_match:
            return live_match.group(1).split("?")[0]

        # 3. 纯数字或标准 token
        token_match = re.fullmatch(r"[a-zA-Z0-9._~-]+", cleaned)
        if token_match:
            return token_match.group(0)

        # 4. 尝试从混合文本中提取纯数字房间号
        digit_match = re.search(r"\b(\d{8,15})\b", cleaned)
        if digit_match:
            return digit_match.group(1)

        raise ValueError(f"无法从输入中提取有效房间标识: '{cleaned}'，需由上层调度平台解析后传入。")


def clean_room_target(text: str) -> str:
    """纯函数入口：清洗直播间目标标识。"""
    return OfflineRoomTargetCleaner.clean(text)

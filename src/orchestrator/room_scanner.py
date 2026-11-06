"""调度层房间扫描：本轮观测与最后可信状态分离，UNKNOWN 不抹掉 LIVE。"""
from dataclasses import dataclass, field
import logging
import time
from typing import Callable

from src.mvp.room_resolver import RoomResolver, RoomStatus, ResolvedRoom

logger = logging.getLogger(__name__)


@dataclass
class ScanResult:
    timestamp: float
    total_count: int
    live_rooms: list[ResolvedRoom]
    ended_rooms: list[ResolvedRoom]
    unknown_rooms: list[ResolvedRoom]
    # 保留原始目标键，避免短链、网页地址与 web_rid 的索引混淆。
    observations: dict[str, ResolvedRoom] = field(default_factory=dict)


class RoomScanner:
    """只读扫描器；调度器另用持久化状态作下播连续确认和任务幂等核对。"""

    def __init__(self, resolver: RoomResolver | None = None, max_workers: int = 8):
        if max_workers < 1:
            raise ValueError("max_workers 必须大于 0")
        self.resolver = resolver or RoomResolver()
        self.max_workers = max_workers
        self._trusted: dict[str, RoomStatus] = {}
        self._confirmed_at: dict[str, float] = {}

    def scan(self, targets: list[str]) -> ScanResult:
        targets = list(dict.fromkeys(targets))
        start = time.monotonic()
        try:
            resolved = self.resolver.batch_resolve(targets, max_workers=self.max_workers)
        except Exception:
            logger.exception("本轮扫描失败，保留最后可信状态")
            resolved = {}
        observations = {
            t: resolved.get(t) or ResolvedRoom(web_rid=t, status=RoomStatus.UNKNOWN)
            for t in targets
        }
        live, ended, unknown = [], [], []
        for room in observations.values():
            status = getattr(room.status, "value", room.status)
            if status == RoomStatus.LIVE.value:
                live.append(room)
            elif status == RoomStatus.ENDED.value:
                ended.append(room)
            else:
                unknown.append(room)
        logger.info("扫描完成: %s 在播 / %s 下播 / %s 未知，耗时 %.2fs",
                    len(live), len(ended), len(unknown), time.monotonic() - start)
        return ScanResult(time.time(), len(targets), live, ended, unknown, observations)

    def detect_changes(
        self, targets: list[str],
        on_newly_live: Callable[[ResolvedRoom], None] | None = None,
        on_newly_ended: Callable[[ResolvedRoom], None] | None = None,
    ) -> ScanResult:
        snapshot = self.scan(targets)
        for target, room in snapshot.observations.items():
            value = getattr(room.status, "value", room.status)
            if value not in ("live", "ended"):
                continue
            status = RoomStatus(value)
            previous = self._trusted.get(target)
            callback = None
            if status == RoomStatus.LIVE and previous != RoomStatus.LIVE:
                callback = on_newly_live
            elif status == RoomStatus.ENDED and previous == RoomStatus.LIVE:
                callback = on_newly_ended
            try:
                if callback:
                    callback(room)
            except Exception:
                # 未确认交付，下一次可信观测继续通知。调度端仍须幂等处理。
                logger.exception("状态回调失败，保留未交付的状态变更: %s", target)
                continue
            self._trusted[target] = status
            self._confirmed_at[target] = snapshot.timestamp
        return snapshot

    def forget(self, target: str) -> None:
        """显式移除监控，不生成假的下播事件。"""
        self._trusted.pop(target, None)
        self._confirmed_at.pop(target, None)

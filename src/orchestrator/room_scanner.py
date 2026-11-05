"""多直播间批量扫描与开播状态监测器 (RoomScanner)。

职责：
1. 高并发轮询监控目标直播间列表（基于 RoomResolver.batch_resolve）；
2. 识别新开播房间与下播房间，计算状态差异；
3. 为调度器派发开播与停播事件通知。
"""
from dataclasses import dataclass
import logging
from typing import Callable, Optional

from src.mvp.room_resolver import RoomResolver, RoomStatus, ResolvedRoom

logger = logging.getLogger(__name__)


@dataclass
class ScanResult:
    """批量扫描结果快照。"""
    timestamp: float
    total_count: int
    live_rooms: list[ResolvedRoom]
    ended_rooms: list[ResolvedRoom]
    unknown_rooms: list[ResolvedRoom]


class RoomScanner:
    """直播间高并发状态扫描器。"""

    def __init__(
        self,
        resolver: Optional[RoomResolver] = None,
        max_workers: int = 8,
    ) -> None:
        self.resolver = resolver or RoomResolver()
        self.max_workers = max_workers
        self._last_live_targets: set[str] = set()

    def scan(self, targets: list[str]) -> ScanResult:
        """执行一次全量并发扫描并汇总。"""
        import time

        start_t = time.time()
        resolved_map = self.resolver.batch_resolve(targets, max_workers=self.max_workers)

        live = []
        ended = []
        unknown = []

        for target in targets:
            room = resolved_map.get(target)
            if not room:
                continue
            st_val = room.status.value if hasattr(room.status, "value") else str(room.status)
            if room.status == RoomStatus.LIVE or st_val == "live":
                live.append(room)
            elif room.status == RoomStatus.ENDED or st_val == "ended":
                ended.append(room)
            else:
                unknown.append(room)

        logger.info(
            f"扫描完成: 目标 {len(targets)} 个 | 在播 {len(live)} | 下播 {len(ended)} | 未知 {len(unknown)} (耗时 {time.time() - start_t:.2f}s)"
        )
        return ScanResult(
            timestamp=time.time(),
            total_count=len(targets),
            live_rooms=live,
            ended_rooms=ended,
            unknown_rooms=unknown,
        )

    def detect_changes(
        self,
        targets: list[str],
        on_newly_live: Optional[Callable[[ResolvedRoom], None]] = None,
        on_newly_ended: Optional[Callable[[ResolvedRoom], None]] = None,
    ) -> ScanResult:
        """执行扫描并比对与上一次扫描的状态差异，触发状态变更回调。"""
        snapshot = self.scan(targets)
        current_live_targets = {r.web_rid for r in snapshot.live_rooms}

        # 发现新开播
        newly_live_targets = current_live_targets - self._last_live_targets
        for room in snapshot.live_rooms:
            if room.web_rid in newly_live_targets and on_newly_live:
                try:
                    on_newly_live(room)
                except Exception as e:
                    logger.warning(f"新开播回调异常: {e}")

        # 发现新下播
        newly_ended_targets = self._last_live_targets - current_live_targets
        for room in snapshot.ended_rooms:
            if room.web_rid in newly_ended_targets and on_newly_ended:
                try:
                    on_newly_ended(room)
                except Exception as e:
                    logger.warning(f"新下播回调异常: {e}")

        self._last_live_targets = current_live_targets
        return snapshot

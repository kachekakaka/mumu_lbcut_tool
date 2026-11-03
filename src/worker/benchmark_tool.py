"""切片空档与音画同步基准测量工具 (Ticket 13 - F-09 工程决策落地)。"""
from dataclasses import dataclass, field
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("BenchmarkTool")


@dataclass
class SegmentMetadata:
    """切片元数据。"""
    path: Path
    index: int
    duration: float
    start_pts: float
    end_pts: float


@dataclass
class GapBenchmarkReport:
    """切片交接空档与漂移测量报告。"""
    total_segments: int
    gaps_ms: list[float]
    avg_gap_ms: float
    max_gap_ms: float
    warnings: list[str] = field(default_factory=list)


def analyze_segment_gaps(
    segments: list[SegmentMetadata],
    max_allowed_gap_ms: float = 2000.0,
) -> GapBenchmarkReport:
    """分析连续切片之间的交接空档丢失时长 (Gap ms)。

    若空档超过 max_allowed_gap_ms，记录预警信息。
    """
    if len(segments) <= 1:
        return GapBenchmarkReport(
            total_segments=len(segments),
            gaps_ms=[],
            avg_gap_ms=0.0,
            max_gap_ms=0.0,
            warnings=[],
        )

    gaps_ms: list[float] = []
    warnings: list[str] = []

    # 按 index 或 start_pts 排序
    sorted_segs = sorted(segments, key=lambda s: s.index)

    for i in range(len(sorted_segs) - 1):
        prev_seg = sorted_segs[i]
        curr_seg = sorted_segs[i + 1]

        # 空档 = 当前切片起始 PTS - 上一切片结束 PTS
        gap_sec = max(0.0, curr_seg.start_pts - prev_seg.end_pts)
        gap_ms = gap_sec * 1000.0
        gaps_ms.append(gap_ms)

        if gap_ms > max_allowed_gap_ms:
            warn_msg = (
                f"【严重切片断层告警】切片 {curr_seg.index} ({curr_seg.path.name}) 与前一切片 {prev_seg.index} "
                f"交接空档达 {gap_ms:.1f}ms (超过阈值 {max_allowed_gap_ms}ms)！"
            )
            logger.warning(warn_msg)
            warnings.append(warn_msg)

    avg_gap = sum(gaps_ms) / len(gaps_ms) if gaps_ms else 0.0
    max_gap = max(gaps_ms) if gaps_ms else 0.0

    return GapBenchmarkReport(
        total_segments=len(sorted_segs),
        gaps_ms=gaps_ms,
        avg_gap_ms=round(avg_gap, 2),
        max_gap_ms=round(max_gap, 2),
        warnings=warnings,
    )

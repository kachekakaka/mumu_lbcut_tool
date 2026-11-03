"""测试切片空档与音画同步基准测量工具 (Ticket 13)。"""
from pathlib import Path
import pytest

from src.worker.benchmark_tool import (
    SegmentMetadata,
    GapBenchmarkReport,
    analyze_segment_gaps,
)


def test_analyze_segment_gaps_calculates_correct_metrics():
    """基准工具正确计算相邻切片的空档丢失时长 (Gap ms)、均值与最大值。"""
    # 模拟 3 个连续切片：
    # 切片 1: 0.0s ~ 60.0s (end=60.0)
    # 切片 2: start=60.5s ~ 120.5s -> 间隙 gap = 0.5s (500ms)
    # 切片 3: start=121.5s ~ 181.5s -> 间隙 gap = 1.0s (1000ms)
    segments = [
        SegmentMetadata(path=Path("seg_01.mp4"), index=1, duration=60.0, start_pts=0.0, end_pts=60.0),
        SegmentMetadata(path=Path("seg_02.mp4"), index=2, duration=60.0, start_pts=60.5, end_pts=120.5),
        SegmentMetadata(path=Path("seg_03.mp4"), index=3, duration=60.0, start_pts=121.5, end_pts=181.5),
    ]

    report = analyze_segment_gaps(segments, max_allowed_gap_ms=2000.0)
    assert report.total_segments == 3
    assert len(report.gaps_ms) == 2
    assert report.gaps_ms[0] == pytest.approx(500.0)
    assert report.gaps_ms[1] == pytest.approx(1000.0)
    assert report.avg_gap_ms == pytest.approx(750.0)
    assert report.max_gap_ms == pytest.approx(1000.0)
    assert len(report.warnings) == 0


def test_analyze_segment_gaps_flags_warning_when_gap_exceeds_threshold():
    """当切片空档超过设定阈值（如 2000ms）时，报告准确标红预警并记录切片编号。"""
    segments = [
        SegmentMetadata(path=Path("seg_01.mp4"), index=1, duration=60.0, start_pts=0.0, end_pts=60.0),
        SegmentMetadata(path=Path("seg_02.mp4"), index=2, duration=60.0, start_pts=62.5, end_pts=122.5),  # gap = 2500ms > 2000ms
    ]

    report = analyze_segment_gaps(segments, max_allowed_gap_ms=2000.0)
    assert len(report.warnings) == 1
    assert "seg_02.mp4" in report.warnings[0] or "切片 2" in report.warnings[0]

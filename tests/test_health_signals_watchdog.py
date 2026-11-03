"""测试四维解耦健康信号与三环节看门狗判定 (Ticket 10)。"""
import pytest

from src.worker.health_monitor import (
    HealthMonitor,
    HealthSignals,
)


def test_health_monitor_triggers_alert_when_video_frozen_even_with_audio_noise():
    """视频动态持续冻结 15 秒（视频差分=0），即使背景有微弱底噪音频，也必须独立告警画面冻结。"""
    monitor = HealthMonitor(video_freeze_threshold_sec=15.0)

    # 模拟视频差分为 0，音频有轻微活动 (底噪)，进程存活，文件在缓慢写入
    signals = monitor.evaluate_signals(
        video_motion=0.0,
        audio_active=True,  # 存在背景杂音底噪
        process_alive=True,
        file_delta_mb=0.05,
        frozen_duration_sec=16.0,  # 已经冻结 16 秒
    )

    assert signals.is_healthy is False
    assert "video_frozen" in signals.alert_reason or "画面冻结" in signals.alert_reason


def test_health_monitor_healthy_under_normal_streaming():
    """正常动态直播（画面有变化，进程存活，文件增长），稳定判定为 HEALTHY。"""
    monitor = HealthMonitor(video_freeze_threshold_sec=15.0)

    signals = monitor.evaluate_signals(
        video_motion=0.05,  # 5% 像素动态
        audio_active=True,
        process_alive=True,
        file_delta_mb=0.5,
        frozen_duration_sec=0.0,
    )

    assert signals.is_healthy is True
    assert signals.alert_reason == ""

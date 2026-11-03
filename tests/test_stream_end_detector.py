"""测试运行期下播多信号判定与动效容错滤波 (Ticket 11)。"""
import pytest

from src.worker.stream_end_detector import StreamEndDetector


def test_transient_end_banner_filtered_out_without_stopping():
    """短暂弹出包含结束关键字的弹幕或横幅（持续 1.5 秒后消失），滤波窗口判定为偶发干扰，不触发停录。"""
    detector = StreamEndDetector(confirm_window_sec=5.0)

    # 第 0 秒：弹出横幅
    confirmed, reason = detector.feed_observation(screen_shows_ended=True, api_shows_ended=False, current_time=1000.0)
    assert not confirmed

    # 第 1.5 秒：仍有横幅，但未满 5 秒
    confirmed, reason = detector.feed_observation(screen_shows_ended=True, api_shows_ended=False, current_time=1001.5)
    assert not confirmed

    # 第 2.0 秒：横幅消失，画面恢复正常
    confirmed, reason = detector.feed_observation(screen_shows_ended=False, api_shows_ended=False, current_time=1002.0)
    assert not confirmed
    assert detector.is_pending_confirmation is False


def test_real_stream_ended_confirmed_after_full_window():
    """真实下播持续满 5 秒且接口同步确认时，触发确诊停录。"""
    detector = StreamEndDetector(confirm_window_sec=5.0)

    # 持续检测到下播
    detector.feed_observation(screen_shows_ended=True, api_shows_ended=True, current_time=1000.0)
    detector.feed_observation(screen_shows_ended=True, api_shows_ended=True, current_time=1003.0)

    # 第 5.1 秒：满 5 秒确诊
    confirmed, reason = detector.feed_observation(screen_shows_ended=True, api_shows_ended=True, current_time=1005.1)
    assert confirmed is True
    assert reason == "stream_ended_confirmed"

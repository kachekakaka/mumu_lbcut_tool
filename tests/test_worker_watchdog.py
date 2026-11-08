"""真实图像采样与队列动作验证：后台不直接操作导航或管线。"""
from unittest.mock import Mock
import numpy as np

from src.worker.watchdog import StallWatchdog
from src.worker.state_machine import ActionType


def build_dog(*, motion=False):
    pipe = Mock()
    pipe.is_recording.return_value = True
    pipe.get_current_file_size_mb.return_value = 10.0
    nav = Mock()
    nav.get_current_focus_activity.return_value = 'com.ss.android.ugc.aweme.LivePlayActivity'
    nav._capture_frame_array.return_value = np.full((100, 100, 3), 80, dtype=np.uint8)
    actions = []
    dog = StallWatchdog(pipe, nav, 'room', stall_threshold_sec=2,
        segment_recycle_threshold_sec=4, fatal_threshold_sec=9,
        visual_probe_interval_sec=1, generation_provider=lambda: 1,
        on_post_action=actions.append)
    return dog, pipe, nav, actions


def test_stalled_file_posts_refresh_without_background_navigation():
    dog, pipe, nav, actions = build_dog()
    for tick in range(5):
        nav._capture_frame_array.return_value = np.full((100, 100, 3), 50 + tick * 25, dtype=np.uint8)
        dog.check_once(float(tick))
    assert any(a.action_type == ActionType.IN_PLACE_HEAL for a in actions)
    assert all(a.generation_id == 1 for a in actions)
    nav.refresh_current_room.assert_not_called()
    nav.dismiss_login_prompt.assert_not_called()
    pipe.stop_segment.assert_not_called()


def test_freeze_detected_even_when_file_grows():
    dog, pipe, nav, actions = build_dog()
    for tick in range(7):
        pipe.get_current_file_size_mb.return_value = 1 + tick
        dog.check_once(float(tick))
    assert dog.snapshot().video_motion == 0
    assert dog.snapshot().video_frozen_sec >= 2
    assert dog.snapshot().file_stall_sec == 0
    assert any(a.action_type == ActionType.RESTART_SEGMENT and a.reason == 'video_frozen' for a in actions)


def test_login_page_posts_fatal_without_direct_clicks():
    dog, pipe, nav, actions = build_dog()
    nav.get_current_focus_activity.return_value = 'com.ss.android.ugc.aweme.DYLoginActivity'
    for tick in (0., 1.):
        pipe.get_current_file_size_mb.return_value = 1 + tick
        dog.check_once(tick)
    assert [a.action_type for a in actions] == [ActionType.IN_PLACE_HEAL, ActionType.FATAL_STOP]
    assert actions[-1].reason == 'login_blocked'
    nav.dismiss_login_prompt.assert_not_called()
    nav.dismiss_center_dialog.assert_not_called()


def test_generation_changes_while_sampling_are_discarded():
    dog, pipe, nav, actions = build_dog()
    generation = [1]
    dog.generation_provider = lambda: generation[0]
    def frame():
        generation[0] = 2
        return np.zeros((100, 100, 3), dtype=np.uint8)
    nav._capture_frame_array.side_effect = frame
    dog.check_once(1.)
    assert dog.snapshot() is None
    assert actions == []


def test_probe_unavailable_is_not_reported_healthy():
    dog, pipe, nav, actions = build_dog()
    nav._capture_frame_array.return_value = None
    for tick in range(17):
        pipe.get_current_file_size_mb.return_value = tick + 1
        dog.check_once(float(tick))
    assert dog.snapshot().video_motion is None
    assert not any(a.reason == 'visual_probe_unavailable' for a in actions)
    for tick in range(17, 61):
        pipe.get_current_file_size_mb.return_value = tick + 1
        dog.check_once(float(tick))
    assert any(a.reason == 'visual_probe_unavailable' for a in actions)

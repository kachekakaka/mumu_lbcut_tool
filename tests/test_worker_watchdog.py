"""录像流画面采样与队列动作验证：后台不直接操作导航或管线，录制期不截图。"""
from unittest.mock import Mock

from src.worker.watchdog import StallWatchdog
from src.worker.state_machine import ActionType
from worker_harness import FakeStreamMonitor


def build_dog():
    pipe = Mock()
    pipe.is_recording.return_value = True
    pipe.get_current_file_size_mb.return_value = 10.0
    pipe.monitor = FakeStreamMonitor()
    nav = Mock()
    nav.get_current_focus_activity.return_value = 'com.ss.android.ugc.aweme.LivePlayActivity'
    nav._capture_frame_array.side_effect = AssertionError('录制期间不得截图')
    actions = []
    dog = StallWatchdog(pipe, nav, 'room', stall_threshold_sec=2,
        segment_recycle_threshold_sec=4, fatal_threshold_sec=9,
        visual_probe_interval_sec=1, generation_provider=lambda: 1,
        on_post_action=actions.append)
    return dog, pipe, nav, actions


def test_stalled_file_posts_refresh_without_background_navigation():
    dog, pipe, nav, actions = build_dog()
    for tick in range(5):
        pipe.monitor.set_value(50 + tick * 25)
        dog.check_once(float(tick))
    assert any(a.action_type == ActionType.IN_PLACE_HEAL for a in actions)
    assert all(a.generation_id == 1 for a in actions)
    nav.refresh_current_room.assert_not_called()
    pipe.stop_segment.assert_not_called()


def test_confirmed_freeze_detected_even_when_file_grows():
    dog, pipe, nav, actions = build_dog()
    pipe.monitor.new_frames = False
    pipe.monitor.frozen_since = 0.
    for tick in range(7):
        pipe.get_current_file_size_mb.return_value = 1 + tick
        dog.check_once(float(tick))
    assert dog.snapshot().video_motion == 0
    assert dog.snapshot().video_frozen_sec >= 2
    assert dog.snapshot().file_stall_sec == 0
    assert dog.snapshot().picture_static_sec == 0
    assert any(a.action_type == ActionType.RESTART_SEGMENT and a.reason == 'video_frozen' for a in actions)
    nav._capture_frame_array.assert_not_called()


def test_static_picture_with_new_frames_is_timed_without_actions():
    dog, pipe, nav, actions = build_dog()
    for tick in range(200):
        pipe.get_current_file_size_mb.return_value = 1 + tick
        dog.check_once(float(tick))
    snap = dog.snapshot()
    assert snap.video_motion == 0 and snap.video_frozen_sec == 0
    assert snap.picture_static_sec >= 190
    assert actions == []
    pipe.monitor.set_value(200)
    dog.check_once(200.)
    assert dog.snapshot().picture_static_sec == 0 and dog.snapshot().video_motion > .003


def test_freeze_after_static_counts_from_confirmed_missing_frames():
    dog, pipe, nav, actions = build_dog()
    for tick in range(30):
        pipe.get_current_file_size_mb.return_value = 1 + tick
        dog.check_once(float(tick))
    assert dog.snapshot().picture_static_sec > 20 and actions == []
    pipe.monitor.new_frames = False
    pipe.monitor.frozen_since = 29.5
    for tick in range(30, 35):
        pipe.get_current_file_size_mb.return_value = 1 + tick
        dog.check_once(float(tick))
    assert dog.snapshot().picture_static_sec == 0
    assert [a.reason for a in actions] == ['video_frozen', 'video_frozen']


def test_login_page_posts_fatal_without_direct_clicks():
    dog, pipe, nav, actions = build_dog()
    nav.get_current_focus_activity.return_value = 'com.ss.android.ugc.aweme.DYLoginActivity'
    for tick in (0., 1.):
        pipe.get_current_file_size_mb.return_value = 1 + tick
        dog.check_once(tick)
    assert [a.action_type for a in actions] == [ActionType.IN_PLACE_HEAL, ActionType.FATAL_STOP]
    assert actions[-1].reason == 'login_blocked'


def test_generation_changes_while_sampling_are_discarded():
    dog, pipe, nav, actions = build_dog()
    generation = [1]
    dog.generation_provider = lambda: generation[0]
    original = pipe.monitor.view
    def view():
        generation[0] = 2
        return original()
    pipe.monitor.view = view
    dog.check_once(1.)
    assert dog.snapshot() is None
    assert actions == []


def test_visual_check_unavailable_never_stops_recording():
    dog, pipe, nav, actions = build_dog()
    pipe.monitor.available = False
    for tick in range(120):
        pipe.get_current_file_size_mb.return_value = tick + 1
        dog.check_once(float(tick))
    assert dog.snapshot().video_motion is None
    assert actions == []


def test_missing_new_frames_is_left_to_stream_verification():
    dog, pipe, nav, actions = build_dog()
    pipe.monitor.new_frames = False
    for tick in range(30):
        pipe.get_current_file_size_mb.return_value = tick + 1
        dog.check_once(float(tick))
    assert dog.snapshot().video_frozen_sec == 0
    assert actions == []


def test_confirmed_no_new_frames_counts_from_first_missing_frame():
    dog, pipe, nav, actions = build_dog()
    for tick in range(3):
        pipe.monitor.set_value(50 + tick * 40)
        pipe.get_current_file_size_mb.return_value = tick + 1
        dog.check_once(float(tick))
    pipe.monitor.new_frames = False
    pipe.monitor.frozen_since = 2.5
    pipe.get_current_file_size_mb.return_value = 10
    dog.check_once(3.)
    assert dog.snapshot().video_frozen_sec == .5
    for tick, size in ((5., 11), (7., 12)):
        pipe.get_current_file_size_mb.return_value = size
        dog.check_once(tick)
    assert [a.reason for a in actions] == ['video_frozen', 'video_frozen']
    assert [a.action_type for a in actions] == [ActionType.IN_PLACE_HEAL, ActionType.RESTART_SEGMENT]


def test_new_watchdog_restarts_freeze_timing_from_its_creation():
    dog, pipe, nav, actions = build_dog()
    pipe.monitor.new_frames = False
    pipe.monitor.frozen_since = 2.0
    dog.check_once(20.)
    assert dog.snapshot().video_frozen_sec == 0
    pipe.get_current_file_size_mb.return_value = 11
    dog.check_once(21.)
    assert dog.snapshot().video_frozen_sec == 1


def test_first_thread_sample_precedes_interval_wait():
    import threading
    dog,pipe,nav,actions=build_dog()
    dog.check_interval_sec=60
    sampled=threading.Event()
    check=dog.check_once
    def sample():
        check()
        sampled.set()
    dog.check_once=sample
    dog.start()
    try:
        assert sampled.wait(1), '首轮采样不能等待常规轮询间隔'
    finally:
        dog.stop()
    nav.get_current_focus_activity.assert_called_once()

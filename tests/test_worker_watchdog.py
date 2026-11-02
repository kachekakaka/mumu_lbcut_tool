"""Worker 卡顿看门狗单元测试。"""
import time
import unittest
from unittest.mock import MagicMock
from src.worker.watchdog import StallWatchdog


class TestStallWatchdog(unittest.TestCase):
    def test_detect_stall_and_trigger_intent_refresh(self):
        mock_pipeline = MagicMock()
        # 模拟文件大小持续不变（0 字节增长）
        mock_pipeline.get_current_file_size_mb.return_value = 10.5
        mock_pipeline.is_recording.return_value = True

        mock_navigator = MagicMock()
        mock_navigator.refresh_current_room.return_value = True

        healed_events = []
        dog = StallWatchdog(
            pipeline=mock_pipeline,
            navigator=mock_navigator,
            room_id="737141",
            check_interval_sec=0.05,
            stall_threshold_sec=0.1,  # 0.1 秒无增长触发自愈
            on_stall_action=lambda act, dur: healed_events.append((act, dur)),
        )

        dog.start()
        for _ in range(10):
            if mock_navigator.refresh_current_room.called:
                break
            time.sleep(0.05)
        dog.stop()

        # 验证是否触发了原地 Intent 刷新
        self.assertTrue(mock_navigator.refresh_current_room.called)
        self.assertGreater(len(healed_events), 0)

    def test_visual_watchdog_detects_login_page_and_triggers_fatal(self):
        import numpy as np
        mock_pipeline = MagicMock()
        # 模拟文件大小每秒都在增长（以此模拟盲目录制死屏）
        sizes = [1.0, 1.2, 1.4, 1.6, 1.8, 2.0]
        mock_pipeline.get_current_file_size_mb.side_effect = lambda: sizes.pop(0) if sizes else 2.5
        mock_pipeline.is_recording.return_value = True

        mock_navigator = MagicMock()
        mock_navigator.get_current_focus_activity.return_value = "com.ss.android.ugc.aweme.account.business.login.DYLoginActivity"
        # 构造高白底大面积粉色按钮的登录帧
        login_frame = np.full((100, 100, 3), 250, dtype=np.uint8)
        login_frame[40:60, 20:80, 0] = 240  # R 高
        login_frame[40:60, 20:80, 1] = 50   # G 低
        login_frame[40:60, 20:80, 2] = 80   # B 低 (粉红按钮)
        mock_navigator._capture_frame_array.return_value = login_frame

        fatal_called = []
        dog = StallWatchdog(
            pipeline=mock_pipeline,
            navigator=mock_navigator,
            room_id="737141",
            check_interval_sec=0.05,
            stall_threshold_sec=10.0,  # 物理流阈值很大，不会触发文件卡顿
            visual_probe_interval_sec=0.1,  # 视觉抽样 0.1s 极速触发
            on_fatal_stall=lambda: fatal_called.append(True),
        )

        dog.start()
        for _ in range(15):
            if fatal_called:
                break
            time.sleep(0.05)
        dog.stop()

        # 断言看门狗尝试点击了跳过与拒绝穿透
        self.assertTrue(mock_navigator.dismiss_login_prompt.called)
        self.assertTrue(mock_navigator.dismiss_center_dialog.called)
        # 断言连续检测到登录页后触发了致命故障回调
        self.assertTrue(len(fatal_called) > 0)


if __name__ == "__main__":
    unittest.main()

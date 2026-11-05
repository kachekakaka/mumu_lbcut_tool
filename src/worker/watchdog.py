"""卡顿看门狗线程与原地自愈意图投递器。"""
import logging
import threading
import time
from typing import Callable, Optional

from src.worker.navigator import WorkerNavigator
from src.worker.pipeline import WorkerPipeline
from src.worker.state_machine import ActionType, PipelineAction

logger = logging.getLogger(__name__)


class StallWatchdog:
    """监测录制文件字节增长与画面动态，向主线程投递分级自愈意图。

    严格遵循 ADR 0008 单写者队列设计：
    看门狗作为只读观察者，仅投递带采样时代际快照的 PipelineAction，
    严禁直接调用导航刷新或发射生命周期事件。
    """

    def __init__(
        self,
        pipeline: WorkerPipeline,
        navigator: WorkerNavigator,
        room_id: str,
        check_interval_sec: float = 2.0,
        stall_threshold_sec: float = 10.0,
        segment_recycle_threshold_sec: float = 20.0,
        fatal_threshold_sec: float = 45.0,
        visual_probe_interval_sec: float = 30.0,
        generation_provider: Callable[[], int] | None = None,
        on_post_action: Callable[[PipelineAction], None] | None = None,
        on_stall_action: Callable[[str, float], None] | None = None,
        on_request_new_segment: Callable[[], None] | None = None,
        on_fatal_stall: Callable[[], None] | None = None,
    ):
        self.pipeline = pipeline
        self.navigator = navigator
        self.room_id = room_id
        self.check_interval_sec = check_interval_sec
        self.stall_threshold_sec = stall_threshold_sec
        self.segment_recycle_threshold_sec = segment_recycle_threshold_sec
        self.fatal_threshold_sec = fatal_threshold_sec
        self.visual_probe_interval_sec = visual_probe_interval_sec

        self.generation_provider = generation_provider or (lambda: 0)
        self.on_post_action = on_post_action
        self.on_stall_action = on_stall_action
        self.on_request_new_segment = on_request_new_segment
        self.on_fatal_stall = on_fatal_stall

        self._running = False
        self._thread: threading.Thread | None = None
        self._last_size_mb: float = 0.0
        self._stall_start_time: float | None = None
        self._action_taken_level: int = 0
        self._last_visual_probe_time: float = 0.0
        self._visual_stall_count: int = 0

    def start(self):
        """启动监控线程。"""
        self._running = True
        self._stall_start_time = None
        self._action_taken_level = 0
        self._visual_stall_count = 0
        self._last_visual_probe_time = time.time()
        self._last_size_mb = self.pipeline.get_current_file_size_mb()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info(f"卡顿看门狗已启动 (检查间隔 {self.check_interval_sec}s, 阈值 {self.stall_threshold_sec}s, 视觉探针 {self.visual_probe_interval_sec}s)")

    def stop(self):
        """停止监控。"""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        logger.info("卡顿看门狗已停止。")

    def notify_segment_reset(self):
        """切片轮转切换时，安全重置看门狗的字节基线与卡顿计时器，杜绝误判跨切片卡顿。"""
        self._last_size_mb = 0.0
        self._stall_start_time = None
        self._action_taken_level = 0
        self._visual_stall_count = 0

    def _run_loop(self):
        import numpy as np

        while self._running:
            time.sleep(self.check_interval_sec)
            if not self._running or not self.pipeline.is_recording():
                continue

            # 捕获采样当时的代际快照
            sampled_gen = self.generation_provider()

            # 1. 周期性视觉动态抽样 (Visual Probe)
            now = time.time()
            if now - self._last_visual_probe_time >= self.visual_probe_interval_sec:
                self._last_visual_probe_time = now
                act = self.navigator.get_current_focus_activity() or ""
                frame = self.navigator._capture_frame_array()
                is_login_page = False
                if "DYLoginActivity" in act or "login" in act.lower():
                    is_login_page = True
                elif frame is not None:
                    # 1. 全屏白底与粉色按钮检测（全屏 DYLoginActivity）
                    white_ratio = float(np.mean((frame[:, :, 0] > 235) & (frame[:, :, 1] > 235) & (frame[:, :, 2] > 235)))
                    pink_pixels = int(np.sum((frame[:, :, 0] > 220) & (frame[:, :, 1] < 120) & (frame[:, :, 2] < 150)))
                    # 2. 居中卡片区域白底检测（居中“发现抖音朋友”弹窗）
                    h, w, _ = frame.shape
                    center_crop = frame[int(h * 0.27):int(h * 0.76), int(w * 0.11):int(w * 0.89)]
                    center_white = float(np.mean((center_crop[:, :, 0] > 235) & (center_crop[:, :, 1] > 235) & (center_crop[:, :, 2] > 235))) if center_crop.size > 0 else 0.0

                    if (white_ratio > 0.65 and pink_pixels > 1000) or center_white > 0.70:
                        is_login_page = True

                if is_login_page:
                    self._visual_stall_count += 1
                    logger.warning(f"视觉看门狗检测到登录/阻断弹窗遮罩 (连续命中 {self._visual_stall_count} 次)")
                    if self._visual_stall_count == 1:
                        # 尝试一级穿透自愈
                        if self.on_post_action:
                            self.on_post_action(PipelineAction(
                                action_type=ActionType.IN_PLACE_HEAL,
                                generation_id=sampled_gen,
                                reason="watchdog_visual_login_detected",
                            ))
                        else:
                            self.navigator.dismiss_login_prompt()
                            self.navigator.dismiss_center_dialog()
                            self.navigator.refresh_current_room(self.room_id)
                        if self.on_stall_action:
                            self.on_stall_action("visual_login_healed", self.visual_probe_interval_sec)
                    elif self._visual_stall_count >= 2:
                        logger.error("视觉看门狗连续检测到登录阻断且自愈失败，投递致命故障动作")
                        if self.on_post_action:
                            self.on_post_action(PipelineAction(
                                action_type=ActionType.FATAL_STOP,
                                generation_id=sampled_gen,
                                reason="watchdog_visual_login_fatal",
                            ))
                        if self.on_fatal_stall:
                            self.on_fatal_stall()
                        return
                else:
                    self._visual_stall_count = 0

            # 2. 物理流文件体积增长检测
            current_size = self.pipeline.get_current_file_size_mb()

            # 判断是否有字节增长
            if current_size > self._last_size_mb:
                # 正常增长，重置卡顿计时器
                if self._stall_start_time is not None:
                    recovered_dur = time.time() - self._stall_start_time
                    logger.info(f"画面与录制流恢复正常，卡顿结束 (持续 {recovered_dur:.1f}s)")
                    if self.on_stall_action:
                        self.on_stall_action("stall_healed", recovered_dur)
                    self._stall_start_time = None
                    self._action_taken_level = 0
                self._last_size_mb = current_size
                continue

            # 出现无增长
            now = time.time()
            if self._stall_start_time is None:
                self._stall_start_time = now

            stall_duration = now - self._stall_start_time

            # 一级自愈：卡顿超过 stall_threshold_sec，向主线程投递原地 Intent 刷新请求
            if stall_duration >= self.stall_threshold_sec and self._action_taken_level < 1:
                logger.warning(f"检测到画面卡顿达 {stall_duration:.1f}s，触发一级自愈：原地 Intent 重新唤起")
                self._action_taken_level = 1
                if self.on_post_action:
                    self.on_post_action(PipelineAction(
                        action_type=ActionType.IN_PLACE_HEAL,
                        generation_id=sampled_gen,
                        reason="watchdog_motion_stall",
                    ))
                else:
                    self.navigator.refresh_current_room(self.room_id)
                if self.on_stall_action:
                    self.on_stall_action("intent_refresh", stall_duration)

            # 二级自愈：卡顿超过 segment_recycle_threshold_sec，向主线程投递切片重拉请求
            elif stall_duration >= self.segment_recycle_threshold_sec and self._action_taken_level < 2:
                logger.warning(f"卡顿持续达 {stall_duration:.1f}s，投递二级自愈：切片重拉")
                self._action_taken_level = 2
                if self.on_post_action:
                    self.on_post_action(PipelineAction(
                        action_type=ActionType.RESTART_SEGMENT,
                        generation_id=sampled_gen,
                        reason="watchdog_stall_recycle",
                    ))
                if self.on_request_new_segment:
                    self.on_request_new_segment()
                if self.on_stall_action:
                    self.on_stall_action("segment_recycle", stall_duration)

            # 三级自愈：超过 fatal_threshold_sec，向主线程投递致命故障
            elif stall_duration >= self.fatal_threshold_sec and self._action_taken_level < 3:
                logger.error(f"卡顿超过致命阈值 {stall_duration:.1f}s，投递致命故障动作")
                self._action_taken_level = 3
                if self.on_post_action:
                    self.on_post_action(PipelineAction(
                        action_type=ActionType.FATAL_STOP,
                        generation_id=sampled_gen,
                        reason="watchdog_fatal_stall",
                    ))
                if self.on_fatal_stall:
                    self.on_fatal_stall()

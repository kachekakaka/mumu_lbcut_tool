"""只读看门狗：录像流画面 ROI 帧差、独立文件进度与登录页检测。

录制期间不向模拟器索取截图；画面来自采集会话的录像流分析（StreamMonitor）。
画面检查不可用时只靠文件增长检测，不因此停止录制。
"""
from dataclasses import dataclass
import logging
import threading
import time
from typing import Callable

import numpy as np
from src.worker.state_machine import PipelineAction, ActionType

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WatchdogSnapshot:
    generation_id: int
    sampled_at: float
    video_motion: float | None
    video_frozen_sec: float
    file_stall_sec: float
    login_blocked: bool = False


class StallWatchdog:
    """后台仅采样和投递意图；导航、重启和生命周期事件均归主控制器。"""
    def __init__(self, pipeline, navigator, room_id: str,
                 check_interval_sec: float = 2.0, stall_threshold_sec: float = 10.0,
                 segment_recycle_threshold_sec: float = 20.0, fatal_threshold_sec: float = 45.0,
                 visual_probe_interval_sec: float = 5.0,
                 generation_provider: Callable[[], int] | None = None,
                 on_post_action: Callable[[PipelineAction], None] | None = None,
                 on_stall_action=None, on_request_new_segment=None, on_fatal_stall=None):
        self.pipeline, self.navigator, self.room_id = pipeline, navigator, room_id
        self.check_interval_sec = check_interval_sec
        self.stall_threshold_sec = stall_threshold_sec
        self.segment_recycle_threshold_sec = max(segment_recycle_threshold_sec, stall_threshold_sec * 2)
        self.fatal_threshold_sec = max(fatal_threshold_sec, self.segment_recycle_threshold_sec + 5)
        self.visual_probe_interval_sec = visual_probe_interval_sec
        self.generation_provider = generation_provider or (lambda: 0)
        self.on_post_action = on_post_action
        self.on_stall_action = on_stall_action
        self.on_request_new_segment = on_request_new_segment
        self.on_fatal_stall = on_fatal_stall
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._snapshot = None
        self._sample_gen = None
        self._reset()

    def _reset(self):
        self._previous_roi = None
        self._last_size_mb = None
        self._stall_start_time = None
        self._frozen_since = None
        self._reset_at = None
        self._frame_seq = None
        self._last_visual_probe_time = float('-inf')
        self._low_motion_count = 0
        self._visual_stall_count = 0
        self._action_taken_level = 0
        self._motion = None

    def notify_segment_reset(self):
        with self._lock:
            self._sample_gen = None
            self._snapshot = None

    def start(self):
        if self._thread and self._thread.is_alive():
            raise RuntimeError("旧看门狗未退出，禁止创建重复监控线程")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="recording-watchdog")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)

    def snapshot(self) -> WatchdogSnapshot | None:
        with self._lock:
            return self._snapshot

    @staticmethod
    def _roi(frame):
        """灰度小图的画面上中部，避开底部弹幕区。"""
        if frame is None or not isinstance(frame, np.ndarray) or frame.ndim != 2:
            return None
        h, w = frame.shape
        crop = frame[int(h * .15):int(h * .65), int(w * .05):int(w * .95)]
        if crop.size == 0:
            return None
        return crop.astype(np.float32)

    def _sample_view(self, view, now):
        if view is None or not view.available:
            self._previous_roi = self._frame_seq = self._motion = None
            self._frozen_since = None
            self._low_motion_count = 0
            return
        if view.frozen_since is not None:
            # 流分析已核实录像本身没有新画面；新建看门狗只从自身创建时刻起算。
            since = max(view.frozen_since, self._reset_at)
            self._frozen_since = since if self._frozen_since is None else min(self._frozen_since, since)
            self._low_motion_count = max(self._low_motion_count, 2)
            self._motion = 0.0
            return
        if view.frame is None or view.frame_seq == self._frame_seq:
            return  # 暂无新帧：是否冻结由流分析的无帧核实决定，这里不推断。
        self._frame_seq = view.frame_seq
        roi = self._roi(view.frame)
        if roi is None:
            return
        if self._previous_roi is not None and roi.shape == self._previous_roi.shape:
            self._motion = float(np.mean(np.abs(roi - self._previous_roi)) / 255.0)
            if self._motion < .003:
                self._low_motion_count += 1
                if self._frozen_since is None:
                    self._frozen_since = now
            else:
                self._frozen_since = None
                self._low_motion_count = 0
        else:
            self._frozen_since = None
            self._low_motion_count = 0
        self._previous_roi = roi

    def _post(self, kind: ActionType, gen: int, reason: str):
        if self._stop.is_set() or gen != self.generation_provider():
            return
        if self.on_post_action:
            self.on_post_action(PipelineAction(kind, gen, reason))
        # 保留无副作用的通知接口，不再在后台直接点击或刷新导航。
        if kind == ActionType.RESTART_SEGMENT and self.on_request_new_segment:
            self.on_request_new_segment()
        if kind == ActionType.FATAL_STOP and self.on_fatal_stall:
            self.on_fatal_stall()

    def check_once(self, now: float | None = None):
        """一次真实采样；公开测试接缝，不依赖线程调度即可故障注入。"""
        now = time.monotonic() if now is None else now
        gen = self.generation_provider()
        with self._lock:
            if self._sample_gen != gen:
                self._reset()
                self._sample_gen = gen
        if self._reset_at is None:
            self._reset_at = now
        if not self.pipeline.is_recording():
            return
        size = self.pipeline.get_current_file_size_mb()
        if self._last_size_mb is not None and size <= self._last_size_mb:
            if self._stall_start_time is None:
                self._stall_start_time = now
        else:
            self._stall_start_time = None
        self._last_size_mb = size

        if now - self._last_visual_probe_time >= self.visual_probe_interval_sec:
            self._last_visual_probe_time = now
            act = self.navigator.get_current_focus_activity() or ""
            monitor = getattr(self.pipeline, "monitor", None)
            view = monitor.view() if monitor else None
            if gen != self.generation_provider() or self._stop.is_set():
                return  # 旧采样不可污染新切片。
            # 登录强结论使用实际 Activity；原生面板交由只读表面探针识别后返回关闭。
            self._visual_stall_count = self._visual_stall_count + 1 if "login" in act.lower() else 0
            self._sample_view(view, now)

        frozen = now - self._frozen_since if self._frozen_since is not None else 0.0
        stalled = now - self._stall_start_time if self._stall_start_time is not None else 0.0
        snapshot = WatchdogSnapshot(gen, now, self._motion, frozen, stalled,
                                    self._visual_stall_count > 0)
        with self._lock:
            self._snapshot = snapshot
        if self._visual_stall_count >= 2:
            self._post(ActionType.FATAL_STOP, gen, "login_blocked")
            return
        duration = max(stalled, frozen if self._low_motion_count >= 2 else 0.0)
        reason = "file_progress_stalled" if stalled >= frozen else "video_frozen"
        if self._visual_stall_count == 1:
            duration = max(duration, self.stall_threshold_sec)
            reason = "login_prompt_detected"
        if duration >= self.fatal_threshold_sec and self._action_taken_level < 3:
            self._action_taken_level = 3
            self._post(ActionType.FATAL_STOP, gen, reason)
        elif duration >= self.segment_recycle_threshold_sec and self._action_taken_level < 2:
            self._action_taken_level = 2
            self._post(ActionType.RESTART_SEGMENT, gen, reason)
        elif duration >= self.stall_threshold_sec and self._action_taken_level < 1:
            self._action_taken_level = 1
            self._post(ActionType.IN_PLACE_HEAL, gen, reason)
        elif duration == 0 and not self._visual_stall_count:
            self._action_taken_level = 0

    def _run_loop(self):
        failures = 0
        while not self._stop.wait(self.check_interval_sec):
            try:
                self.check_once()
                failures = 0
            except Exception:
                failures += 1
                logger.exception("看门狗采样异常")
                if failures >= 3:
                    self._post(ActionType.FATAL_STOP, self.generation_provider(), "watchdog_probe_failed")
                    return

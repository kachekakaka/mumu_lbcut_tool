"""录播 Worker 顶层状态机与生命周期控制器。"""
from dataclasses import dataclass
from pathlib import Path
import logging
import os
import signal
import sys
import time

from src.mvp.dep_manager import DependencyManager
from src.mvp.mumu_client import MuMuClient
from src.worker.events import (
    WorkerEventEmitter,
    WorkerStartedEvent,
    SegmentStartedEvent,
    HeartbeatEvent,
    StallDetectedEvent,
    StallHealedEvent,
    SegmentFinishedEvent,
    StreamEndedEvent,
    LoginBlockedEvent,
)
from src.worker.navigator import WorkerNavigator
from src.worker.pipeline import WorkerPipeline
from src.worker.resolver import WorkerRoomResolver, RoomStatus
from src.worker.watchdog import StallWatchdog

logger = logging.getLogger(__name__)

# 退出码常量
EXIT_SUCCESS = 0
EXIT_STREAM_ENDED = 2
EXIT_TARGET_INVALID = 10
EXIT_LOGIN_BLOCKED = 15
EXIT_DEVICE_UNAVAILABLE = 20
EXIT_RECORDING_FAILED = 30


@dataclass
class WorkerConfig:
    vmindex: int
    target: str
    output_dir: Path
    segment_duration_sec: int = 1800  # 默认 30 分钟切片，消灭累积漂移
    total_duration_sec: int = 0       # 0 表示持续录制直到停止信号或下播
    heartbeat_interval_sec: float = 3.0
    stall_timeout_sec: float = 10.0


class WorkerController:
    """控制单路录制的生命周期、分段切片、看门狗自愈与优雅退出。"""

    def __init__(self, config: WorkerConfig, emitter: WorkerEventEmitter | None = None):
        self.config = config
        self.emitter = emitter or WorkerEventEmitter(stream=sys.stdout)
        self.stop_requested = False
        self._fatal_error = False
        self._login_blocked = False
        self._pipeline: WorkerPipeline | None = None
        self._watchdog: StallWatchdog | None = None

    def request_stop(self):
        """外部中断信号触发平稳停止。"""
        self.stop_requested = True

    def run(self) -> int:
        """主执行循环，返回进程退出码。"""
        pid = os.getpid()
        self.emitter.emit(WorkerStartedEvent(
            vmindex=self.config.vmindex,
            target=self.config.target,
            pid=pid
        ))

        # 1. 解析目标直播间
        resolver = WorkerRoomResolver()
        resolved = resolver.resolve(self.config.target)
        if not resolved.web_rid:
            logger.error("直播间输入格式无效，无法解析。")
            return EXIT_TARGET_INVALID

        # 2. 检查开播状态
        if resolved.status == RoomStatus.ENDED:
            logger.info("目标直播间当前处于未开播/已下播状态。")
            self.emitter.emit(StreamEndedEvent(reason="anchor_offline", exit_code=EXIT_STREAM_ENDED))
            return EXIT_STREAM_ENDED

        # 3. 准备运行组件与 MuMu 实例
        dep_mgr = DependencyManager()
        scrcpy_exe = dep_mgr.ensure_scrcpy()
        adb_exe = dep_mgr.get_adb_path()

        mumu = MuMuClient(adb_exe=adb_exe)
        if not mumu.launch_player(self.config.vmindex):
            logger.error(f"启动 MuMu 实例 {self.config.vmindex} 失败！")
            return EXIT_DEVICE_UNAVAILABLE

        serial = mumu.connect_adb(self.config.vmindex)
        if not serial:
            logger.error("建立 ADB 连接失败！")
            return EXIT_DEVICE_UNAVAILABLE

        # 4. 唤起直播间
        target_id = resolved.room_id or resolved.web_rid
        nav = WorkerNavigator(serial=serial, adb_exe=adb_exe)
        nav.launch_room(room_id=target_id, web_rid=resolved.web_rid, wait_sec=3)
        nav.dismiss_system_dialogs()

        # 5. 开录前置双重内容门禁校验 (Pre-flight Content Gate)
        gate_ok = nav.verify_preflight_content_gate(timeout_sec=8.0)
        if not gate_ok:
            logger.error("未通过开录前置门禁：直播间未成功渲染或被登录页强阻断，拒绝盲目录制！")
            self.emitter.emit(LoginBlockedEvent(
                reason="preflight_gate_failed",
                vmindex=self.config.vmindex
            ))
            return EXIT_LOGIN_BLOCKED

        # 6. 初始化录制管线与切片循环
        self._pipeline = WorkerPipeline(
            serial=serial,
            output_dir=self.config.output_dir,
            scrcpy_exe=scrcpy_exe,
            adb_exe=adb_exe,
        )

        # 7. 初始化看门狗
        def on_stall_action(act: str, dur: float):
            if act == "intent_refresh":
                self.emitter.emit(StallDetectedEvent(stall_duration_sec=dur, action="intent_refresh"))
            elif act == "stall_healed":
                self.emitter.emit(StallHealedEvent(action="intent_refresh", latency_sec=dur))

        def on_request_new_segment():
            # 由看门狗触发的强制切片重启
            self._recycle_current_segment()

        def on_fatal_stall():
            self._fatal_error = True
            if self._watchdog and self._watchdog._visual_stall_count >= 2:
                self._login_blocked = True
                self.emitter.emit(LoginBlockedEvent(
                    reason="runtime_visual_login_blocked",
                    vmindex=self.config.vmindex
                ))
            self.request_stop()

        self._watchdog = StallWatchdog(
            pipeline=self._pipeline,
            navigator=nav,
            room_id=target_id,
            stall_threshold_sec=self.config.stall_timeout_sec,
            on_stall_action=on_stall_action,
            on_request_new_segment=on_request_new_segment,
            on_fatal_stall=on_fatal_stall,
        )

        segment_idx = 1
        start_time = time.time()
        last_heartbeat = 0.0

        try:
            # 开启第一个切片
            cur_file = self._pipeline.start_segment(segment_idx)
            self.emitter.emit(SegmentStartedEvent(
                segment_index=segment_idx,
                file_path=str(cur_file),
                target_duration_sec=self.config.segment_duration_sec
            ))
            self._watchdog.start()

            while not self.stop_requested:
                time.sleep(0.5)
                now = time.time()
                total_elapsed = now - start_time
                seg_elapsed = self._pipeline.get_current_segment_elapsed()

                # 发射心跳
                if now - last_heartbeat >= self.config.heartbeat_interval_sec:
                    self.emitter.emit(HeartbeatEvent(
                        elapsed_sec=int(total_elapsed),
                        segment_elapsed_sec=int(seg_elapsed),
                        file_size_mb=round(self._pipeline.get_current_file_size_mb(), 2),
                        fps=30.0,
                        status="recording"
                    ))
                    last_heartbeat = now

                # 检查总录制时长限制
                if self.config.total_duration_sec > 0 and total_elapsed >= self.config.total_duration_sec:
                    logger.info(f"达到预设总录制时长 {self.config.total_duration_sec}s，平稳结束。")
                    break

                # 检查周期切片（默认 30 分钟）
                if seg_elapsed >= self.config.segment_duration_sec:
                    logger.info(f"切片 #{segment_idx} 录制满 {self.config.segment_duration_sec}s，准备无缝切换下一段...")
                    self._watchdog.stop()
                    seg_size = self._pipeline.get_current_file_size_mb()
                    self._pipeline.stop_segment()
                    self.emitter.emit(SegmentFinishedEvent(
                        segment_index=segment_idx,
                        file_path=str(cur_file),
                        duration_sec=int(seg_elapsed),
                        file_size_mb=round(seg_size, 2)
                    ))

                    # 开启下一段切片（时间戳硬件重置为0）
                    segment_idx += 1
                    cur_file = self._pipeline.start_segment(segment_idx)
                    self.emitter.emit(SegmentStartedEvent(
                        segment_index=segment_idx,
                        file_path=str(cur_file),
                        target_duration_sec=self.config.segment_duration_sec
                    ))
                    self._watchdog.start()

        finally:
            if self._watchdog:
                self._watchdog.stop()
            if self._pipeline and self._pipeline.is_recording():
                seg_elapsed = self._pipeline.get_current_segment_elapsed()
                seg_size = self._pipeline.get_current_file_size_mb()
                self._pipeline.stop_segment()
                self.emitter.emit(SegmentFinishedEvent(
                    segment_index=segment_idx,
                    file_path=str(self._pipeline.current_file_path or ""),
                    duration_sec=int(seg_elapsed),
                    file_size_mb=round(seg_size, 2)
                ))

        if self._login_blocked:
            return EXIT_LOGIN_BLOCKED

        if self._fatal_error:
            return EXIT_RECORDING_FAILED

        return EXIT_SUCCESS

    def _recycle_current_segment(self):
        """强制重启切片以恢复卡死。"""
        if not self._pipeline:
            return
        idx = self._pipeline.current_segment_index
        logger.warning(f"看门狗正在快速重拉切片 #{idx}...")
        self._pipeline.stop_segment()
        next_idx = idx + 1
        new_file = self._pipeline.start_segment(next_idx)
        self.emitter.emit(SegmentStartedEvent(
            segment_index=next_idx,
            file_path=str(new_file),
            target_duration_sec=self.config.segment_duration_sec
        ))

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
    SegmentAbortedEvent,
    StreamEndedEvent,
    LoginBlockedEvent,
)
from src.worker.state_machine import (
    WorkerState,
    WorkerStateMachine,
    GenerationActionDispatcher,
    PipelineAction,
    ActionType,
)
from src.worker.navigator import WorkerNavigator
from src.worker.pipeline import WorkerPipeline
from src.worker.resolver import WorkerRoomResolver, RoomStatus
from src.worker.naming_lock import build_segment_filename, WorkerInstanceLock, LockAcquisitionError
from src.worker.popup_bypasser import PopupBypasser
from src.worker.identity_verifier import verify_room_identity, IdentityStatus
from src.worker.segment_verifier import finalize_segment, verify_segment_file
from src.worker.health_monitor import HealthMonitor, HealthSignals
from src.worker.stream_end_detector import StreamEndDetector
from src.worker.watchdog import StallWatchdog

logger = logging.getLogger(__name__)

# 退出码常量
EXIT_SUCCESS = 0
EXIT_STREAM_ENDED = 2
EXIT_TARGET_INVALID = 9
EXIT_TARGET_OFFLINE = 10          # 确诊未开播
EXIT_GATE_NETWORK_ERROR = 11      # 门禁检测服务异常
EXIT_LOGIN_BLOCKED = 15           # 登录页或弹窗强阻断
EXIT_ROOM_MISMATCH = 18           # 确诊错房
EXIT_DEVICE_UNAVAILABLE = 20      # 设备离线或未就绪
EXIT_RECORDING_FAILED = 30        # 录制崩溃或异常无法自愈


@dataclass
class WorkerConfig:
    vmindex: int
    target: str
    output_dir: Path
    segment_duration_sec: int = 1800  # 默认 30 分钟切片，消灭累积漂移
    total_duration_sec: int = 0       # 0 表示持续录制直到停止信号或下播
    heartbeat_interval_sec: float = 3.0
    stall_timeout_sec: float = 10.0
    allow_unconfirmed_identity: bool = False


class WorkerController:
    """控制单路录制的生命周期、分段切片、看门狗自愈与优雅退出。"""

    def __init__(self, config: WorkerConfig, emitter: WorkerEventEmitter | None = None):
        self.config = config
        self.emitter = emitter or WorkerEventEmitter(stream=sys.stdout)
        self.stop_requested = False
        self._fatal_error = False
        self._login_blocked = False
        self._total_crash_count = 0
        self.MAX_RECOVERY_RETRIES = 3
        self._pipeline: WorkerPipeline | None = None
        self._watchdog: StallWatchdog | None = None
        self.state_machine = WorkerStateMachine(initial_state=WorkerState.PREFLIGHT)
        self.action_dispatcher = GenerationActionDispatcher()
        self.current_generation_id = 0

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

        # 2. 开录门禁状态严格分流
        if resolved.status == RoomStatus.ENDED:
            logger.info("目标直播间当前确诊未开播。")
            sys.stderr.write("[开录门禁阻断] 主播未开播\n")
            self.emitter.emit(StreamEndedEvent(reason="anchor_offline", exit_code=EXIT_TARGET_OFFLINE))
            return EXIT_TARGET_OFFLINE
        elif resolved.status != RoomStatus.LIVE:
            logger.error("门禁接口返回异常或网络超时，无法确认开播状态，阻断盲录。")
            sys.stderr.write("[开录门禁阻断] 门禁检测服务异常，无法确认开播状态\n")
            return EXIT_GATE_NETWORK_ERROR

        # 实例锁防抢占 (确诊开播后获取)
        lock_dir = self.config.output_dir / ".locks" if isinstance(self.config.output_dir, Path) else Path("./output/.locks")
        instance_lock = WorkerInstanceLock(lock_dir=lock_dir, vmindex=self.config.vmindex, room_id=resolved.web_rid)
        try:
            instance_lock.acquire()
        except LockAcquisitionError as e:
            logger.error(str(e))
            return EXIT_DEVICE_UNAVAILABLE

        try:
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

            # 4. 唤起直播间与白名单弹窗穿透
            target_id = resolved.room_id or resolved.web_rid
            nav = WorkerNavigator(serial=serial, adb_exe=adb_exe)
            nav.launch_room(room_id=target_id, web_rid=resolved.web_rid, wait_sec=3)
            nav.dismiss_system_dialogs()

            # 白名单弹窗穿透 (如青少年模式/更新提示)
            bypasser = PopupBypasser(adb_client=nav.adb_client)
            bypasser.detect_and_dismiss_popups()

            # 身份核验闭环：UID 错房绝对阻断
            id_res = verify_room_identity(
                target_uid=resolved.room_id,
                current_uid=target_id,
                target_nickname=resolved.nickname,
                current_nickname=resolved.nickname,
                allow_unconfirmed=self.config.allow_unconfirmed_identity,
            )
            if not id_res.can_proceed:
                logger.error(f"主播身份核验未通过阻断: {id_res.reason}")
                return EXIT_ROOM_MISMATCH

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

            # 7. 初始化看门狗投递队列
            def on_stall_action(act: str, dur: float):
                if act == "intent_refresh":
                    self.emitter.emit(StallDetectedEvent(stall_duration_sec=dur, action="intent_refresh"))
                elif act == "stall_healed":
                    self.emitter.emit(StallHealedEvent(action="intent_refresh", latency_sec=dur))

            def on_request_new_segment():
                # 由看门狗只读监测投递动作请求，附带当前切片快照代际 ID
                self.action_dispatcher.post_action(PipelineAction(
                    action_type=ActionType.RESTART_SEGMENT,
                    generation_id=self.current_generation_id,
                    reason="watchdog_stall_recycle",
                ))

            def on_fatal_stall():
                self.action_dispatcher.post_action(PipelineAction(
                    action_type=ActionType.FATAL_STOP,
                    generation_id=self.current_generation_id,
                    reason="watchdog_fatal_stall",
                ))

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
            self.current_generation_id = 1
            self.action_dispatcher.current_generation_id = self.current_generation_id
            start_time = time.time()
            last_heartbeat = 0.0

            self.state_machine.transition_to(WorkerState.RECORDING)
            cur_file = self._pipeline.start_segment(segment_idx)
            self.emitter.emit(SegmentStartedEvent(
                segment_index=segment_idx,
                file_path=str(cur_file),
                target_duration_sec=self.config.segment_duration_sec
            ))
            self._watchdog.start()

            while not self.stop_requested:
                time.sleep(0.5)

                # 秒级录制进程存活与异常退出探针
                alive = self._check_pipeline_survival(segment_idx=segment_idx)
                if not alive:
                    break

                now = time.time()
                total_elapsed = now - start_time
                seg_elapsed = self._pipeline.get_current_segment_elapsed()

                # 单写者动作处理：由主线程唯一消费并执行动作
                actions = self.action_dispatcher.drain_actions()
                for action in actions:
                    if action.action_type == ActionType.FATAL_STOP:
                        self._fatal_error = True
                        if self._watchdog and getattr(self._watchdog, "_visual_stall_count", 0) >= 2:
                            self._login_blocked = True
                            self.emitter.emit(LoginBlockedEvent(
                                reason="runtime_visual_login_blocked",
                                vmindex=self.config.vmindex
                            ))
                        self.request_stop()
                        break
                    elif action.action_type == ActionType.RESTART_SEGMENT:
                        logger.info(f"主线程处理代际有效动作: 重启当前切片 (reason: {action.reason})")
                        self._recycle_current_segment()
                        segment_idx += 1
                        self.current_generation_id += 1
                        self.action_dispatcher.current_generation_id = self.current_generation_id
                        break

                if self.stop_requested:
                    break

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

                    # 开启下一段切片并递增代际
                    segment_idx += 1
                    self.current_generation_id += 1
                    self.action_dispatcher.current_generation_id = self.current_generation_id
                    cur_file = self._pipeline.start_segment(segment_idx)
                    self.emitter.emit(SegmentStartedEvent(
                        segment_index=segment_idx,
                        file_path=str(cur_file),
                        target_duration_sec=self.config.segment_duration_sec
                    ))
                    self._watchdog.start()

        finally:
            if instance_lock:
                instance_lock.release()
            if self._watchdog:
                self._watchdog.stop()
            if self.state_machine.state in {WorkerState.RECORDING, WorkerState.RECOVERING}:
                self.state_machine.transition_to(WorkerState.STOPPING)
            if self._pipeline and self._pipeline.is_recording():
                seg_elapsed = self._pipeline.get_current_segment_elapsed()
                seg_size = self._pipeline.get_current_file_size_mb()
                self._pipeline.stop_segment()
                if self._fatal_error or self._login_blocked:
                    # 异常中止切片，发射 segment_aborted，严禁伪报 segment_finished
                    self.emitter.emit(SegmentAbortedEvent(
                        segment_index=segment_idx,
                        file_path=str(self._pipeline.current_file_path or ""),
                        reason="worker_aborted_by_error"
                    ))
                else:
                    target_final = self.config.output_dir / build_segment_filename(
                        room_id=target_id,
                        start_epoch=int(start_time),
                        segment_index=segment_idx,
                        generation_id=self.current_generation_id,
                    )
                    raw_cur = self._pipeline.current_file_path
                    if raw_cur and raw_cur.exists():
                        ok, final_p, res_val = finalize_segment(raw_cur, target_final)
                    else:
                        ok, final_p = False, raw_cur or target_final
                    self.emitter.emit(SegmentFinishedEvent(
                        segment_index=segment_idx,
                        file_path=str(final_p),
                        duration_sec=int(seg_elapsed),
                        file_size_mb=round(seg_size, 2)
                    ))
            if self.state_machine.state == WorkerState.STOPPING:
                self.state_machine.transition_to(WorkerState.STOPPED)

        if self._login_blocked:
            return EXIT_LOGIN_BLOCKED

        if self._fatal_error:
            return EXIT_RECORDING_FAILED

        return EXIT_SUCCESS

    def _check_pipeline_survival(self, segment_idx: int) -> bool:
        """秒级录制进程存活与异常退出探针。"""
        if self._pipeline and not self._pipeline.is_recording():
            exit_code = self._pipeline.get_exit_code()
            logger.warning(f"秒级探针感知到录制进程异常退出！(segment #{segment_idx}, exit_code={exit_code})")
            return self._handle_pipeline_crash(segment_idx=segment_idx, exit_code=exit_code)
        return True

    def _handle_pipeline_crash(self, segment_idx: int, exit_code: int | None = None) -> bool:
        """处理录制子进程崩溃，实施有界就地自愈。"""
        # 1. 迁移到 RECOVERING
        if self.state_machine.state in {WorkerState.RECORDING, WorkerState.PREFLIGHT}:
            try:
                self.state_machine.transition_to(WorkerState.RECOVERING)
            except Exception:
                pass

        # 2. 发射异常中断事件
        file_path = str(self._pipeline.current_file_path or "") if self._pipeline else ""
        self.emitter.emit(SegmentAbortedEvent(
            segment_index=segment_idx,
            file_path=file_path,
            reason=f"process_crash_code_{exit_code}"
        ))

        # 3. 校验重试上限（切片轮转不无条件重置全局计数，防止死循环）
        if self._total_crash_count >= self.MAX_RECOVERY_RETRIES:
            logger.error(
                f"已达最大连续崩溃恢复上限 ({self.MAX_RECOVERY_RETRIES} 次)，果断终止录制，防止死循环！"
            )
            self._fatal_error = True
            self.request_stop()
            return False

        self._total_crash_count += 1
        logger.info(f"正在触发第 {self._total_crash_count}/{self.MAX_RECOVERY_RETRIES} 次就地自愈拉起...")

        # 4. 尝试就地重新拉起
        try:
            new_seg_idx = segment_idx + 1
            self.current_generation_id += 1
            self.action_dispatcher.current_generation_id = self.current_generation_id

            if self._pipeline:
                cur_file = self._pipeline.start_segment(new_seg_idx)
                self.emitter.emit(SegmentStartedEvent(
                    segment_index=new_seg_idx,
                    file_path=str(cur_file),
                    target_duration_sec=self.config.segment_duration_sec
                ))
            self.state_machine.transition_to(WorkerState.RECORDING)
            return True
        except Exception as e:
            logger.error(f"就地拉起恢复失败: {e}")
            self._fatal_error = True
            self.request_stop()
            return False

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

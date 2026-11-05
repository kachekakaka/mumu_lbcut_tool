"""录播 Worker 顶层状态机与生命周期控制器。"""
from dataclasses import dataclass
from pathlib import Path
import logging
import os
import signal
import sys
import time
from typing import Optional
import uuid

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
from src.worker.pipeline import WorkerPipeline, DualPipelineManager
from src.worker.resolver import clean_room_target
from src.worker.naming_lock import (
    build_segment_filename,
    WorkerInstanceLock,
    LockAcquisitionError,
    get_shared_lock_dir,
)
from src.worker.popup_bypasser import PopupBypasser
from src.worker.identity_verifier import verify_room_identity, IdentityStatus
from src.worker.segment_verifier import finalize_segment, verify_segment_file
from src.worker.health_monitor import HealthMonitor, HealthSignals
from src.worker.stream_end_detector import StreamEndDetector
from src.worker.watchdog import StallWatchdog

logger = logging.getLogger(__name__)

# 规范退出码常量
EXIT_SUCCESS = 0
EXIT_STREAM_ENDED = 2
EXIT_TARGET_INVALID = 9
EXIT_TARGET_OFFLINE = 10          # 确诊未开播
EXIT_GATE_NETWORK_ERROR = 11      # 门禁检测服务异常
EXIT_LOGIN_BLOCKED = 15           # 登录页或弹窗强阻断
EXIT_ROOM_MISMATCH = 18           # 确诊错房或身份未确认阻断
EXIT_DEVICE_UNAVAILABLE = 20      # 设备离线或未就绪
EXIT_RECORDING_FAILED = 30        # 录制崩溃或产物校验失败无法自愈


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
    lock_dir: Path | None = None
    overlap_duration_sec: float = 3.0  # 默认 3 秒双管线安全交叠 (ADR 0009)，0 表示串行模式
    background_mode: bool = False      # 兼容旧开关：为 True 时映射为 hidden
    window_mode: str = "foreground"    # 窗口呈现模式: "foreground" | "bottom" | "offscreen" | "hidden"
    window_pos_x: int | None = None    # 窗口 X 坐标 (可选，用于 offscreen 边缘放置或自定义摆放)
    window_pos_y: int | None = None    # 窗口 Y 坐标 (可选，用于 offscreen 边缘放置或自定义摆放)


class WorkerController:
    """控制单路录制的生命周期、分段切片、看门狗自愈与优雅退出。

    严格遵循：
    1. 权威切片状态单调演进，杜绝主循环与恢复函数状态割裂；
    2. 切片事件成对闭环（每个 started 恰好对应一个 finished 或 aborted）；
    3. 全流程统一产物完整性校验，校验失败严禁谎报成功；
    4. 实例独占锁持有至管线彻底停止，独立 run_id 任务目录杜绝文件冲突；
    5. 主播身份真实独立探测与运行期复核，逃逸开关边界明确；
    6. 四维健康解耦与运行期下播检测真正接入主循环。
    """

    def __init__(self, config: WorkerConfig, emitter: WorkerEventEmitter | None = None):
        self.config = config
        self.emitter = emitter or WorkerEventEmitter(stream=sys.stdout)
        self.stop_requested = False
        self._fatal_error = False
        self._login_blocked = False
        self._stream_ended = False
        self._room_mismatch = False
        self._last_segment_failed = False
        self._total_crash_count = 0
        self.MAX_RECOVERY_RETRIES = 3

        # 权威任务级目录隔离 (原子排他)
        self.run_id = f"run_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        self.task_output_dir = self.config.output_dir / self.run_id

        # 权威切片生命周期状态 (全局唯一事实来源)
        self.current_segment_index: int = 0
        self.current_generation_id: int = 0
        self.current_file_path: Path | None = None
        self.current_segment_started: bool = False
        self.current_segment_finalized: bool = True
        self.segment_start_mono: float = 0.0
        self.last_size_mb: float = 0.0

        # 组件实例
        self._pipeline: WorkerPipeline | None = None
        self._watchdog: StallWatchdog | None = None
        self._navigator: WorkerNavigator | None = None
        self.state_machine = WorkerStateMachine(initial_state=WorkerState.PREFLIGHT)
        self.action_dispatcher = GenerationActionDispatcher()
        self.health_monitor = HealthMonitor(
            video_freeze_threshold_sec=self.config.stall_timeout_sec,
            file_stall_threshold_sec=self.config.stall_timeout_sec,
        )
        self.stream_end_detector = StreamEndDetector(confirm_window_sec=5.0)

        # 身份状态
        self.identity_status: IdentityStatus = IdentityStatus.UNCONFIRMED
        self.has_identity_risk: bool = False
        self.target_room_id: str = ""
        self.target_uid: str = ""
        self.target_nickname: str = ""

        # 双管线无缝交叠切片状态 (ADR 0009)
        self._overlapping_started: bool = False
        self._overlapping_seg_index: int = 0
        self._overlapping_file_path: Path | None = None
        self._overlapping_start_mono: float = 0.0

    def request_stop(self):
        """外部中断信号触发平稳停止，立即推进状态机。"""
        self.stop_requested = True
        if self.state_machine.state in {WorkerState.RECORDING, WorkerState.RECOVERING}:
            try:
                self.state_machine.transition_to(WorkerState.STOPPING)
            except Exception:
                pass

    def _trigger_overlap_start_if_needed(self, current_elapsed: float) -> bool:
        """检查并触发交叠次切片预热启动 (提前 overlap_duration_sec 开启次管线)。"""
        if self._overlapping_started:
            return False

        overlap_window = self.config.overlap_duration_sec
        if overlap_window <= 0:
            return False

        # 判断是否具备双管线能力 (避免纯单管线 mock 环境崩溃)
        has_dual = isinstance(self._pipeline, DualPipelineManager) or hasattr(self._pipeline, "start_overlapping_segment")
        if not has_dual:
            return False

        target_dur = self.config.segment_duration_sec
        if overlap_window >= target_dur:
            return False

        if current_elapsed >= (target_dur - overlap_window):
            next_idx = self.current_segment_index + 1
            logger.info(
                f"当前切片已录制 {current_elapsed:.1f}s，提前 {overlap_window}s 预热启动双管线交叠切片 #{next_idx}..."
            )
            try:
                overlap_file = self._pipeline.start_overlapping_segment(next_idx)
                self._overlapping_started = True
                self._overlapping_seg_index = next_idx
                self._overlapping_file_path = overlap_file
                self._overlapping_start_mono = time.monotonic()

                # 发射新切片 started 事件
                self.emitter.emit(SegmentStartedEvent(
                    segment_index=next_idx,
                    file_path=str(overlap_file),
                    target_duration_sec=self.config.segment_duration_sec
                ))
                logger.info(f"双管线交叠切片 #{next_idx} 预热成功: {Path(overlap_file).name}，两管线并行捕获中")
                return True
            except Exception as e:
                logger.error(f"双管线预热启动异常: {e}，将回退串行轮转")
                self._overlapping_started = False
                return False
        return False

    def _finalize_overlap_transition(self) -> bool:
        """完成双管线切片交替提升与旧切片封口归档。"""
        if not self._overlapping_started:
            return False

        logger.info(f"切片 #{self.current_segment_index} 录满周期，执行双管线平滑提升交接...")
        old_primary = None
        if isinstance(self._pipeline, DualPipelineManager) or hasattr(self._pipeline, "promote_secondary"):
            old_primary = self._pipeline.promote_secondary()

        # 1. 关停旧主管线物理录制
        if old_primary and hasattr(old_primary, "stop_segment"):
            old_primary.stop_segment()

        # 2. 封口归档旧切片 (注意：此时新切片正在活跃录制中，stop_pipeline=False 避免误关新切片)
        ok = self._finalize_current_segment(is_aborted=False, stop_pipeline=False)

        # 3. 将次管线参数顺位提升为当前主管线权威状态
        self.current_segment_index = self._overlapping_seg_index
        self.current_file_path = self._overlapping_file_path
        self.current_generation_id += 1
        self.action_dispatcher.current_generation_id = self.current_generation_id
        self.current_segment_started = True
        self.current_segment_finalized = False
        self.segment_start_mono = self._overlapping_start_mono
        self._overlapping_started = False
        self.last_size_mb = 0.0
        if self._watchdog:
            self._watchdog.notify_segment_reset()

        logger.info(f"双管线交接完成！切片 #{self.current_segment_index} 成为当前活跃主切片，实现零丢帧无缝切片。")
        return ok

    def _start_new_segment(self) -> Path:
        """单调递增开启新切片并同步更新全局权威状态。"""
        # 若前一切片尚未收尾，必须先执行收尾，杜绝生命周期事件悬空
        if self.current_segment_started and not self.current_segment_finalized:
            self._finalize_current_segment(is_aborted=True, reason="previous_segment_not_finalized")

        self.current_segment_index += 1
        self.current_generation_id += 1
        self.action_dispatcher.current_generation_id = self.current_generation_id

        if not self._pipeline:
            raise RuntimeError("Pipeline 未就绪，无法开启新切片")

        cur_file = self._pipeline.start_segment(self.current_segment_index)
        self.current_file_path = cur_file
        self.current_segment_started = True
        self.current_segment_finalized = False
        self.segment_start_mono = time.monotonic()
        self.last_size_mb = 0.0
        if self._watchdog:
            self._watchdog.notify_segment_reset()

        self.emitter.emit(SegmentStartedEvent(
            segment_index=self.current_segment_index,
            file_path=str(cur_file),
            target_duration_sec=self.config.segment_duration_sec
        ))
        cur_file_name = cur_file.name if hasattr(cur_file, "name") else str(cur_file)
        logger.info(
            f"已成功开启新切片 #{self.current_segment_index} (代际 {self.current_generation_id}): {cur_file_name}"
        )
        return cur_file

    def _finalize_current_segment(
        self,
        is_aborted: bool = False,
        reason: str = "",
        allow_short_tail: bool = False,
        ffprobe_exe: Path | None = None,
        stop_pipeline: bool = True,
        is_final: bool = False,
    ) -> bool:
        """统一切片收尾出口：执行封口、完整性核验、事件发射与安全归档。

        每个已发射 segment_started 的切片，必须且只能对应一个 segment_finished 或 segment_aborted。
        校验失败坚决不得发射 segment_finished！
        """
        if not self.current_segment_started or self.current_segment_finalized:
            return True

        idx = self.current_segment_index
        raw_file = self.current_file_path or (self._pipeline.current_file_path if self._pipeline else None)
        seg_dur = int(time.monotonic() - self.segment_start_mono) if self.segment_start_mono > 0 else 0
        raw_seg_size = self._pipeline.get_current_file_size_mb() if self._pipeline else 0.0
        seg_size = float(raw_seg_size) if isinstance(raw_seg_size, (int, float)) else 0.0

        # 1. 停止物理管线 (交叠轮转时由 caller 负责停止旧管线，stop_pipeline=False 避免误关新主管线)
        if stop_pipeline and self._pipeline and self._pipeline.is_recording():
            self._pipeline.stop_segment()

        # 2. 异常中止路径
        if is_aborted or self._fatal_error or self._login_blocked or self._room_mismatch:
            abort_reason = reason or "worker_aborted_by_error"
            final_p = raw_file
            raw_path_obj = Path(raw_file) if isinstance(raw_file, str) else raw_file
            if raw_path_obj and hasattr(raw_path_obj, "exists") and raw_path_obj.exists():
                corrupted_p = raw_path_obj.with_name(raw_path_obj.stem + ".corrupted")
                try:
                    if not corrupted_p.exists():
                        raw_path_obj.replace(corrupted_p)
                    final_p = corrupted_p
                except Exception:
                    final_p = raw_path_obj

            self.emitter.emit(SegmentAbortedEvent(
                segment_index=idx,
                file_path=str(final_p or ""),
                reason=abort_reason
            ))
            self.current_segment_finalized = True
            self._last_segment_failed = True
            logger.warning(f"切片 #{idx} 已异常中止归档: reason={abort_reason}, file={final_p}")
            return False

        # 3. 正常出口路径：必须通过完整性核验与归档
        target_final_name = build_segment_filename(
            room_id=self.target_room_id or "live",
            start_epoch=int(time.time() - seg_dur),
            segment_index=idx,
            generation_id=self.current_generation_id,
        )
        target_final = self.task_output_dir / target_final_name
        raw_path_obj = Path(raw_file) if isinstance(raw_file, str) else raw_file

        if raw_path_obj and hasattr(raw_path_obj, "exists") and raw_path_obj.exists():
            ok, final_p, res_obj = finalize_segment(
                raw_path=raw_path_obj,
                target_path=target_final,
                ffprobe_exe=ffprobe_exe,
                allow_short_tail=allow_short_tail,
            )
        else:
            ok, final_p = False, raw_file or target_final
            res_obj = None

        if ok:
            # 校验成功，发射 finished 事件
            self.emitter.emit(SegmentFinishedEvent(
                segment_index=idx,
                file_path=str(final_p),
                duration_sec=seg_dur,
                file_size_mb=round(seg_size, 2),
                is_final=is_final,
            ))
            self.current_segment_finalized = True
            self._last_segment_failed = False
            logger.info(f"切片 #{idx} 校验合格并完成归档: {final_p.name} ({seg_dur}s, {seg_size:.2f}MB)")
            return True
        else:
            # 校验失败，严禁伪报 finished，如实发射 aborted 事件
            reject_msg = res_obj.reject_reason if res_obj else "file_missing_or_corrupt"
            self.emitter.emit(SegmentAbortedEvent(
                segment_index=idx,
                file_path=str(final_p),
                reason=f"validation_failed: {reject_msg}"
            ))
            self.current_segment_finalized = True
            self._last_segment_failed = True
            logger.error(f"切片 #{idx} 完整性校验未通过，标记损坏: {reject_msg}")
            return False

    def run(self) -> int:
        """主执行循环，返回进程退出码。"""
        pid = os.getpid()
        self.emitter.emit(WorkerStartedEvent(
            vmindex=self.config.vmindex,
            target=self.config.target,
            pid=pid
        ))

        # 1. 离线清洗目标直播间标识 (ADR-0011 契约：纯本地正则，零外网延迟)
        try:
            web_rid = clean_room_target(self.config.target)
        except ValueError as e:
            logger.error(f"直播间输入格式无效，无法解析: {e}")
            sys.stderr.write(f"[目标非法阻断] {e}\n")
            return EXIT_TARGET_INVALID

        self.target_room_id = web_rid
        self.target_uid = web_rid
        self.target_nickname = None

        # 3. 实例独占锁防并发抢占 (统一 VM 锁作用域，持有至全流程结束)
        instance_lock = WorkerInstanceLock(
            lock_dir=self.config.lock_dir,
            vmindex=self.config.vmindex,
            room_id=web_rid
        )
        try:
            instance_lock.acquire()
        except LockAcquisitionError as e:
            logger.error(str(e))
            return EXIT_DEVICE_UNAVAILABLE

        try:
            # 4. 创建任务独立输出目录
            self.task_output_dir.mkdir(parents=True, exist_ok=True)

            # 5. 准备运行组件与 MuMu 实例
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

            # 录播底层调优：拦截 Windows 声卡外放静音 + 禁用后台动态降频 + 灵活窗口模式 (foreground/bottom/offscreen/hidden)
            try:
                eff_mode = self.config.window_mode
                if self.config.background_mode and eff_mode == "foreground":
                    eff_mode = "hidden"
                mumu.optimize_performance_for_recording(
                    self.config.vmindex,
                    mute=True,
                    disable_dynamic_fps=True,
                    window_mode=eff_mode,
                    pos_x=self.config.window_pos_x,
                    pos_y=self.config.window_pos_y,
                )
            except Exception as e:
                logger.warning(f"设置模拟器录播环境调优异常 (不影响录制): {e}")

            # 6. 唤起直播间与白名单弹窗穿透
            self._navigator = WorkerNavigator(serial=serial, adb_exe=adb_exe)
            self._navigator.launch_room(
                room_id=self.target_room_id,
                web_rid=self.target_room_id,
                wait_sec=3,
                warmup_first=True,
            )
            self._navigator.dismiss_system_dialogs()

            bypasser = PopupBypasser(adb_client=self._navigator.adb_client)
            bypasser.detect_and_dismiss_popups()

            # 7. 独立主播身份证据核验 (严禁拿目标 ID 冒充观测结果)
            try:
                ident_res = self._navigator.detect_current_room_identity()
                det_uid = ident_res[0] if (ident_res and len(ident_res) > 0) else None
                det_nick = ident_res[1] if (ident_res and len(ident_res) > 1) else None
            except Exception:
                det_uid, det_nick = None, None

            id_res = verify_room_identity(
                target_uid=self.target_uid,
                current_uid=det_uid,
                target_nickname=self.target_nickname,
                current_nickname=det_nick,
                allow_unconfirmed=self.config.allow_unconfirmed_identity,
            )
            self.identity_status = id_res.status
            if id_res.status == IdentityStatus.UNCONFIRMED:
                self.has_identity_risk = True

            if not id_res.can_proceed:
                logger.error(f"主播身份核验未通过阻断: status={id_res.status}, reason={id_res.reason}")
                return EXIT_ROOM_MISMATCH

            # 8. 开录前置双重内容门禁校验 (Pre-flight Content Gate)
            gate_ok = self._navigator.verify_preflight_content_gate(timeout_sec=15.0)
            if not gate_ok:
                logger.error("未通过开录前置门禁：直播间未成功渲染或被登录页强阻断，拒绝盲目录制！")
                self.emitter.emit(LoginBlockedEvent(
                    reason="preflight_gate_failed",
                    vmindex=self.config.vmindex
                ))
                return EXIT_LOGIN_BLOCKED

            # 9. 初始化录制管线 (仅当切片时长大于交叠窗口时启用 DualPipelineManager ADR 0009)
            if self.config.overlap_duration_sec > 0 and self.config.segment_duration_sec > self.config.overlap_duration_sec:
                self._pipeline = DualPipelineManager(
                    serial=serial,
                    output_dir=self.task_output_dir,
                    scrcpy_exe=scrcpy_exe,
                    adb_exe=adb_exe,
                )
            else:
                self._pipeline = WorkerPipeline(
                    serial=serial,
                    output_dir=self.task_output_dir,
                    scrcpy_exe=scrcpy_exe,
                    adb_exe=adb_exe,
                )

            # 10. 初始化看门狗动作投递回调 (携带采样时代际快照)
            def on_watchdog_action(act: PipelineAction):
                self.action_dispatcher.post_action(act)

            def on_stall_action(act: str, dur: float):
                if act == "intent_refresh":
                    self.emitter.emit(StallDetectedEvent(stall_duration_sec=dur, action="intent_refresh"))
                elif act == "stall_healed":
                    self.emitter.emit(StallHealedEvent(action="intent_refresh", latency_sec=dur))

            self._watchdog = StallWatchdog(
                pipeline=self._pipeline,
                navigator=self._navigator,
                room_id=self.target_room_id,
                stall_threshold_sec=self.config.stall_timeout_sec,
                generation_provider=lambda: self.current_generation_id,
                on_post_action=on_watchdog_action,
                on_stall_action=on_stall_action,
            )

            # 11. 状态机就绪，启动首个切片
            self.state_machine.transition_to(WorkerState.RECORDING)
            self._start_new_segment()
            self._watchdog.start()

            start_mono = time.monotonic()
            last_heartbeat = 0.0
            last_stream_check = 0.0
            video_frozen_start: Optional[float] = None
            file_stall_start: Optional[float] = None
            last_known_size_mb: float = 0.0

            # 12. 非阻塞主控制循环 (Single-Writer 唯一执行主体)
            while not self.stop_requested and self.state_machine.state == WorkerState.RECORDING:
                time.sleep(0.5)
                now_mono = time.monotonic()
                total_elapsed = now_mono - start_mono
                seg_elapsed = now_mono - self.segment_start_mono

                # A. 秒级管线存活探针
                if self._pipeline and not self._pipeline.is_recording():
                    exit_code = self._pipeline.get_exit_code()
                    logger.warning(
                        f"秒级探针感知到录制进程异常退出！(segment #{self.current_segment_index}, exit_code={exit_code})"
                    )
                    recovered = self._handle_pipeline_crash(exit_code=exit_code)
                    if not recovered:
                        break
                    continue

                # B. 单写者动作排空与代际校验
                actions = self.action_dispatcher.drain_actions()
                for action in actions:
                    if action.action_type == ActionType.FATAL_STOP:
                        logger.error(f"执行代际有效动作: 致命停止 (reason: {action.reason})")
                        self._fatal_error = True
                        if self._watchdog and getattr(self._watchdog, "_visual_stall_count", 0) >= 2:
                            self._login_blocked = True
                            self.emitter.emit(LoginBlockedEvent(
                                reason="runtime_visual_login_blocked",
                                vmindex=self.config.vmindex
                            ))
                        self.request_stop()
                        break
                    elif action.action_type == ActionType.IN_PLACE_HEAL:
                        logger.info(f"主线程执行代际有效动作: 原地 Intent 重新唤起与穿透 (reason: {action.reason})")
                        if self._navigator:
                            self._navigator.dismiss_login_prompt()
                            self._navigator.dismiss_center_dialog()
                            self._navigator.refresh_current_room(self.target_room_id)
                    elif action.action_type == ActionType.RESTART_SEGMENT:
                        logger.info(f"主线程执行代际有效动作: 重启当前切片 (reason: {action.reason})")
                        self._recycle_current_segment(reason=action.reason)
                        break

                if self.stop_requested:
                    break

                # C. 四维解耦健康监测真实评估
                current_size_mb = self._pipeline.get_current_file_size_mb() if self._pipeline else 0.0
                file_delta = current_size_mb - last_known_size_mb
                last_known_size_mb = current_size_mb

                if file_delta <= 0.0:
                    if file_stall_start is None:
                        file_stall_start = now_mono
                else:
                    file_stall_start = None

                f_stall_dur = (now_mono - file_stall_start) if file_stall_start is not None else 0.0
                v_frozen_dur = (now_mono - video_frozen_start) if video_frozen_start is not None else 0.0

                signals = self.health_monitor.evaluate_signals(
                    video_motion=0.5,  # 动态由看门狗视觉探针更新
                    audio_active=None, # 未知/无专用音频分贝计
                    process_alive=self._pipeline.is_recording() if self._pipeline else False,
                    file_delta_mb=file_delta,
                    video_frozen_duration_sec=v_frozen_dur,
                    file_stall_duration_sec=f_stall_dur,
                )

                if not signals.is_healthy and signals.alert_reason == "file_write_stalled":
                    logger.warning("四维健康监测判定文件写入严重停滞，触发自愈切片重拉...")
                    self._recycle_current_segment(reason="health_monitor_file_stalled")
                    continue

                # D. 周期心跳发射 (未测得真实 FPS 时显式输出 null，真实反映生命周期)
                if now_mono - last_heartbeat >= self.config.heartbeat_interval_sec:
                    self.emitter.emit(HeartbeatEvent(
                        elapsed_sec=int(total_elapsed),
                        segment_elapsed_sec=int(seg_elapsed),
                        file_size_mb=round(current_size_mb, 2),
                        fps=None,  # 避免硬编码伪造 30.0
                        status=self.state_machine.state.value.lower()
                    ))
                    last_heartbeat = now_mono

                # F. 检查总录制时长限制
                if self.config.total_duration_sec > 0 and total_elapsed >= self.config.total_duration_sec:
                    logger.info(f"达到预设总录制时长 {self.config.total_duration_sec}s，平稳结束。")
                    break

                # G. 检查计划周期切片 (ADR 0009 双管线交叠无缝切片)
                if self.config.overlap_duration_sec > 0:
                    self._trigger_overlap_start_if_needed(seg_elapsed)

                if seg_elapsed >= self.config.segment_duration_sec:
                    if self._overlapping_started:
                        # 双管线平滑提升交接与旧段收尾
                        self._finalize_overlap_transition()
                    else:
                        logger.info(f"切片 #{self.current_segment_index} 录满 {self.config.segment_duration_sec}s，无缝轮转下一段...")
                        if self._watchdog:
                            self._watchdog.stop()
                        # 轮转必须执行完整性核验与归档
                        ok = self._finalize_current_segment(allow_short_tail=False)
                        if not ok:
                            logger.warning(f"周期切片 #{self.current_segment_index} 完整性校验失败，但按规格继续录制后续切片")
                        # 启动下一段
                        self._start_new_segment()
                        if self._watchdog:
                            self._watchdog.start()

        finally:
            # 停止看门狗
            if self._watchdog:
                try:
                    self._watchdog.stop()
                except Exception:
                    pass

            # 状态机迁往 STOPPING
            if self.state_machine.state in {WorkerState.RECORDING, WorkerState.RECOVERING}:
                try:
                    self.state_machine.transition_to(WorkerState.STOPPING)
                except Exception:
                    pass

            # 统一收尾最后一个切片 (允许正常短尾片成功归档)
            if self.current_segment_started and not self.current_segment_finalized:
                is_aborted = self._fatal_error or self._login_blocked or self._room_mismatch
                self._finalize_current_segment(
                    is_aborted=is_aborted,
                    reason="worker_stopped",
                    allow_short_tail=True,
                    is_final=True,
                )

            # 若退出时次切片已在交叠捕获中，一并安全封口并收尾归档
            if self._overlapping_started and (isinstance(self._pipeline, DualPipelineManager) or hasattr(self._pipeline, "get_secondary_pipeline")):
                try:
                    secondary = self._pipeline.get_secondary_pipeline()
                    if secondary and hasattr(secondary, "is_recording") and secondary.is_recording():
                        secondary.stop_segment()
                    raw_overlap = self._overlapping_file_path or (secondary.current_file_path if secondary else None)
                    target_final_name = build_segment_filename(
                        room_id=self.target_room_id or "live",
                        start_epoch=int(time.time()),
                        segment_index=self._overlapping_seg_index,
                        generation_id=self.current_generation_id + 1,
                    )
                    target_final = self.task_output_dir / target_final_name
                    if raw_overlap and Path(raw_overlap).exists():
                        ok, final_p, _ = finalize_segment(
                            raw_path=Path(raw_overlap),
                            target_path=target_final,
                            allow_short_tail=True,
                        )
                        if ok:
                            self.emitter.emit(SegmentFinishedEvent(
                                segment_index=self._overlapping_seg_index,
                                file_path=str(final_p),
                                duration_sec=int(time.monotonic() - self._overlapping_start_mono),
                                file_size_mb=round(secondary.get_current_file_size_mb(), 2) if hasattr(secondary, "get_current_file_size_mb") else 0.0,
                                is_final=True,
                            ))
                        else:
                            self.emitter.emit(SegmentAbortedEvent(
                                segment_index=self._overlapping_seg_index,
                                file_path=str(final_p),
                                reason="exit_short_tail_failed"
                            ))
                except Exception as e:
                    logger.warning(f"交叠次切片退出收尾异常: {e}")
                self._overlapping_started = False

            # 状态机迁往终态 STOPPED
            if self.state_machine.state == WorkerState.STOPPING:
                try:
                    self.state_machine.transition_to(WorkerState.STOPPED)
                except Exception:
                    pass

            # 实例锁严格持有至管线彻底停止并完成所有收尾后再释放
            if instance_lock:
                try:
                    instance_lock.release()
                except Exception as e:
                    logger.warning(f"释放实例锁异常: {e}")

        # 确定最终退出码
        if self._room_mismatch:
            return EXIT_ROOM_MISMATCH

        if self._login_blocked:
            return EXIT_LOGIN_BLOCKED

        if self._stream_ended:
            return EXIT_STREAM_ENDED

        if self._fatal_error or self._last_segment_failed:
            return EXIT_RECORDING_FAILED

        return EXIT_SUCCESS

    def _check_pipeline_survival(self, segment_idx: int | None = None) -> bool:
        """秒级录制进程存活与异常退出探针。"""
        if self._pipeline and not self._pipeline.is_recording():
            exit_code = self._pipeline.get_exit_code()
            logger.warning(
                f"秒级探针感知到录制进程异常退出！(segment #{self.current_segment_index}, exit_code={exit_code})"
            )
            return self._handle_pipeline_crash(segment_idx=segment_idx, exit_code=exit_code)
        return True

    def _handle_pipeline_crash(self, segment_idx: int | None = None, exit_code: int | None = None) -> bool:
        """处理录制子进程崩溃，实施有界就地自愈。"""
        if self.stop_requested or self.state_machine.state in {WorkerState.STOPPING, WorkerState.STOPPED}:
            logger.info("已处于停止状态或收到停止请求，放弃崩溃自愈拉起。")
            if self.current_segment_started and not self.current_segment_finalized:
                self._finalize_current_segment(
                    is_aborted=True,
                    reason=f"process_crash_code_{exit_code}"
                )
            return False

        if segment_idx is not None and self.current_segment_index == 0:
            self.current_segment_index = segment_idx
            self.current_segment_started = True
            self.current_segment_finalized = False

        # 1. 迁移到 RECOVERING
        if self.state_machine.state in {WorkerState.RECORDING, WorkerState.PREFLIGHT}:
            try:
                self.state_machine.transition_to(WorkerState.RECOVERING)
            except Exception:
                pass

        # 2. 为崩溃切片收尾，发射 segment_aborted
        self._finalize_current_segment(
            is_aborted=True,
            reason=f"process_crash_code_{exit_code}"
        )

        # 3. 校验重试上限（正常轮转不递增计数，只统计故障次数）
        if self._total_crash_count >= self.MAX_RECOVERY_RETRIES:
            logger.error(
                f"已达最大连续崩溃恢复上限 ({self.MAX_RECOVERY_RETRIES} 次)，果断终止录制，防止死循环！"
            )
            self._fatal_error = True
            self.request_stop()
            return False

        self._total_crash_count += 1
        logger.info(f"正在触发第 {self._total_crash_count}/{self.MAX_RECOVERY_RETRIES} 次就地自愈拉起...")

        # 4. 尝试启动新切片，单调推进序号与代际
        try:
            self._start_new_segment()
            self.state_machine.transition_to(WorkerState.RECORDING)
            return True
        except Exception as e:
            logger.error(f"就地拉起恢复失败: {e}")
            self._fatal_error = True
            self.request_stop()
            return False

    def _recycle_current_segment(self, reason: str = "watchdog_recycled"):
        """强制重启切片以恢复卡死：旧片准确中止收尾，新片单调开启。"""
        if self.stop_requested or self.state_machine.state in {WorkerState.STOPPING, WorkerState.STOPPED}:
            logger.info(f"已处于停止状态或收到停止请求，放弃看门狗重拉 (reason: {reason})。")
            if self.current_segment_started and not self.current_segment_finalized:
                self._finalize_current_segment(is_aborted=True, reason=reason)
            return

        if self.current_segment_index == 0 and self._pipeline and hasattr(self._pipeline, "current_segment_index"):
            self.current_segment_index = self._pipeline.current_segment_index
            self.current_segment_started = True
            self.current_segment_finalized = False

        logger.warning(f"看门狗正在快速重拉切片 #{self.current_segment_index} (reason: {reason})...")
        # 为旧切片发射终态 aborted 事件
        self._finalize_current_segment(is_aborted=True, reason=reason)
        # 开启新切片
        try:
            self._start_new_segment()
        except Exception as e:
            logger.error(f"重拉切片启动新段失败: {e}")
            self._fatal_error = True
            self.request_stop()

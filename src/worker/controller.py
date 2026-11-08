"""录播 Worker 顶层状态机与生命周期控制器。"""
from dataclasses import dataclass
from pathlib import Path
import logging
import json
import os
import signal
import sys
import time
from typing import Optional
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor

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
    WorkerStoppedEvent, WorkerFailedEvent, SegmentQualityWarningEvent, RoomNavigationEvent,
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
from src.worker.identity_verifier import IdentityStatus
from src.worker.room_locator import locate_target, validate_navigation_timeout
from src.worker.segment_verifier import finalize_segment, verify_segment_file
from src.worker.watchdog import StallWatchdog

logger = logging.getLogger(__name__)

# 规范退出码常量
EXIT_SUCCESS = 0
EXIT_STREAM_ENDED = 2
EXIT_TARGET_INVALID = 9
EXIT_TARGET_OFFLINE = 10          # 确诊未开播
EXIT_GATE_NETWORK_ERROR = 11      # 门禁检测服务异常
EXIT_PREFLIGHT_STALL = 16
EXIT_NAVIGATION_FAILED = 17
EXIT_LOGIN_BLOCKED = 15           # 登录页或弹窗强阻断
EXIT_ROOM_MISMATCH = 18           # 确诊错房或身份未确认阻断
EXIT_DEVICE_UNAVAILABLE = 20      # 设备离线或未就绪
EXIT_RECORDING_FAILED = 30        # 录制崩溃或产物校验失败无法自愈


@dataclass
class WorkerConfig:
    vmindex: int
    target: str
    output_dir: Path
    recording_mode: str = "continuous"  # continuous / restart（诊断旧切片）
    segment_duration_sec: int = 1800  # 默认 30 分钟切片；音画同步效果需实测
    total_duration_sec: int = 0       # 0 表示持续录制直到停止信号或下播
    heartbeat_interval_sec: float = 3.0
    stall_timeout_sec: float = 10.0
    allow_unconfirmed_identity: bool = False
    lock_dir: Path | None = None
    overlap_duration_sec: float = 0.0  # restart 诊断模式的交叠参数；continuous 必须为 0
    background_mode: bool = False      # 兼容旧开关：为 True 时映射为 hidden
    window_mode: str = "foreground"    # 窗口呈现模式: "foreground" | "bottom" | "offscreen" | "hidden"
    window_pos_x: int | None = None    # 窗口 X 坐标 (可选，用于 offscreen 边缘放置或自定义摆放)
    window_pos_y: int | None = None    # 窗口 Y 坐标 (可选，用于 offscreen 边缘放置或自定义摆放)

    target_kind: str = "auto"
    web_rid: str | None = None
    expected_anchor_uid: str | None = None
    job_id: str | None = None
    navigation_timeout_sec: float = 300.0  # 从目标定位到开录就绪的总时限
    identity_interval_sec: float = 120.0
    identity_recheck_timeout_sec: float = 30.0


class WorkerController:
    """控制单路录制的生命周期、分段切片、看门狗自愈与优雅退出。

    严格遵循：
    1. 权威切片状态单调演进，杜绝主循环与恢复函数状态割裂；
    2. 切片事件成对闭环（每个 started 恰好对应一个 finished 或 aborted）；
    3. 全流程统一产物完整性校验，校验失败严禁谎报成功；
    4. 实例独占锁持有至管线彻底停止，独立 run_id 任务目录杜绝文件冲突；
    5. 主播身份真实独立探测与运行期复核，逃逸开关边界明确；
    6. 本地视频与文件健康独立监测；开下播由调度层决定。
    """

    def __init__(self, config: WorkerConfig, emitter: WorkerEventEmitter | None = None):
        validate_navigation_timeout(config.navigation_timeout_sec)
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
        self.run_id = f"run_{int(time.time())}_{uuid.uuid4().hex}"
        self.task_output_dir = self.config.output_dir.resolve() / self.run_id
        self.emitter.set_context(run_id=self.run_id, job_id=self.config.job_id or self.run_id)
        self._failure_reason = ""
        self._had_segment_errors = False
        self._recovery_times = deque()
        self._highest_segment_index = 0
        self._overlap_disabled = False
        self._task_start_mono = time.monotonic()
        self._last_wait_heartbeat = 0.0
        self._probe_executor = None
        self._identity_future = None
        self._identity_future_generation = 0
        self._heal_future = None
        self._heal_ready_at = None
        self._last_identity_check = 0.0
        self._identity_deadline = None
        self._last_verified_index = 1
        self._last_verified_time = 0.0
        self._last_verified_mono = 0.0
        self._bad_page_count = 0
        self._resources_stopped = False

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

        if self.stop_requested or self._overlap_disabled:
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
            next_idx = self._highest_segment_index + 1
            self._highest_segment_index = next_idx
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
                self._overlap_disabled = True
                self._pipeline.get_secondary_pipeline().teardown()
                return False
        return False

    def _finalize_overlap_transition(self) -> bool:
        """完成双管线切片交替提升与旧切片封口归档。"""
        if not self._overlapping_started:
            return False

        if self.stop_requested:
            return False
        if not self._pipeline.get_secondary_pipeline().is_recording():
            self._finish_overlapping_segment(is_aborted=True, reason="secondary_exited")
            self._overlap_disabled = True
            return False
        logger.info(f"切片 #{self.current_segment_index} 录满周期，执行双管线平滑提升交接...")
        old_primary = None
        if isinstance(self._pipeline, DualPipelineManager) or hasattr(self._pipeline, "promote_secondary"):
            old_primary = self._pipeline.promote_secondary()

        # 1. 关停旧主管线物理录制
        if old_primary and hasattr(old_primary, "stop_segment"):
            old_primary.stop_segment()

        # 2. 封口归档旧切片 (注意：此时新切片正在活跃录制中，stop_pipeline=False 避免误关新切片)
        ok = self._finalize_current_segment(is_aborted=False, stop_pipeline=False, source_pipeline=old_primary)

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

        logger.info(f"双管线交接完成！切片 #{self.current_segment_index} 成为当前活跃主切片，已交接，真实媒体交叠效果须实测。")
        return ok

    def _start_new_segment(self) -> Path:
        """单调递增开启新切片并同步更新全局权威状态。"""
        # 若前一切片尚未收尾，必须先执行收尾，杜绝生命周期事件悬空
        if self.current_segment_started and not self.current_segment_finalized:
            self._finalize_current_segment(is_aborted=True, reason="previous_segment_not_finalized")

        if self.stop_requested or self.state_machine.state in {WorkerState.STOPPING, WorkerState.STOPPED}:
            raise InterruptedError("停止状态禁止启动新切片")
        self._highest_segment_index = max(self._highest_segment_index, self.current_segment_index) + 1
        self.current_segment_index = self._highest_segment_index
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
        source_pipeline=None,
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
        source = source_pipeline or self._pipeline
        raw_seg_size = source.get_current_file_size_mb() if source else 0.0
        seg_size = float(raw_seg_size) if isinstance(raw_seg_size, (int, float)) else 0.0

        # 1. 停止物理管线 (交叠轮转时由 caller 负责停止旧管线，stop_pipeline=False 避免误关新主管线)
        if stop_pipeline and self._pipeline and self._pipeline.is_recording():
            self._pipeline.stop_segment()

        # 2. 异常中止路径
        if is_aborted or self._fatal_error or self._login_blocked or self._room_mismatch:
            abort_reason = reason or "worker_aborted_by_error"
            # 逻辑中止不等同物理损坏，保留原始录像和真实路径。
            final_p = raw_file

            self.emitter.emit(SegmentAbortedEvent(
                segment_index=idx,
                file_path=str(final_p or ""),
                reason=abort_reason
            ))
            self.current_segment_finalized = True
            self._last_segment_failed = True
            self._had_segment_errors = True
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
                duration_sec=res_obj.duration,
                file_size_mb=res_obj.size_mb,
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
            self._had_segment_errors = True
            logger.error(f"切片 #{idx} 完整性校验未通过，标记损坏: {reject_msg}")
            return False

    def run(self) -> int:
        """运行并发射任务级终态；不把预检失败伪报为登录问题。"""
        code = EXIT_RECORDING_FAILED
        try:
            code = self._run_inner()
        except InterruptedError:
            code = EXIT_SUCCESS if self.stop_requested else EXIT_RECORDING_FAILED
            self._failure_reason = "stop_requested" if self.stop_requested else "startup_interrupted"
        except Exception as exc:
            logger.exception("Worker 未处理异常")
            self._fatal_error = True
            self._failure_reason = self._failure_reason or f"worker_exception: {exc}"
            self.request_stop()
        if self.state_machine.state not in {WorkerState.STOPPING, WorkerState.STOPPED}:
            self.state_machine.transition_to(WorkerState.STOPPING)
        if self.state_machine.state == WorkerState.STOPPING:
            self.state_machine.transition_to(WorkerState.STOPPED)
        if code:
            self.emitter.emit(WorkerFailedEvent(exit_code=code, reason=self._failure_reason or "worker_failed"))
        self.emitter.emit(WorkerStoppedEvent(exit_code=code,
            reason=self._failure_reason or ("stop_requested" if self.stop_requested else "completed"),
            had_segment_errors=self._had_segment_errors))
        return code

    def _run_inner(self) -> int:
        """主执行循环，返回进程退出码。"""
        pid = os.getpid()
        self.emitter.emit(WorkerStartedEvent(
            vmindex=self.config.vmindex,
            target=self.config.target,
            pid=pid
        ))

        if self.stop_requested:
            return EXIT_SUCCESS
        # 1. 离线清洗目标直播间标识 (ADR-0011 契约：纯本地正则，零外网延迟)
        try:
            web_rid = clean_room_target(self.config.target)
        except ValueError as e:
            logger.error(f"直播间输入格式无效，无法解析: {e}")
            sys.stderr.write(f"[目标非法阻断] {e}\n")
            return EXIT_TARGET_INVALID

        self.target_room_id = web_rid
        # room_id/web_rid 不是主播 UID。稳定标识必须由上层单独传入。
        self.target_uid = self.config.expected_anchor_uid
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
            self.task_output_dir.mkdir(parents=True, exist_ok=False)

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

            # 6. 先找到实际目标画面，再开录；无 UI UID 时主动读取当前房间的资料跳转证据。
            self._navigator = WorkerNavigator(serial=serial, adb_exe=adb_exe)
            self._navigator.configure_target(self.config.target_kind, self.config.web_rid)
            landing = self._locate_target_room()
            if self.stop_requested:
                return EXIT_SUCCESS
            if not landing.success:
                return self._landing_exit_code(landing.reason)

            if self.config.recording_mode == 'continuous':
                if self.config.overlap_duration_sec:
                    raise ValueError('连续分装不需要交叠；诊断交叠须显式选择 restart')
                from src.worker.continuous_session import ContinuousSession
                return ContinuousSession(self, serial, scrcpy_exe, adb_exe).run()

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


            self._watchdog = StallWatchdog(
                pipeline=self._pipeline,
                navigator=self._navigator,
                room_id=self.target_room_id,
                stall_threshold_sec=self.config.stall_timeout_sec,
                generation_provider=lambda: self.current_generation_id,
                on_post_action=on_watchdog_action,
            )

            pipes = (self._pipeline.pipe_a, self._pipeline.pipe_b) if isinstance(self._pipeline, DualPipelineManager) else (self._pipeline,)
            for pipe in pipes:
                pipe.wait_hook = self._pipeline_wait_tick
            self._probe_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="worker-probe")
            self._last_identity_check = time.monotonic()
            # 11. 状态机就绪，启动首个切片
            self.state_machine.transition_to(WorkerState.RECORDING)
            self._start_new_segment()
            self._watchdog.start()

            start_mono = time.monotonic()
            last_heartbeat = 0.0

            # 12. 非阻塞主控制循环 (Single-Writer 唯一执行主体)
            while not self.stop_requested and self.state_machine.state in {WorkerState.RECORDING, WorkerState.RECOVERING}:
                time.sleep(0.5)
                if self.stop_requested or self.state_machine.state in {WorkerState.STOPPING, WorkerState.STOPPED}:
                    break
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
                        self._failure_reason = action.reason
                        if action.reason == "login_blocked":
                            self._login_blocked = True
                            self.emitter.emit(LoginBlockedEvent(
                                reason="runtime_visual_login_blocked",
                                vmindex=self.config.vmindex
                            ))
                        self.request_stop()
                        break
                    elif action.action_type == ActionType.IN_PLACE_HEAL:
                        # 定位会打开短暂资料页，必须先封口，不能把取证操作录进直播。
                        self._correct_target_room(action.reason)
                        break
                    elif action.action_type == ActionType.RESTART_SEGMENT:
                        self._recycle_current_segment(reason=action.reason)
                        break
                if self.stop_requested:
                    break
                self._poll_identity(now_mono)
                if self.stop_requested:
                    break
                current_size_mb = self._pipeline.get_current_file_size_mb() if self._pipeline else 0.0
                snapshot = self._watchdog.snapshot() if self._watchdog else None
                video_motion = snapshot.video_motion if snapshot and snapshot.generation_id == self.current_generation_id else None
                if self._heal_ready_at is not None:
                    healthy = (snapshot is not None and snapshot.generation_id == self.current_generation_id
                               and snapshot.sampled_at >= self._heal_ready_at and snapshot.video_motion is not None
                               and snapshot.video_frozen_sec == 0 and snapshot.file_stall_sec == 0
                               and snapshot.probe_failure_sec == 0 and not snapshot.login_blocked)
                    if healthy:
                        self._heal_ready_at = None
                        self.state_machine.transition_to(WorkerState.RECORDING)
                        self.emitter.emit(StallHealedEvent(action="intent_refresh"))
                    elif time.monotonic() - self._heal_ready_at >= 15:
                        self._heal_ready_at = None
                        self._recycle_current_segment("refresh_verification_failed")
                seg_elapsed = time.monotonic() - self.segment_start_mono

                # D. 周期心跳发射 (未测得真实 FPS 时显式输出 null，真实反映生命周期)
                if now_mono - last_heartbeat >= self.config.heartbeat_interval_sec:
                    self.emitter.emit(HeartbeatEvent(
                        elapsed_sec=int(total_elapsed),
                        segment_elapsed_sec=int(seg_elapsed),
                        file_size_mb=round(current_size_mb, 2),
                        fps=None,  # 避免硬编码伪造 30.0
                        status=self.state_machine.state.value.lower(),
                        room_identity_status=self.identity_status.value.lower(),
                        video_motion=video_motion,
                    ))
                    last_heartbeat = now_mono

                # F. 检查总录制时长限制
                if self.config.total_duration_sec > 0 and total_elapsed >= self.config.total_duration_sec:
                    logger.info(f"达到预设总录制时长 {self.config.total_duration_sec}s，平稳结束。")
                    break

                if self.state_machine.state != WorkerState.RECORDING:
                    continue
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
                        # 边界复核不占恢复预算；运行中只读探针不会打开资料页。
                        if time.monotonic() - self._last_verified_mono >= self.config.identity_interval_sec:
                            landing = self._locate_target_room()
                            if not landing.success:
                                if not self.stop_requested:
                                    self._fatal_error = True
                                self.request_stop()
                                break
                        if self.stop_requested:
                            break
                        self._start_new_segment()
                        if self._watchdog:
                            self._watchdog.start()

        finally:
            self._cleanup(instance_lock)

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

        self._finish_overlapping_segment(is_aborted=True, reason="primary_crashed_during_overlap")
        if not self._consume_recovery_budget():
            return False

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

        if not self._consume_recovery_budget():
            return
        self.state_machine.transition_to(WorkerState.RECOVERING)
        self._finish_overlapping_segment(is_aborted=True, reason=reason)
        logger.warning(f"看门狗正在快速重拉切片 #{self.current_segment_index} (reason: {reason})...")
        # 为旧切片发射终态 aborted 事件
        self._finalize_current_segment(is_aborted=True, reason=reason)
        # 开启新切片
        try:
            self._start_new_segment()
            self.state_machine.transition_to(WorkerState.RECORDING)
        except Exception as e:
            logger.error(f"重拉切片启动新段失败: {e}")
            self._fatal_error = True
            self.request_stop()


    def _consume_recovery_budget(self) -> bool:
        now = time.monotonic()
        while self._recovery_times and now - self._recovery_times[0] > 300:
            self._recovery_times.popleft()
        if self._total_crash_count >= self.MAX_RECOVERY_RETRIES or len(self._recovery_times) >= 2:
            self._failure_reason = "recovery_budget_exhausted"
            self._fatal_error = True
            self.request_stop()
            return False
        self._total_crash_count += 1
        self._recovery_times.append(now)
        return True

    def _pipeline_wait_tick(self) -> bool:
        """管线短轮询期间仍响应停止、输出真实状态，不运行新的切片动作。"""
        now = time.monotonic()
        if now - self._last_wait_heartbeat >= self.config.heartbeat_interval_sec:
            self.emitter.emit(HeartbeatEvent(elapsed_sec=int(now - self._task_start_mono),
                status=self.state_machine.state.value.lower(), fps=None,
                room_identity_status=self.identity_status.value.lower()))
            self._last_wait_heartbeat = now
        return not self.stop_requested

    def _locate_target_room(self):
        navigation_started = time.monotonic()
        current_phase = ''
        if self._navigator and callable(getattr(type(self._navigator), "reset_metrics", None)):
            self._navigator.reset_metrics()
        def report(**step):
            nonlocal current_phase
            current_phase = step.get('phase', current_phase)
            elapsed = time.monotonic() - navigation_started
            evidence = step.get('evidence')
            self.emitter.emit(RoomNavigationEvent(attempt=step.get('attempt', 0),
                action=step.get('action', ''), route=step.get('route', ''), reason=step.get('reason', ''),
                observed_room_id=evidence.room_id if evidence else None,
                observed_anchor_uid=evidence.anchor_uid if evidence else None,
                evidence_source=evidence.source if evidence else 'unavailable',
                evidence_observed_at=evidence.observed_at if evidence else 0.0,
                phase=current_phase, elapsed_sec=step.get('elapsed_sec', round(elapsed, 3)),
                remaining_sec=step.get('remaining_sec', round(max(0.0, self.config.navigation_timeout_sec-elapsed), 3)),
                stage_budget_sec=step.get('stage_budget_sec', 0.0)))
        result = locate_target(self._navigator, self.target_room_id,
            target_kind=self.config.target_kind, web_rid=self.config.web_rid,
            anchor_uid=self.target_uid, timeout_sec=self.config.navigation_timeout_sec, max_attempts=3,
            stop_requested=lambda: not self._pipeline_wait_tick(), on_step=report,
            allow_unconfirmed=self.config.allow_unconfirmed_identity)
        if self._navigator and callable(getattr(type(self._navigator), "metrics_snapshot", None)):
            logger.info("NAVIGATION_PERF %s", json.dumps(
                self._navigator.metrics_snapshot(), ensure_ascii=False, sort_keys=True))
        self.identity_status = result.status
        self.emitter.set_context(room_identity_status=result.status.value.lower())
        if result.success:
            self._failure_reason = ''
            self._identity_deadline = None
            self._bad_page_count = 0
            self.target_nickname = result.evidence.nickname if result.evidence else None
            if result.status == IdentityStatus.VERIFIED:
                self._last_verified_mono = time.monotonic()
                self._last_verified_time = time.time()
                self._last_verified_index = max(1, self.current_segment_index)
            else:
                self.has_identity_risk = True
            evidence = result.evidence
            self.emitter.set_context(room_evidence_source=evidence.source if evidence else 'unavailable',
                                     room_evidence_at=evidence.observed_at if evidence else 0.0)
            report(attempt=result.attempts, action='target_ready', reason=result.reason, evidence=evidence)
        else:
            self._failure_reason = result.reason
            report(attempt=result.attempts, action='navigation_exhausted', reason=result.reason,
                   evidence=result.evidence)
        return result

    def _landing_exit_code(self, reason):
        if reason == 'login_blocked':
            self._login_blocked = True
            self.emitter.emit(LoginBlockedEvent(reason='auth_required', vmindex=self.config.vmindex))
            return EXIT_LOGIN_BLOCKED
        if reason == 'device_unavailable':
            return EXIT_DEVICE_UNAVAILABLE
        if reason in ('id_mismatch', 'room_evidence_unavailable'):
            return EXIT_ROOM_MISMATCH
        if reason in ('render_timeout', 'picture_probe_unavailable', 'overlay_not_recovered', 'page_probe_unavailable'):
            return EXIT_PREFLIGHT_STALL
        return EXIT_NAVIGATION_FAILED

    def _correct_target_room(self, reason):
        """先保存已有录像，再自动返回目标；尝试失败才报告无法恢复。"""
        if self.stop_requested:
            return False
        if not self._consume_recovery_budget():
            return False
        self.state_machine.transition_to(WorkerState.RECOVERING)
        self.emitter.emit(StallDetectedEvent(action='relocate_target'))
        if self._watchdog:
            self._watchdog.stop()
        self._finish_overlapping_segment(is_aborted=True, reason='relocating_target')
        self._finalize_current_segment(is_aborted=True, reason=reason)
        result = self._locate_target_room()
        if self.stop_requested:
            return False
        if not result.success:
            self._login_blocked = result.reason == 'login_blocked'
            self._room_mismatch = result.reason in ('id_mismatch', 'room_evidence_unavailable')
            self._fatal_error = True
            self.request_stop()
            return False
        self._start_new_segment()
        self.state_machine.transition_to(WorkerState.RECORDING)
        if self._watchdog:
            self._watchdog.start()
        self.emitter.emit(StallHealedEvent(action='relocate_target'))
        return True

    def _poll_identity(self, now: float):
        """运行期只读检查是否仍在直播页；强取证在开录/纠偏/自然切片边界执行。

        本地 UID 不显示不否定已获取的独立房间证据；证据来源和时间一直显式保留。
        仍处于 LivePlayActivity 的切房，可能需要等下一次强复核才能发现。
        """
        if not self._probe_executor or not self._navigator:
            return
        if self._identity_future is not None and self._identity_future.done():
            try:
                activity = self._identity_future.result() or ''
            except Exception:
                activity = ''
            self._identity_future = None
            if self._identity_future_generation != self.current_generation_id:
                return
            if 'LivePlayActivity' in activity:
                self._bad_page_count = 0
                self._identity_deadline = None
            else:
                self._bad_page_count += 1
                if self._identity_deadline is None:
                    self._identity_deadline = now + self.config.identity_recheck_timeout_sec
                # 页面明确离开时先导航恢复，不把异常画面继续录作目标内容。
                if activity and self._bad_page_count >= 2:
                    for idx in range(self._last_verified_index, self.current_segment_index + 1):
                        self.emitter.emit(SegmentQualityWarningEvent(segment_index=idx,
                            flag='possible_navigation_drift', suspected_start_time=self._last_verified_time))
                    self._correct_target_room('runtime_left_live_page')
                    return
        if self._identity_deadline is not None and now >= self._identity_deadline:
            self._correct_target_room('runtime_page_probe_unavailable')
            return
        if not self.stop_requested and self._identity_future is None and now - self._last_identity_check >= 5:
            self._last_identity_check = now
            self._identity_future_generation = self.current_generation_id
            self._identity_future = self._probe_executor.submit(self._navigator.get_current_focus_activity)

    def _finish_overlapping_segment(self, is_aborted=False, reason="", is_final=False):
        if not self._overlapping_started:
            return
        idx, raw = self._overlapping_seg_index, self._overlapping_file_path
        secondary = self._pipeline.get_secondary_pipeline()
        try:
            secondary.stop_segment()
            if is_aborted or not secondary.get_exit_code() == 0:
                raise RuntimeError(reason or "secondary_exited")
            target = self.task_output_dir / build_segment_filename(self.target_room_id or "live", int(time.time()), idx, self.current_generation_id + 1)
            ok, final, result = finalize_segment(Path(raw), target, allow_short_tail=True)
            if not ok:
                raise RuntimeError(result.reject_reason)
            self.emitter.emit(SegmentFinishedEvent(segment_index=idx, file_path=str(final),
                duration_sec=result.duration, file_size_mb=result.size_mb, is_final=is_final))
        except Exception as exc:
            self._had_segment_errors = True
            self._last_segment_failed = True
            self.emitter.emit(SegmentAbortedEvent(segment_index=idx, file_path=str(raw or ""),
                                                  reason=reason or f"overlap_finalize_failed: {exc}"))
        finally:
            self._overlapping_started = False

    def _cleanup(self, instance_lock):
        """完整停止两条管线后才归还锁；不会把两个切片都标为最终片。"""
        if self._resources_stopped:
            return
        if self._watchdog:
            self._watchdog.stop()
        if self.state_machine.state != WorkerState.STOPPED:
            self.state_machine.transition_to(WorkerState.STOPPING)
        overlap = self._overlapping_started
        try:
            self._finalize_current_segment(
                is_aborted=self._fatal_error or self._login_blocked or self._room_mismatch,
                reason=self._failure_reason or "worker_stopped", allow_short_tail=True,
                is_final=not overlap)
        except Exception as exc:
            self._fatal_error = True
            self._failure_reason = f"finalize_exception: {exc}"
            if self.current_segment_started and not self.current_segment_finalized:
                self.emitter.emit(SegmentAbortedEvent(segment_index=self.current_segment_index,
                    file_path=str(self.current_file_path or ""), reason=self._failure_reason))
                self.current_segment_finalized = True
        finally:
            self._finish_overlapping_segment(is_aborted=self._fatal_error or self._room_mismatch,
                                             reason=self._failure_reason, is_final=True)
            # 即使启动期间抛错、尚未发 started，也不能遗留自己创建的录制进程。
            if self._pipeline:
                self._pipeline.teardown()
            if self._probe_executor:
                self._probe_executor.shutdown(wait=False, cancel_futures=True)
            self._resources_stopped = True
            if instance_lock:
                instance_lock.release()
            if self.state_machine.state == WorkerState.STOPPING:
                self.state_machine.transition_to(WorkerState.STOPPED)

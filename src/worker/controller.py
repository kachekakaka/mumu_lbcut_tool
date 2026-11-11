"""录播 Worker 顶层状态机与生命周期控制器。"""
from dataclasses import dataclass
from pathlib import Path
import logging
import json
import os
import sys
import time
import uuid
from collections import deque

from src.mvp.dep_manager import DependencyManager
from src.mvp.mumu_client import MuMuClient
from src.worker.events import (
    WorkerEventEmitter,
    WorkerStartedEvent,
    HeartbeatEvent,
    LoginBlockedEvent,
    WorkerStoppedEvent, WorkerFailedEvent, RoomNavigationEvent,
)
from src.worker.state_machine import WorkerState, WorkerStateMachine, GenerationActionDispatcher
from src.worker.navigator import WorkerNavigator
from src.worker.resolver import clean_room_target
from src.worker.naming_lock import WorkerInstanceLock, LockAcquisitionError
from src.worker.identity_verifier import IdentityStatus
from src.worker.room_locator import locate_target, validate_navigation_timeout
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
    segment_duration_sec: int = 1800  # 默认 30 分钟切片；音画同步效果需实测
    total_duration_sec: int = 0       # 0 表示持续录制直到停止信号或下播
    heartbeat_interval_sec: float = 3.0
    stall_timeout_sec: float = 10.0
    allow_unconfirmed_identity: bool = False
    lock_dir: Path | None = None
    background_mode: bool = False      # 兼容旧开关：为 True 时映射为 hidden
    window_mode: str = "foreground"    # 窗口呈现模式: "foreground" | "bottom" | "offscreen" | "hidden"
    window_pos_x: int | None = None    # 窗口 X 坐标 (可选，用于 offscreen 边缘放置或自定义摆放)
    window_pos_y: int | None = None    # 窗口 Y 坐标 (可选，用于 offscreen 边缘放置或自定义摆放)

    target_kind: str = "auto"
    web_rid: str | None = None
    expected_anchor_uid: str | None = None
    job_id: str | None = None
    navigation_timeout_sec: float = 300.0  # 从目标定位到开录就绪的总时限
    event_log_path: Path | None = None     # 事件 stdout 断开（主控已退出）后改写到此日志


class WorkerController:
    """单路录制的任务级生命周期：目标定位、持续采集会话、恢复预算与优雅退出。

    录制期的切片、恢复和事件由 ContinuousSession 负责；本类持有它共享的状态：
    状态机、代际动作分发、事件发送器、导航器、恢复预算和任务目录。
    """

    def __init__(self, config: WorkerConfig, emitter: WorkerEventEmitter | None = None):
        validate_navigation_timeout(config.navigation_timeout_sec)
        self.config = config
        self.emitter = emitter or WorkerEventEmitter(stream=sys.stdout, fallback_path=config.event_log_path)
        self.emitter.on_stream_lost = lambda: self.request_stop("event_stream_lost")
        self.stop_requested = False
        self._stop_reason = ""
        self._fatal_error = False
        self._exit_code = 0               # 录制期确认的专用退出码（如设备离线 20）
        self._login_blocked = False
        self._last_segment_failed = False

        # 权威任务级目录隔离 (原子排他)
        self.run_id = f"run_{int(time.time())}_{uuid.uuid4().hex}"
        self.task_output_dir = self.config.output_dir.resolve() / self.run_id
        self.emitter.set_context(run_id=self.run_id, job_id=self.config.job_id or self.run_id)
        self._failure_reason = ""
        self._had_segment_errors = False
        self._recovery_times = deque()
        self._highest_segment_index = 0
        self._task_start_mono = time.monotonic()
        self._last_wait_heartbeat = 0.0
        self._resources_stopped = False

        # 当前采集会话同步过来的切片位置
        self.current_segment_index: int = 0
        self.current_generation_id: int = 0
        self.current_file_path: Path | None = None

        # 组件实例
        self._pipeline = None
        self._watchdog: StallWatchdog | None = None
        self._navigator: WorkerNavigator | None = None
        self.state_machine = WorkerStateMachine(initial_state=WorkerState.PREFLIGHT)
        self.action_dispatcher = GenerationActionDispatcher()

        # 身份状态
        self.identity_status: IdentityStatus = IdentityStatus.UNCONFIRMED
        self.target_room_id: str = ""
        self.target_uid: str = ""

    def request_stop(self, reason: str = "stop_requested"):
        """外部中断信号触发平稳停止，立即推进状态机。"""
        if not self.stop_requested:
            self._stop_reason = reason
        self.stop_requested = True
        if self.state_machine.state in {WorkerState.RECORDING, WorkerState.RECOVERING}:
            try:
                self.state_machine.transition_to(WorkerState.STOPPING)
            except Exception:
                pass

    def run(self) -> int:
        """运行并发射任务级终态；不把预检失败伪报为登录问题。"""
        code = EXIT_RECORDING_FAILED
        try:
            code = self._run_inner()
        except InterruptedError:
            code = EXIT_SUCCESS if self.stop_requested else EXIT_RECORDING_FAILED
            self._failure_reason = self._stop_reason if self.stop_requested else "startup_interrupted"
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
            reason=self._failure_reason or (self._stop_reason if self.stop_requested else "completed"),
            had_segment_errors=self._had_segment_errors))
        close = getattr(self.emitter, "close", None)
        if close:
            close()
        return code

    def _run_inner(self) -> int:
        """主执行流程，返回进程退出码。"""
        self.emitter.emit(WorkerStartedEvent(
            vmindex=self.config.vmindex,
            target=self.config.target,
            pid=os.getpid()
        ))

        if self.stop_requested:
            return EXIT_SUCCESS
        # 离线清洗目标直播间标识 (ADR-0011 契约：纯本地正则，零外网延迟)
        try:
            web_rid = clean_room_target(self.config.target)
        except ValueError as e:
            logger.error(f"直播间输入格式无效，无法解析: {e}")
            sys.stderr.write(f"[目标非法阻断] {e}\n")
            return EXIT_TARGET_INVALID

        self.target_room_id = web_rid
        # room_id/web_rid 不是主播 UID。稳定标识必须由上层单独传入。
        self.target_uid = self.config.expected_anchor_uid

        # 实例独占锁防并发抢占 (统一 VM 锁作用域，持有至全流程结束)
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
            self.task_output_dir.mkdir(parents=True, exist_ok=False)

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

            # 先找到实际目标画面，再开录；无 UI UID 时主动读取当前房间的资料跳转证据。
            self._navigator = WorkerNavigator(serial=serial, adb_exe=adb_exe)
            self._navigator.configure_target(self.config.target_kind, self.config.web_rid)
            landing = self._locate_target_room()
            if self.stop_requested:
                return EXIT_SUCCESS
            if not landing.success:
                return self._landing_exit_code(landing.reason)

            from src.worker.continuous_session import ContinuousSession
            return ContinuousSession(self, serial, scrcpy_exe, adb_exe).run()
        finally:
            self._cleanup(instance_lock)

    def _consume_recovery_budget(self) -> bool:
        """5 分钟内最多恢复 2 次；不设整场上限，频繁失败交给调度器退避后重开。"""
        now = time.monotonic()
        while self._recovery_times and now - self._recovery_times[0] > 300:
            self._recovery_times.popleft()
        if len(self._recovery_times) >= 2:
            self._failure_reason = "recovery_budget_exhausted"
            self._fatal_error = True
            self.request_stop()
            return False
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

    def _cleanup(self, instance_lock):
        """采集进程完全停止后才归还实例锁；切片终态由采集会话负责。"""
        if self._resources_stopped:
            return
        if self._watchdog:
            self._watchdog.stop()
        if self.state_machine.state != WorkerState.STOPPED:
            self.state_machine.transition_to(WorkerState.STOPPING)
        try:
            # 即使启动期间抛错，也不能遗留自己创建的录制进程。
            if self._pipeline:
                self._pipeline.teardown()
        finally:
            self._resources_stopped = True
            if instance_lock:
                instance_lock.release()
            if self.state_machine.state == WorkerState.STOPPING:
                self.state_machine.transition_to(WorkerState.STOPPED)

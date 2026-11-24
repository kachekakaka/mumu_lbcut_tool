"""单机最小调度器：持久化核对、FIFO、异步扫描、在播期间退避重试、实例同步与开机、重启对账。"""
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
from pathlib import Path
import queue
import sys
import threading
import time
import uuid

import psutil
from . import recording_archive
from src.mvp.room_resolver import ResolvedRoom, RoomStatus
from src.orchestrator.instance_pool import InstancePool
from src.orchestrator.models import WorkerLaunchSpec
from src.orchestrator.room_scanner import ObservationDispatcher
from src.orchestrator.room_query_process import QueryProcesses
from src.orchestrator.storage import NEEDS_FIX, Store, ControlError, identity_admission, validate_worker_event
from src.worker.room_locator import valid_identity_id
from src.orchestrator.worker_process import WorkerProcessHandle

logger = logging.getLogger(__name__)

# 旧主控遗留的 Worker 写 stdout 失败后会自行封口退出（心跳间隔 3 秒）；超过此时限才转人工对账。
OLD_WORKER_EXIT_WAIT_SEC = 120.0
OLD_WORKER_PROBE_INTERVAL_SEC = 2.0
LOGIN_CHECK_WINDOW_SEC = 120.0
LOGIN_REASONS = {"login_prompt_detected", "login_blocked"}


class HostLease:
    """操作系统持有的主控锁，进程退出自动释放；锁文件不删除以免换 inode 竞态。"""
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("a+b")
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("此数据目录已经有调度器运行")

    def close(self):
        if self.file.closed:
            return
        self.file.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        self.file.close()


def inspect_job_process(job: dict) -> str:
    """只核验归属，不接管或终止任何未知进程。返回 alive/absent/unknown。"""
    pid, born = job.get("pid"), job.get("process_created")
    key, journal = job["id"], job.get("event_log_path")

    def belongs(cmd):
        if not journal or not any(a == "-m" and b == "src.worker.cli" for a, b in zip(cmd, cmd[1:])):
            return False
        flags = dict(zip(cmd, cmd[1:]))
        return (flags.get("--job-id") == key and flags.get("--vmindex") == str(job["vmindex"])
                and os.path.normcase(flags.get("--event-log", "")) == os.path.normcase(journal))

    uncertain = False
    if pid:
        try:
            p = psutil.Process(pid)
            if born is None or abs(p.create_time() - born) < .01:
                if belongs(p.cmdline()):
                    return "alive"
                uncertain = born is not None
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            uncertain = True
    # 覆盖启动后、持久化 PID 前主控崩溃的窗口，以及遗留 scrcpy 子进程。
    launch_at = job.get("launch_intent_at")
    python_name = Path(sys.executable).name.casefold()
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"], ad_value=None):
        try:
            cmd = proc.info.get("cmdline")
            name = (proc.info.get("name") or "").casefold()
            if cmd is None:
                created = proc.info.get("create_time")
                if journal and name == python_name and launch_at is not None and (
                    created is None or abs(created - launch_at) <= 2
                ):
                    uncertain = True
                continue
            if belongs(cmd):
                return "alive"
            if name.startswith("scrcpy") and any(
                arg.startswith("--record=" + r"\\.\pipe\mumu_capture_" + key + "_") for arg in cmd
            ):
                return "alive"
            if name.startswith(("ffmpeg", "ffprobe")) and any(
                key in Path(arg).parts or arg == f"comment=mumu_capture_{key}"
                for arg in cmd[1:] if not arg.startswith("-")
            ):
                return "alive"
        except psutil.Error:
            continue
    return "unknown" if uncertain else "absent"


class Scheduler:
    def __init__(self, store: Store, output_dir: Path, instance_indices: list[int] | None = None,
                 pool=None, query_executor=None, handle_factory=WorkerProcessHandle,
                 clock=time.time, process_probe=inspect_job_process,
                 scan_interval_sec: float = 30.0,
                 ended_interval_sec: float = 10.0, auto_scan: bool = True,
                 segment_duration_sec: int = 1800, navigation_timeout_sec: float = 300.0,
                 require_local_tools: bool = False):
        from src.worker.room_locator import validate_navigation_timeout
        validate_navigation_timeout(navigation_timeout_sec)
        self.navigation_timeout = navigation_timeout_sec
        self.require_local_tools = require_local_tools
        if instance_indices is not None and any(i < 0 for i in instance_indices):
            raise ValueError("实例索引不能为负数")
        self.store, self.clock = store, clock
        self.output_dir = Path(output_dir).resolve()
        self.preset = set(instance_indices) if instance_indices is not None else None
        self.scan_interval = max(.1, scan_interval_sec)
        self.ended_interval = ended_interval_sec
        self.auto_scan, self.segment_duration = auto_scan, segment_duration_sec
        self.pool = pool or InstancePool()
        self.handle_factory, self.process_probe = handle_factory, process_probe
        self._lease = HostLease(store.path.with_suffix(".host.lock"))
        pool_cap = max(8, len(self.preset or ()))
        self._queries = ThreadPoolExecutor(max_workers=2 * pool_cap + 2, thread_name_prefix="scheduler-query")
        self._stoppers = ThreadPoolExecutor(max_workers=pool_cap, thread_name_prefix="scheduler-stop")
        self._events: queue.Queue = queue.Queue(maxsize=8192)
        self._event_pending = None
        self._handles, self._closing, self._verifying = {}, {}, {}
        self._replays = {}
        self._replay_contexts = {}
        self._scan_requested = []
        self._next_scan = 0.0
        self._refresh_future, self._refresh_started = None, 0.0
        self._force_refresh = False
        self._boots = {}
        self._boot_stop = threading.Event()
        self._waiting_exit = {}
        self._login_checks = {}
        self._title_queries = {}
        self._next_waiting_probe = 0.0
        self._shutdown = False
        self._closed = False
        self.room_queries = None
        try:
            self.store.interrupt_commands(self.clock())
            self.room_queries = query_executor or QueryProcesses()
            self.observations = ObservationDispatcher(store, self.room_queries, clock)
            with self.store.transaction() as db:
                # 已派发的历史请求不能变成重启后的又一次开录查询。
                db.execute("""UPDATE recording_sessions SET title_step=CASE
                    WHEN title_step='fallback_query' THEN 'done' ELSE 'opening_done' END
                    WHERE title_step IN ('opening_wait','opening_query','fallback_query')""")
            self.store.sync_instances(self.pool.refresh(), self.clock(), preset=self.preset, clear_booting=True)
            self.reconcile()
        except BaseException:
            try:
                if self.room_queries:
                    self.room_queries.close()
            finally:
                self._queries.shutdown(wait=False, cancel_futures=True)
                self._stoppers.shutdown(wait=False, cancel_futures=True)
                self._lease.close()
            raise

    def _drain_events(self):
        for _ in range(2000):
            if self._event_pending is None:
                try:
                    self._event_pending = self._events.get_nowait()
                except queue.Empty:
                    break
            job_id, event = self._event_pending
            if isinstance(event, str):
                if event:
                    self._replays[job_id] = 0
                    self._replay_contexts[job_id] = event
                else:
                    job = self.store.row("SELECT * FROM jobs WHERE id=?", (job_id,))
                    if job["recording_root"] and self.process_probe(job) != "absent":
                        self.store.mark_waiting_exit(job_id, self.clock(), quarantine=True,
                            reason="worker_recording_process_still_present")
                        self._forget_worker(job_id)
                        self._check_old_worker(self.store.row("SELECT * FROM jobs WHERE id=?", (job_id,)))
                    else:
                        recording_archive.recover_unfinished(self.store, job_id, self.clock())
                        self.store.finish_worker(job_id, self.clock(), self._handles[job_id].poll_exit_code())
                        self._forget_worker(job_id)
                self._event_pending = None
                continue
            # 数据库失败时保留当前事件，不静默丢弃；主循环异常会停止本主控创建的 Worker。
            try:
                validate_worker_event(job_id, event)
            except ValueError as exc:
                logger.warning("任务 %s 收到无效 Worker 事件：%s", job_id, exc)
                self._event_pending = None
                continue
            self.store.record_event(job_id, event, self.clock())
            login_notice = (event.get("event") == "stall_detected" or
                            (event.get("event") == "capture_startup" and event.get("action") == "rejected"))
            if login_notice and event.get("reason") in LOGIN_REASONS:
                # 同一次恢复重复通知不延长窗口，也不产生无界查询队列。
                self._login_checks.setdefault(job_id, (self.clock(), self.clock() + LOGIN_CHECK_WINDOW_SEC, uuid.uuid4().hex))
            elif (event.get("event") in ("stall_healed", "worker_stopped") or
                  (event.get("event") == "capture_session" and event.get("action") == "started")):
                self._login_checks.pop(job_id, None)
            self._event_pending = None

    def _collect(self):
        self._drain_events()
        for job_id, context in list(self._replay_contexts.items()):
            job = self.store.row("SELECT * FROM jobs WHERE id=?", (job_id,))
            if self.process_probe(job) == "absent":
                self._finish_orphan(job, self._handles[job_id].poll_exit_code(), context)
            else:
                self.store.mark_waiting_exit(job_id, self.clock(), quarantine=True,
                                             reason="worker_recording_process_still_present")
                self._forget_worker(job_id)
        for job_id, future in list(self._closing.items()):
            if future is None or not future.done():
                continue
            handle = self._handles[job_id]
            reason = ""
            try:
                future.result()
                complete = handle.events_complete
            except Exception as exc:
                reason = f"event_collection_incomplete: {exc}"
                complete = False
            if handle.is_alive() or (not complete and not getattr(handle, "delivery_closed", True)):
                self.store.mark_waiting_exit(job_id, self.clock(), quarantine=True,
                    reason=reason or "worker_delivery_still_open")
                self._closing[job_id] = self._stoppers.submit(handle.stop, 60.0)
                continue
            if handle.forced_stop or not complete:
                reason = reason or ("worker_forced_stop" if handle.forced_stop else "worker_delivery_incomplete")
            try:
                self._events.put_nowait((job_id, reason))
            except queue.Full:
                continue
            self._closing[job_id] = None
        for job_id, handle in list(self._handles.items()):
            if job_id in self._closing:
                continue
            if handle.event_errors:
                self.store.request_stop(job_id, "event_receiver_failed")
                self._closing[job_id] = self._stoppers.submit(handle.stop, 60.0)
            elif not handle.is_alive():
                self._closing[job_id] = self._stoppers.submit(handle.wait, 10.0)

    def _forget_worker(self, job_id):
        job = self.store.row("SELECT exit_code FROM jobs WHERE id=?", (job_id,))
        if job and job["exit_code"] == 20:
            self._force_refresh = True
        self._closing.pop(job_id, None)
        self._handles.pop(job_id, None)
        self._replays.pop(job_id, None)
        self._replay_contexts.pop(job_id, None)

    def _stop_job(self, job_id: str, reason: str):
        self.store.request_stop(job_id, reason)
        if job_id in self._handles and job_id not in self._closing:
            self._closing[job_id] = self._stoppers.submit(self._handles[job_id].stop, 60.0)

    def _identity_reason(self, target):
        return identity_admission(target, self.clock(), max(60, self.scan_interval * 2))

    def _pending_needs_refresh(self, target_id):
        job = self.store.active_job(target_id)
        target = self.store.row('SELECT * FROM targets WHERE id=?', (target_id,))
        return bool(job and job['status'] == 'pending' and not job['pid'] and target
                    and self._identity_reason(target))

    def _commands(self):
        for command in self.store.rows("SELECT * FROM commands WHERE status='pending' ORDER BY created_at,id LIMIT 30"):
            if self._shutdown:
                break
            kind, subject = command["kind"], command["subject"]
            try:
                with self.store.transaction() as db:
                    db.execute("UPDATE commands SET status='running',started_at=? WHERE id=?", (self.clock(), command["id"]))
                    if kind == "record":
                        self.store.validate_subject(kind, subject)
                        result = self.store.existing_record(subject)
                        if result is None or self._pending_needs_refresh(subject):
                            target = self.store.row("SELECT * FROM targets WHERE id=?", (subject,))
                            if not self.observations.dispatch("manual", target, command["id"]):
                                raise ControlError("query_busy", "手动查询繁忙，请稍后重试")
                            continue
                    else:
                        result = self.store.apply_control(command, self.clock())
                    self.store.complete_command(command["id"], result, now=self.clock())
            except ControlError as exc:
                self.store.complete_command(command["id"], error_code=exc.code, message=str(exc), now=self.clock())

    def _query_results(self):
        for source, meta, subject, result in self.observations.collect():
            if source == "verify":
                self._verifying.pop(subject, None)
            if source == "manual":
                self._manual_result(meta, subject, result)
                continue
            if source == "login":
                self._login_result(meta, subject[0], subject[1], result)
                continue
            with self.store.transaction():
                job = self.store.row("SELECT * FROM jobs WHERE id=?", (subject,)) if source == "verify" else None
                if source == "verify" and (self._shutdown or not job or job["status"] != "starting" or job["event_log_path"]
                                           or job["session_key"] != meta["session_key"]):
                    continue
                accepted = self.store.apply_observation(meta, result.room, self.ended_interval)
            if source != "verify" or not accepted:
                continue
            target = self.store.row("SELECT * FROM targets WHERE id=?", (meta["target_id"],))
            if not target["enabled"] or target["session_key"] != job["session_key"]:
                self.store.request_stop(subject, "live_session_changed")
            elif result.room.status != RoomStatus.LIVE:
                self.store.defer_start(subject, self.clock(), "prelaunch_not_confirmed_live")
            else:
                self._launch(job, target, result.room, meta["observed_at"])

    def _session_titles(self):
        now = self.clock()
        for key, (step, future, deadline) in list(self._title_queries.items()):
            if not future.done() and now < deadline:
                continue
            del self._title_queries[key]
            room = None
            if future.done():
                try:
                    result = future.result()
                    room = result.room if not result.error and now < deadline else None
                except Exception:
                    logger.warning("本场标题查询失败", exc_info=True)
            title = room.title.strip() if room and isinstance(room.title, str) else ""
            with self.store.transaction() as db:
                if title and room.status in (RoomStatus.LIVE, RoomStatus.ENDED):
                    db.execute("""UPDATE recording_sessions SET title=?,title_observed_at=?,title_step='done'
                        WHERE target_id=? AND session_key=? AND room_id=? AND anchor_uid=? AND title IS NULL
                          AND title_step=? AND status IN ('recording','checking','only_record','review')
                          AND EXISTS (SELECT 1 FROM targets WHERE id=? AND session_key=?)""",
                               (title, now, *key, room.room_id, room.anchor_uid, step, *key))
                db.execute("""UPDATE recording_sessions SET title_step=? WHERE target_id=? AND session_key=?
                    AND title_step=?""", ("opening_done" if step == "opening_query" else "done", *key, step))

        with self.store.transaction() as db:
            db.execute("""UPDATE recording_sessions SET title_step='opening_done'
                WHERE title_step='opening_wait' AND title_deadline<=?""", (now,))
            db.execute("""UPDATE recording_sessions SET title_step='fallback_wait',title_deadline=?
                WHERE status IN ('checking','only_record','review') AND title_step='opening_done'""", (now + 30,))
            db.execute("""UPDATE recording_sessions SET title_step='done'
                WHERE title_step='fallback_wait' AND title_deadline<=?""", (now,))
        for row in self.store.rows("""SELECT target_id,session_key,web_rid,title_step,title_deadline FROM recording_sessions
            WHERE title_step IN ('opening_wait','fallback_wait') ORDER BY title_step DESC,started_at LIMIT 20"""):
            if not self.room_queries.available("title"):
                break
            key = row["target_id"], row["session_key"]
            step = row["title_step"].replace("_wait", "_query")
            with self.store.transaction() as db:
                db.execute("UPDATE recording_sessions SET title_step=? WHERE target_id=? AND session_key=?", (step, *key))
            future = self.room_queries.submit("title", row["web_rid"])
            if future is not None:
                self._title_queries[key] = step, future, row["title_deadline"]
            else:
                with self.store.transaction() as db:
                    db.execute("UPDATE recording_sessions SET title_step=? WHERE target_id=? AND session_key=?",
                               ("opening_done" if step == "opening_query" else "done", *key))

    def _login_result(self, meta, job_id, token, result):
        check = self._login_checks.get(job_id)
        job = self.store.row("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not check or token != check[2] or self.clock() >= check[1] or not job or job["status"] not in ("starting", "running"):
            return
        with self.store.transaction():
            accepted = self.store.apply_observation(meta, result.room, self.ended_interval)
            target = self.store.row("SELECT * FROM targets WHERE id=?", (meta["target_id"],))
            reason = ("observation_superseded" if not accepted else
                      "anchor_offline" if target["trusted_status"] == "ended" else
                      "anchor_ended_unconfirmed" if result.room.status == RoomStatus.ENDED else
                      (self._identity_reason(target) or "anchor_live") if result.room.status == RoomStatus.LIVE else "live_status_unknown")
            self.store.record_event(job_id, dict(event="login_status_checked", timestamp=self.clock(),
                reason=reason, observed_status=result.room.status.value, accepted=accepted,
                query_error=result.error), self.clock())
        if accepted and target["trusted_status"] == "ended":
            self._login_checks.pop(job_id, None)
        else:
            self._login_checks[job_id] = (self.clock() + max(1.0, self.ended_interval), check[1], token)

    def _check_login_status(self):
        """登录页不是下播证据；复用有序观测，以专用槽位及时核对。"""
        now = self.clock()
        for job_id, (due, deadline, token) in list(self._login_checks.items()):
            job = self.store.row("SELECT * FROM jobs WHERE id=?", (job_id,))
            target = self.store.row("SELECT * FROM targets WHERE id=?", (job["target_id"],)) if job else None
            if (now >= deadline or not job or job["status"] not in ("starting", "running")
                    or not target or not target["enabled"] or target["session_key"] != job["session_key"]
                    or target["trusted_status"] == "ended"):
                self._login_checks.pop(job_id, None)
                continue
            if now >= due:
                self.observations.dispatch("login", target, (job_id, token))

    def _manual_result(self, meta, command_id, result):
        try:
            with self.store.transaction() as db:
                cmd = self.store.row("SELECT * FROM commands WHERE id=?", (command_id,))
                if not cmd or cmd["status"] != "running":
                    return
                t = self.store.row("SELECT * FROM targets WHERE id=?", (meta["target_id"],))
                existing = self.store.existing_record(t["id"]) if t and t["enabled"] else None
                if existing and self.store.active_job(t["id"])["session_key"] == t["session_key"] and not self._pending_needs_refresh(t['id']):
                    self.store.complete_command(command_id, existing, now=self.clock())
                    return
                if not t or not t["enabled"] or t["control_revision"] != meta["revision"] or t["session_key"] != meta["session_key"]:
                    raise ControlError("target_changed", "主播控制状态已变化，请刷新后重试")
                if not self.store.apply_observation(meta, result.room, self.ended_interval):
                    raise ControlError("target_changed", "已有更新的开播观测，请重试")
                # 查询失败仍可保存有序 UNKNOWN/ENDED 观测，但不改变原有控制标记。
                error = result.error or ("target_not_live" if result.room.status == RoomStatus.ENDED else "live_status_unknown" if result.room.status != RoomStatus.LIVE else None)
                if error:
                    self.store.complete_command(command_id, error_code=error, message={"target_not_live":"主播当前未开播", "live_query_timeout":"开播查询超时", "live_status_unknown":"暂时无法确认开播"}.get(error, "开播查询失败"), now=self.clock())
                    return
                t = self.store.row("SELECT * FROM targets WHERE id=?", (t["id"],))
                if not t["enabled"]:
                    self.store.complete_command(command_id, error_code="target_changed", message="解析结果与已有监控目标重复", now=self.clock())
                    return
                active = self.store.active_job(t['id'])
                if active and active['session_key'] != t['session_key']:
                    self._stop_job(active['id'], 'live_session_changed')
                    self.store.complete_command(command_id, error_code='live_session_changed',
                        message='已换场，旧任务正在结束，请重新查询开录', now=self.clock())
                    return
                admission = self._identity_reason(t)
                if admission:
                    if active and active['status'] == 'pending':
                        db.execute('UPDATE jobs SET error_reason=? WHERE id=?', (admission, active['id']))
                    self.store.complete_command(command_id, error_code=admission,
                        message=('主播在播，但身份标识未齐，等待重新查询后开录' if admission == 'target_identity_incomplete'
                                 else '身份信息已过期，等待重新查询后开录'), now=self.clock())
                    return
                db.execute("UPDATE targets SET record_session_key=session_key,suppressed_session=NULL,note=CASE WHEN note LIKE ? THEN NULL ELSE note END,attempts=0,retry_at=0,updated_at=?,control_revision=control_revision+1 WHERE id=?", (NEEDS_FIX + "%", self.clock(), t["id"]))
                job_id = self.store.create_pending(t["id"], self.clock())
                self.store.complete_command(command_id, dict(target_id=t["id"], job_id=job_id, outcome="queued"), now=self.clock())
        except ControlError as exc:
            self.store.complete_command(command_id, error_code=exc.code, message=str(exc), now=self.clock())

    def _scan(self):
        if self._shutdown:
            return
        now = self.clock()
        if self.auto_scan and not self._scan_requested and now >= self._next_scan and not self._shutdown:
            self._scan_requested = [t["id"] for t in self.store.rows("SELECT id FROM targets WHERE enabled=1 ORDER BY created_at,id")]
            self._next_scan = now + self.scan_interval
            if self._refresh_future is None:
                self._refresh_started = now
                self._refresh_future = self._queries.submit(self.pool.refresh)
        for target_id in list(self._scan_requested):
            target = self.store.row("SELECT * FROM targets WHERE id=? AND enabled=1", (target_id,))
            if ("login", target_id) in self.observations.pending:
                # 本轮已由优先复核接手，避免紧随其后的周期查询使优先结果过期。
                self._scan_requested.remove(target_id)
                continue
            if not target or self.observations.dispatch("scan", target):
                self._scan_requested.remove(target_id)
            elif not self.room_queries.available("scan"):
                break

    def _collect_instance_futures(self, futures: dict, finish_fn, label: str):
        for vmindex, future in list(futures.items()):
            if not future.done():
                continue
            futures.pop(vmindex)
            try:
                ok = bool(future.result())
            except Exception:
                logger.exception("实例 %s %s异常", vmindex, label)
                ok = False
            finish_fn(vmindex, ok, self.clock())

    def _instances(self):
        """收取实例刷新和自动开机结果，按需刷新掉线实例状态。"""
        if self._refresh_future and self._refresh_future.done():
            try:
                slots = self._refresh_future.result()
            except Exception:
                logger.exception("实例查询失败，保留原状态")
            else:
                self.store.sync_instances(slots, self._refresh_started)
            self._refresh_future = None
        self._collect_instance_futures(self._boots, self.store.finish_boot, "自动开机")
        if self._shutdown:
            return
        if self._force_refresh and self._refresh_future is None:
            self._force_refresh = False
            self._refresh_started = self.clock()
            self._refresh_future = self._queries.submit(self.pool.refresh)

    def _reconcile_targets(self):
        now = self.clock()
        for target in self.store.rows("""WITH candidates AS (
                SELECT id FROM targets WHERE enabled=1
                UNION ALL
                SELECT t.id FROM jobs active JOIN targets t ON t.id=active.target_id
                    WHERE active.status IN ('pending','starting','running','stopping','needs_reconcile')
                      AND t.enabled=0
            )
            SELECT t.*,j.id AS active_id,j.status AS active_status,
            j.session_key AS active_session,j.stop_reason AS active_stop
            FROM candidates c JOIN targets t ON t.id=c.id
            LEFT JOIN jobs j ON j.target_id=t.id
                AND j.status IN ('pending','starting','running','stopping','needs_reconcile')
            ORDER BY t.created_at,t.rowid"""):
            active = target["active_id"]
            if active:
                if target["active_status"] == "needs_reconcile":
                    continue
                if target["active_status"] == "stopping":
                    if active in self._handles and active not in self._closing:
                        self._stop_job(active, target["active_stop"] or "manual_stop")
                elif not target["enabled"]:
                    self._stop_job(active, "target_removed")
                elif target["trusted_status"] == "ended":
                    self._stop_job(active, "anchor_offline")
                elif target["session_key"] != target["active_session"]:
                    self._stop_job(active, "live_session_changed")
                continue
            if self._shutdown or not target["enabled"]:
                continue
            ready = (target["trusted_status"] == "live" and target["observed_status"] == "live"
                     and self._identity_reason(target) is None
                     and (target["auto_record"] or (target["record_session_key"] and target["record_session_key"] == target["session_key"]))
                     and not (target["suppressed_session"] and target["suppressed_session"] == target["session_key"])
                     and not (target["note"] or "").startswith(NEEDS_FIX) and now >= target["retry_at"])
            if ready:
                self.store.create_pending(target["id"], now)

    def _dispatch(self):
        now = self.clock()
        for job in self.store.rows("SELECT * FROM jobs WHERE status='stopping' AND pid IS NULL AND event_log_path IS NULL"):
            if job["id"] not in self._handles and job["id"] not in self._waiting_exit and job["id"] not in self._replays:
                self._verifying.pop(job["id"], None)
                self.store.finish_job(job["id"], 0, now, True, "cancelled_before_launch")
        if self._shutdown:
            return
        # 被新观测淘汰的启动复核留在 starting，下轮重新获取有界查询槽位。
        for job in self.store.rows("SELECT * FROM jobs WHERE status='starting' AND pid IS NULL AND event_log_path IS NULL ORDER BY created_at,rowid"):
            if job["id"] not in self._verifying and job["id"] not in self._handles:
                target = self.store.row("SELECT * FROM targets WHERE id=?", (job["target_id"],))
                reason = self._identity_reason(target)
                if reason in {'target_identity_incomplete', 'target_identity_stale'}:
                    self.store.defer_start(job['id'], now, reason)
                    continue
                if self.observations.dispatch("verify", target, job["id"]):
                    self._verifying[job["id"]] = True
        for job in self.store.rows("SELECT * FROM jobs WHERE status='pending' ORDER BY created_at,rowid"):
            target = self.store.row("SELECT * FROM targets WHERE id=?", (job["target_id"],))
            reason = self._identity_reason(target)
            if reason:
                with self.store.transaction() as db:
                    db.execute('UPDATE jobs SET error_reason=? WHERE id=?', (reason, job['id']))
                continue
            if now < target["retry_at"]:
                continue
            slot = self.store.row("SELECT * FROM instances WHERE enabled=1 AND status='idle' AND job_id IS NULL ORDER BY vmindex")
            if not slot:
                break
            if self.store.claim(job["id"], slot["vmindex"], now):
                if self.observations.dispatch("verify", target, job["id"]):
                    self._verifying[job["id"]] = True
        if not self._boots:
            vmindex = self.store.claim_boot(now, max(60, self.scan_interval * 2))
            if vmindex is not None:
                self._boots[vmindex] = self._queries.submit(self.pool.boot_instance, vmindex, self._boot_stop)

    def _launch(self, job: dict, target: dict, room: ResolvedRoom, observed_at=None):
        if self._shutdown:
            return
        job_id = job["id"]
        reason = self._identity_reason(target)
        if not valid_identity_id(room.room_id) or not valid_identity_id(room.anchor_uid):
            reason = 'target_identity_incomplete'
        if reason:
            self.store.defer_start(job_id, self.clock(), reason)
            return
        journal = self.store.path.parent / "events" / f"{job_id}.ndjson"
        session = self.store.row("""SELECT recording_root FROM recording_sessions
            WHERE target_id=? AND session_key=?""", (job["target_id"], job["session_key"]))
        root = Path(session["recording_root"]) if session and session["recording_root"] else self.output_dir
        staging = root / "_录制中"
        if not recording_archive.stable(staging) or (session and session["recording_root"] and not root.is_dir()):
            self.store.defer_start(job_id, self.clock(), f"原录像根不可用：{root}")
            return
        try:
            staging.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.store.defer_start(job_id, self.clock(), f"无法创建录像暂存目录 {staging}：{exc}")
            return
        spec = WorkerLaunchSpec(vmindex=job["vmindex"], target=room.room_id,
            output_dir=staging / job_id, target_kind="room_id",
            web_rid=room.web_rid, expected_anchor_uid=getattr(room, "anchor_uid", None),
            allow_unconfirmed_identity=bool(target["allow_unconfirmed"]), job_id=job_id,
            event_log_path=journal, log_path=journal.with_suffix(".log"), segment_duration_sec=self.segment_duration,
            navigation_timeout_sec=self.navigation_timeout, require_local_tools=self.require_local_tools)
        # 先保存启动意图和产物关联，覆盖 Popen 后写 PID 前的异常窗口。
        with self.store.transaction() as db:
            saved = db.execute("""UPDATE jobs SET room_id=?,event_log_path=?,launch_intent_at=?,recording_root=?
                WHERE id=? AND status='starting' AND event_log_path IS NULL""",
                               (room.room_id, str(journal), self.clock(), str(root), job_id))
            if not saved.rowcount:
                return
            db.execute("INSERT INTO recording_admissions VALUES(?,?,?,?,?,?)",
                       (job_id, room.room_id, room.anchor_uid, room.nickname, room.web_rid,
                        self.clock() if observed_at is None else observed_at))
        handle = self.handle_factory(spec=spec, on_event=lambda event, key=job_id: self._events.put_nowait((key, event)))
        self._handles[job_id] = handle
        try:
            handle.start()
        except Exception as exc:
            logger.exception("Worker 启动失败")
            if handle.is_alive():
                raise
            else:
                self._handles.pop(job_id, None)
                # 启动失败计入失败次数，使退避间隔逐次加长。
                with self.store.transaction() as db:
                    db.execute("UPDATE targets SET attempts=attempts+1 WHERE id=?", (target["id"],))
                self.store.finish_job(job_id, 30, self.clock(), True, f"launch_failed: {exc}")
        else:
            # 写 PID 失败必须进入服务的存储故障收尾，句柄仍归本主控。
            self.store.mark_launched(job_id, handle, str(journal), self.clock())

    def reconcile(self, selected_job: str | None = None):
        sql = "SELECT * FROM jobs WHERE status IN ('starting','running','stopping','needs_reconcile')"
        params = ()
        if selected_job:
            sql += " AND id=?"
            params = (selected_job,)
        for job in self.store.rows(sql, params):
            job_id = job["id"]
            if job_id in self._handles or job_id in self._verifying or job_id in self._replays:
                continue
            if not job["event_log_path"] and not job["pid"] and job["status"] in ("starting", "stopping"):
                self.store.finish_job(job_id, 0, self.clock(), True, "cancelled_before_launch")
                continue
            self._check_old_worker(job)

    def _check_old_worker(self, job):
        job_id, now = job["id"], self.clock()
        status = self.process_probe(job)
        deadline = job["old_worker_deadline"]
        if status == "absent":
            self._waiting_exit.pop(job_id, None)
            self.store.mark_waiting_exit(job_id, now, reason="replaying_worker_events")
            self._replays[job_id] = 0
        elif status == "alive" and (deadline is None or now < deadline):
            self._waiting_exit[job_id] = deadline if deadline is not None else now + OLD_WORKER_EXIT_WAIT_SEC
            if job["error_reason"] != "waiting_old_worker_exit":
                self.store.mark_waiting_exit(job_id, now, deadline=self._waiting_exit[job_id])
        else:
            self._waiting_exit.pop(job_id, None)
            reason = "old_worker_did_not_exit" if status == "alive" else "unowned_process_requires_reconciliation"
            if job["error_reason"] != reason:
                self.store.mark_waiting_exit(job_id, now, reason=reason, quarantine=True)

    def _watch_old_workers(self):
        now = self.clock()
        if not self._waiting_exit or now < self._next_waiting_probe:
            return
        self._next_waiting_probe = now + OLD_WORKER_PROBE_INTERVAL_SEC
        for job_id in list(self._waiting_exit):
            job = self.store.row("SELECT * FROM jobs WHERE id=?", (job_id,))
            if not job or job["status"] != "stopping":
                self._waiting_exit.pop(job_id)
                continue
            self._check_old_worker(job)

    def _finish_orphan(self, job, process_code=None, context=""):
        """每轮有界补账。坏行按物理行跳过，提交后才推进读取位置。"""
        job_id = job["id"]
        if job["recording_root"] and not Path(job["recording_root"]).is_dir():
            reason = f"原录像根不可用：{job['recording_root']}"
            if job["error_reason"] != reason:
                self.store.mark_waiting_exit(job_id, self.clock(), reason=reason, quarantine=True)
            return
        offset = self._replays.get(job_id, 0)
        path = Path(job["event_log_path"]) if job["event_log_path"] else None
        try:
            if path is None:
                raise FileNotFoundError
            with path.open("rb") as stream:
                stream.seek(offset)
                for _ in range(1000):
                    line = stream.readline()
                    if not line:
                        break
                    try:
                        if not line.endswith(b"\n"):
                            raise ValueError("残行")
                        event = json.loads(line.decode("utf-8"))
                        validate_worker_event(job_id, event)
                    except ValueError as exc:
                        logger.warning("任务 %s 日志字节 %s 无效：%s", job_id, offset, exc)
                    else:
                        self.store.record_event(job_id, event, self.clock(), replay=True)
                    self._replays[job_id] = offset = stream.tell()
                else:
                    return
        except FileNotFoundError:
            logger.warning("任务 %s 事件日志缺失：%s", job_id, path)
            context = "事件日志缺失" + (f"；{context}" if context else "")
        except OSError as exc:
            logger.warning("任务 %s 事件日志暂不可读：%s", job_id, exc)
            self._replays[job_id] = offset
            return
        if job["recording_root"] and self.process_probe(job) != "absent":
            self._replays.pop(job_id, None)
            self._check_old_worker(job)
            return
        recording_archive.recover_unfinished(self.store, job_id, self.clock())
        self.store.finish_worker(job_id, self.clock(), process_code, context)
        self._forget_worker(job_id)

    def tick(self):
        self._collect()
        recording_archive.publish(self.store)
        if self._shutdown:
            return
        self._commands()
        self._query_results()
        self._check_login_status()
        for job in self.store.rows("SELECT id FROM jobs WHERE reconcile_requested=1"):
            self.reconcile(job["id"])
            with self.store.transaction() as db:
                db.execute("UPDATE jobs SET reconcile_requested=0 WHERE id=?", (job["id"],))
        self._scan()
        self._instances()
        self._watch_old_workers()
        for job_id in list(self._replays):
            if job_id not in self._handles:
                self._finish_orphan(self.store.row("SELECT * FROM jobs WHERE id=?", (job_id,)))
        self._reconcile_targets()
        self._dispatch()
        self._collect()
        recording_archive.publish(self.store)
        self._session_titles()

    def close(self, timeout_sec: float = 70.0, *, preserve_ledger=False):
        """即使存储故障也先收尾本主控的子进程，不碰其他任务。"""
        if self._closed:
            return
        self._shutdown = True
        self._boot_stop.set()
        shutdown_errors = []
        query_cleanup = ThreadPoolExecutor(max_workers=1, thread_name_prefix="query-cleanup")
        query_closed = query_cleanup.submit(self.room_queries.close)
        unlaunched = []
        if not preserve_ledger:
            try:
                self.store.interrupt_commands(self.clock())
                unlaunched = [j["id"] for j in self.store.rows("""SELECT id FROM jobs
                    WHERE status='starting' AND pid IS NULL AND event_log_path IS NULL""")
                    if j["id"] not in self._handles]
            except Exception as exc:
                shutdown_errors.append(str(exc))
        self._verifying.clear()
        for job_id in unlaunched:
            try:
                self.store.request_stop(job_id, "scheduler_shutdown")
                self.store.finish_job(job_id, 0, self.clock(), True, "cancelled_before_launch")
            except Exception as exc:
                shutdown_errors.append(str(exc))
        for job_id in list(self._handles):
            if preserve_ledger:
                if job_id not in self._closing:
                    self._closing[job_id] = self._stoppers.submit(self._handles[job_id].stop, 60.0)
                continue
            try:
                self._stop_job(job_id, "scheduler_shutdown")
            except Exception as exc:
                shutdown_errors.append(str(exc))
                if job_id not in self._closing:
                    self._closing[job_id] = self._stoppers.submit(self._handles[job_id].stop, 60.0)
        deadline = time.monotonic() + timeout_sec
        try:
            while self._closing and time.monotonic() < deadline:
                if not preserve_ledger:
                    try:
                        self._collect()
                    except Exception as exc:
                        shutdown_errors.append(str(exc))
                        preserve_ledger = True
                if preserve_ledger:
                    # 日志由子进程和独立读线程写，存储异常后由下一任主控补账。
                    for job_id, future in list(self._closing.items()):
                        if future is not None and not future.done():
                            continue
                        try:
                            if future is not None:
                                future.result()
                        except Exception as exc:
                            shutdown_errors.append(str(exc))
                        if not self._handles[job_id].is_alive():
                            self._closing.pop(job_id)
                            self._handles.pop(job_id)
                time.sleep(.05)
            if self._closing or self._handles:
                shutdown_errors.append("shutdown_incomplete: 存在未收齐或未退出的任务，需对账")
                for job_id in () if preserve_ledger else self._handles:
                    try:
                        self.store.finish_job(job_id, 30, self.clock(), False,
                                              "shutdown_incomplete", hold_instance=True)
                    except Exception:
                        pass
            if not preserve_ledger:
                try:
                    recording_archive.publish(self.store)
                except Exception as exc:
                    shutdown_errors.append(str(exc))
        finally:
            try:
                query_closed.result(timeout=6)
            except Exception as exc:
                shutdown_errors.append(str(exc))
            query_cleanup.shutdown(wait=False)
            # 开机命令有硬超时，重试等待可取消；在释放主控锁之前等其彻底停止发命令。
            for vmindex, future in list(self._boots.items()):
                future.cancel()
                try:
                    ok = False if future.cancelled() else bool(future.result())
                except Exception:
                    logger.exception("停服收取实例 %s 开机结果失败", vmindex)
                    ok = False
                if not preserve_ledger:
                    try:
                        self.store.finish_boot(vmindex, ok, self.clock(), cancelled=True)
                    except Exception as exc:
                        shutdown_errors.append(str(exc))
                self._boots.pop(vmindex)
            self._queries.shutdown(wait=False, cancel_futures=True)
            self._stoppers.shutdown(wait=False, cancel_futures=True)
            self._lease.close()
            self._closed = True
        if shutdown_errors:
            raise RuntimeError("调度收尾存在异常，日志与状态保留: " + "; ".join(dict.fromkeys(shutdown_errors)))

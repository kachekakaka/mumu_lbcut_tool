"""单机最小调度器：持久化核对、FIFO、异步扫描、在播期间退避重试、实例自动同步与复位、重启对账。"""
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
from pathlib import Path
import queue
import threading
import time

import psutil
from src.mvp.room_resolver import ResolvedRoom, RoomStatus
from src.orchestrator.instance_pool import InstancePool
from src.orchestrator.models import WorkerLaunchSpec
from src.orchestrator.room_scanner import ObservationDispatcher
from src.orchestrator.room_query_process import QueryProcesses
from src.orchestrator.storage import NEEDS_FIX, Store, ControlError
from src.orchestrator.worker_process import WorkerProcessHandle

logger = logging.getLogger(__name__)

# 旧主控遗留的 Worker 写 stdout 失败后会自行封口退出（心跳间隔 3 秒）；超过此时限才转人工对账。
OLD_WORKER_EXIT_WAIT_SEC = 120.0
OLD_WORKER_PROBE_INTERVAL_SEC = 2.0


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
    if pid:
        try:
            p = psutil.Process(pid)
            cmd = p.cmdline()
            if born is not None and abs(p.create_time() - born) < .01:
                if "--job-id" in cmd and cmd[cmd.index("--job-id") + 1] == job["id"] and "src.worker.cli" in cmd:
                    return "alive"
                return "unknown"
        except psutil.NoSuchProcess:
            pass
        except (psutil.AccessDenied, IndexError):
            return "unknown"
    # 覆盖启动后、持久化 PID 前主控崩溃的窗口，以及遗留 scrcpy 子进程。
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmd = proc.info.get("cmdline") or []
            if "--job-id" in cmd and cmd[cmd.index("--job-id") + 1] == job["id"]:
                return "alive"
            if (proc.info.get("name") or "").lower().startswith("scrcpy"):
                if any(arg.startswith("--record=") and job["id"] in arg for arg in cmd):
                    return "alive"
        except (psutil.Error, IndexError):
            continue
    return "absent"


class Scheduler:
    def __init__(self, store: Store, output_dir: Path, instance_indices: list[int],
                 pool=None, query_executor=None, handle_factory=WorkerProcessHandle,
                 clock=time.time, process_probe=inspect_job_process,
                 scan_interval_sec: float = 30.0,
                 ended_interval_sec: float = 10.0, auto_scan: bool = True,
                 segment_duration_sec: int = 1800, navigation_timeout_sec: float = 300.0):
        from src.worker.room_locator import validate_navigation_timeout
        validate_navigation_timeout(navigation_timeout_sec)
        self.navigation_timeout = navigation_timeout_sec
        if not instance_indices or any(i < 0 for i in instance_indices):
            raise ValueError("必须明确配置允许使用的实例索引")
        self.store, self.clock = store, clock
        self.output_dir = Path(output_dir).resolve()
        self.allowed = set(instance_indices)
        self.scan_interval = max(.1, scan_interval_sec)
        self.ended_interval = ended_interval_sec
        self.auto_scan, self.segment_duration = auto_scan, segment_duration_sec
        self.pool = pool or InstancePool()
        self.handle_factory, self.process_probe = handle_factory, process_probe
        self._lease = HostLease(store.path.with_suffix(".host.lock"))
        self._queries = ThreadPoolExecutor(max_workers=2 * len(self.allowed) + 2, thread_name_prefix="scheduler-query")
        self._stoppers = ThreadPoolExecutor(max_workers=len(self.allowed), thread_name_prefix="scheduler-stop")
        self._events: queue.Queue = queue.Queue(maxsize=8192)
        self._event_pending = None
        self._handles, self._closing, self._verifying = {}, {}, {}
        self._scan_requested = []
        self._next_scan = 0.0
        self._refresh_future, self._refresh_started = None, 0.0
        self._resets = {}
        self._waiting_exit = {}
        self._next_waiting_probe = 0.0
        self._shutdown = False
        self._closed = False
        self.room_queries = None
        try:
            self.store.interrupt_commands(self.clock())
            self.room_queries = query_executor or QueryProcesses()
            self.observations = ObservationDispatcher(store, self.room_queries, clock)
            self.store.sync_instances(self.pool.refresh(), self.allowed, self.clock())
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
            # 数据库失败时保留当前事件，不静默丢弃；主循环异常会停止本主控创建的 Worker。
            self.store.record_event(job_id, event, self.clock())
            self._event_pending = None

    def _collect(self):
        self._drain_events()
        for job_id, future in list(self._closing.items()):
            if not future.done():
                continue
            handle = self._handles[job_id]
            reason = ""
            try:
                code = future.result()
                complete = handle.events_complete
            except Exception as exc:
                code = handle.poll_exit_code()
                code = code if code is not None else 30
                reason = f"event_collection_incomplete: {exc}"
                complete = False
            self._drain_events()
            terminal = handle.terminal_event or {}
            reason = reason or terminal.get("reason", "")
            hold = handle.is_alive() or handle.forced_stop or not complete
            self.store.finish_job(job_id, code, self.clock(), complete, reason, hold_instance=hold)
            self._closing.pop(job_id)
            self._handles.pop(job_id)
        for job_id, handle in list(self._handles.items()):
            if job_id in self._closing:
                continue
            if handle.event_errors:
                self.store.request_stop(job_id, "event_receiver_failed")
                self._closing[job_id] = self._stoppers.submit(handle.stop, 60.0)
            elif not handle.is_alive():
                self._closing[job_id] = self._stoppers.submit(handle.wait, 10.0)

    def _stop_job(self, job_id: str, reason: str):
        self.store.request_stop(job_id, reason)
        if job_id in self._handles and job_id not in self._closing:
            self._closing[job_id] = self._stoppers.submit(self._handles[job_id].stop, 60.0)

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
                        if result is None:
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
            with self.store.transaction():
                job = self.store.row("SELECT * FROM jobs WHERE id=?", (subject,)) if source == "verify" else None
                if source == "verify" and (not job or job["status"] != "starting" or job["session_key"] != meta["session_key"]):
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
                self._launch(job, target, result.room)

    def _manual_result(self, meta, command_id, result):
        try:
            with self.store.transaction() as db:
                cmd = self.store.row("SELECT * FROM commands WHERE id=?", (command_id,))
                if not cmd or cmd["status"] != "running":
                    return
                t = self.store.row("SELECT * FROM targets WHERE id=?", (meta["target_id"],))
                existing = self.store.existing_record(t["id"]) if t and t["enabled"] else None
                if existing and self.store.active_job(t["id"])["session_key"] == t["session_key"]:
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
            if not target or self.observations.dispatch("scan", target):
                self._scan_requested.remove(target_id)
            elif not self.room_queries.available("scan"):
                break

    def _instances(self):
        """收取实例刷新结果，并在后台复位登录强阻断后的实例。"""
        if self._refresh_future and self._refresh_future.done():
            try:
                slots = self._refresh_future.result()
            except Exception:
                logger.exception("实例查询失败，保留原状态")
            else:
                self.store.sync_instances(slots, self.allowed, self._refresh_started)
            self._refresh_future = None
        for vmindex, future in list(self._resets.items()):
            if not future.done():
                continue
            self._resets.pop(vmindex)
            try:
                ok = bool(future.result())
            except Exception:
                logger.exception("实例 %s 复位抖音异常", vmindex)
                ok = False
            self.store.finish_reset(vmindex, ok, self.clock())
        if self._shutdown:
            return
        for slot in self.store.rows("SELECT vmindex FROM instances WHERE status='resetting' AND job_id IS NULL"):
            index = slot["vmindex"]
            if index in self.allowed and index not in self._resets:
                self._resets[index] = self._queries.submit(self.pool.reset_app, index)

    def _reconcile_targets(self):
        now = self.clock()
        for target in self.store.rows("SELECT * FROM targets ORDER BY created_at"):
            active = self.store.row("SELECT * FROM jobs WHERE target_id=? AND status IN ('pending','starting','running','stopping','needs_reconcile')", (target["id"],))
            if active:
                if active["status"] == "needs_reconcile":
                    continue
                if not target["enabled"]:
                    self._stop_job(active["id"], "target_removed")
                elif target["trusted_status"] == "ended":
                    self._stop_job(active["id"], "anchor_offline")
                elif target["session_key"] != active["session_key"]:
                    self._stop_job(active["id"], "live_session_changed")
                elif active["status"] == "stopping" and active["id"] in self._handles:
                    self._stop_job(active["id"], active["stop_reason"] or "manual_stop")
                continue
            if self._shutdown or not target["enabled"]:
                continue
            ready = (target["trusted_status"] == "live" and target["observed_status"] == "live"
                     and now - target["observed_at"] <= max(60, self.scan_interval * 2)
                     and (target["auto_record"] or (target["record_session_key"] and target["record_session_key"] == target["session_key"]))
                     and not (target["suppressed_session"] and target["suppressed_session"] == target["session_key"])
                     and not (target["note"] or "").startswith(NEEDS_FIX) and now >= target["retry_at"])
            if ready:
                self.store.create_pending(target["id"], now)

    def _dispatch(self):
        now = self.clock()
        for job in self.store.rows("SELECT * FROM jobs WHERE status='stopping' AND pid IS NULL"):
            if job["id"] not in self._handles:
                self.store.finish_job(job["id"], 0, now, True, "cancelled_before_launch")
        if self._shutdown:
            return
        # 被新观测淘汰的启动复核留在 starting，下轮重新获取有界查询槽位。
        for job in self.store.rows("SELECT * FROM jobs WHERE status='starting' AND pid IS NULL ORDER BY created_at,rowid"):
            if job["id"] not in self._verifying and job["id"] not in self._handles:
                target = self.store.row("SELECT * FROM targets WHERE id=?", (job["target_id"],))
                if self.observations.dispatch("verify", target, job["id"]):
                    self._verifying[job["id"]] = True
        for job in self.store.rows("SELECT * FROM jobs WHERE status='pending' ORDER BY created_at,rowid"):
            target = self.store.row("SELECT * FROM targets WHERE id=?", (job["target_id"],))
            if target["observed_status"] != "live" or now < target["retry_at"]:
                continue
            slots = self.store.rows("SELECT * FROM instances WHERE status='idle' AND job_id IS NULL ORDER BY vmindex")
            slot = next((s for s in slots if s["vmindex"] in self.allowed), None)
            if not slot:
                break
            if self.store.claim(job["id"], slot["vmindex"], now):
                if self.observations.dispatch("verify", target, job["id"]):
                    self._verifying[job["id"]] = True

    def _launch(self, job: dict, target: dict, room: ResolvedRoom):
        job_id = job["id"]
        journal = self.store.path.parent / "events" / f"{job_id}.ndjson"
        internal = bool(room.room_id and room.room_id != room.web_rid)
        spec = WorkerLaunchSpec(vmindex=job["vmindex"], target=room.room_id if internal else room.web_rid,
            output_dir=self.output_dir / job_id, target_kind="room_id" if internal else "web_rid",
            web_rid=room.web_rid, expected_anchor_uid=getattr(room, "anchor_uid", None),
            allow_unconfirmed_identity=bool(target["allow_unconfirmed"]), job_id=job_id,
            event_log_path=journal, log_path=journal.with_suffix(".log"), segment_duration_sec=self.segment_duration,
            navigation_timeout_sec=self.navigation_timeout)
        # 先保存启动意图和产物关联，覆盖 Popen 后写 PID 前的异常窗口。
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET room_id=?,event_log_path=? WHERE id=?", (room.room_id, str(journal), job_id))
        handle = self.handle_factory(spec=spec, on_event=lambda event, key=job_id: self._events.put_nowait((key, event)))
        self._handles[job_id] = handle
        try:
            handle.start()
            self.store.mark_launched(job_id, handle, str(journal), self.clock())
        except Exception as exc:
            logger.exception("Worker 启动失败")
            if handle.is_alive():
                self.store.request_stop(job_id, "launch_persistence_failed")
                self._closing[job_id] = self._stoppers.submit(handle.stop, 60.0)
            else:
                self._handles.pop(job_id, None)
                # 启动失败计入失败次数，使退避间隔逐次加长。
                with self.store.transaction() as db:
                    db.execute("UPDATE targets SET attempts=attempts+1 WHERE id=?", (target["id"],))
                self.store.finish_job(job_id, 30, self.clock(), True, f"launch_failed: {exc}")

    def reconcile(self, selected_job: str | None = None):
        sql = "SELECT * FROM jobs WHERE status IN ('starting','running','stopping','needs_reconcile')"
        params = ()
        if selected_job:
            sql += " AND id=?"
            params = (selected_job,)
        for job in self.store.rows(sql, params):
            if job["id"] in self._handles or job["id"] in self._verifying:
                continue
            status = self.process_probe(job)
            if status == "absent":
                self._waiting_exit.pop(job["id"], None)
                self._finish_orphan(job)
            elif status == "alive":
                # 新主控不在旧进程的控制台，发不出 Ctrl+Break；等 Worker 发现主控断开后自行封口。
                if job["id"] not in self._waiting_exit:
                    self._waiting_exit[job["id"]] = self.clock() + OLD_WORKER_EXIT_WAIT_SEC
                    self.store.mark_waiting_exit(job["id"])
            else:
                self._waiting_exit.pop(job["id"], None)
                self.store.finish_job(job["id"], 30, self.clock(), False,
                                      "unowned_process_requires_reconciliation", hold_instance=True)

    def _watch_old_workers(self):
        now = self.clock()
        if not self._waiting_exit or now < self._next_waiting_probe:
            return
        self._next_waiting_probe = now + OLD_WORKER_PROBE_INTERVAL_SEC
        for job_id, deadline in list(self._waiting_exit.items()):
            job = self.store.row("SELECT * FROM jobs WHERE id=?", (job_id,))
            if not job or job["status"] != "stopping":
                self._waiting_exit.pop(job_id)
                continue
            status = self.process_probe(job)
            if status == "absent":
                self._waiting_exit.pop(job_id)
                self._finish_orphan(job)
            elif status != "alive" or now >= deadline:
                self._waiting_exit.pop(job_id)
                reason = "old_worker_did_not_exit" if status == "alive" else "unowned_process_requires_reconciliation"
                self.store.finish_job(job_id, 30, now, False, reason, hold_instance=True)

    def _finish_orphan(self, job: dict):
        """进程已消失：重放 NDJSON 补齐事件，按其终止事件记录结果；不抑制本场，由退避规则续录。"""
        complete, terminal = True, None
        path = Path(job["event_log_path"]) if job["event_log_path"] else None
        if path and path.exists():
            try:
                with path.open(encoding="utf-8") as stream:
                    for line in stream:
                        if line.strip():
                            event = json.loads(line)
                            self.store.record_event(job["id"], event, self.clock())
                            if event.get("event") in ("worker_stopped", "worker_failed"):
                                terminal = event
            except (OSError, ValueError):
                complete = False
        code = terminal.get("exit_code") if terminal else None
        code = 30 if code is None else int(code)
        reason = (terminal or {}).get("reason") or "interrupted_host_restart"
        stopped_at = (terminal or {}).get("timestamp")
        self.store.finish_job(job["id"], code, self.clock(), complete and terminal is not None, reason,
                              ended_at=stopped_at if isinstance(stopped_at, (int, float)) else None)

    def tick(self):
        self._collect()
        if self._shutdown:
            return
        self._commands()
        self._query_results()
        for job in self.store.rows("SELECT id FROM jobs WHERE reconcile_requested=1"):
            self.reconcile(job["id"])
            with self.store.transaction() as db:
                db.execute("UPDATE jobs SET reconcile_requested=0 WHERE id=?", (job["id"],))
        self._scan()
        self._instances()
        self._watch_old_workers()
        self._reconcile_targets()
        self._dispatch()
        self._collect()

    def close(self, timeout_sec: float = 70.0):
        """即使存储故障也先收尾本主控的子进程，不碰其他任务。"""
        if self._closed:
            return
        self._shutdown = True
        shutdown_errors = []
        query_cleanup = ThreadPoolExecutor(max_workers=1, thread_name_prefix="query-cleanup")
        query_closed = query_cleanup.submit(self.room_queries.close)
        try:
            self.store.interrupt_commands(self.clock())
        except Exception as exc:
            shutdown_errors.append(str(exc))
        try:
            unlaunched = [j["id"] for j in self.store.rows("SELECT id FROM jobs WHERE status='starting' AND pid IS NULL")]
        except Exception as exc:
            shutdown_errors.append(str(exc))
            unlaunched = list(self._verifying)
        for job_id in unlaunched:
            try:
                self.store.request_stop(job_id, "scheduler_shutdown")
                self.store.finish_job(job_id, 0, self.clock(), True, "cancelled_before_launch")
            except Exception as exc:
                shutdown_errors.append(str(exc))
        self._verifying.clear()
        for job_id in list(self._handles):
            try:
                self._stop_job(job_id, "scheduler_shutdown")
            except Exception as exc:
                shutdown_errors.append(str(exc))
                if job_id not in self._closing:
                    self._closing[job_id] = self._stoppers.submit(self._handles[job_id].stop, 60.0)
        deadline = time.monotonic() + timeout_sec
        try:
            while self._closing and time.monotonic() < deadline:
                try:
                    self._collect()
                except Exception as exc:
                    # 数据库不可写时，NDJSON 日志仍是对账证据；不得因此跳过进程停止。
                    shutdown_errors.append(str(exc))
                    for job_id, future in list(self._closing.items()):
                        if future.done():
                            try:
                                future.result()
                            except Exception as stop_exc:
                                shutdown_errors.append(str(stop_exc))
                            handle = self._handles.get(job_id)
                            if handle is not None and not handle.is_alive():
                                self._closing.pop(job_id)
                                self._handles.pop(job_id, None)
                time.sleep(.05)
            if self._closing or self._handles:
                shutdown_errors.append("shutdown_incomplete: 存在未收齐或未退出的任务，需对账")
                for job_id in self._handles:
                    try:
                        self.store.finish_job(job_id, 30, self.clock(), False,
                                              "shutdown_incomplete", hold_instance=True)
                    except Exception:
                        pass
        finally:
            try:
                query_closed.result(timeout=6)
            except Exception as exc:
                shutdown_errors.append(str(exc))
            query_cleanup.shutdown(wait=False)
            self._queries.shutdown(wait=False, cancel_futures=True)
            self._stoppers.shutdown(wait=False, cancel_futures=True)
            self._lease.close()
            self._closed = True
        if shutdown_errors:
            raise RuntimeError("调度收尾存在异常，日志与状态保留: " + "; ".join(dict.fromkeys(shutdown_errors)))

"""单机最小调度器：持久化核对、FIFO、异步扫描、有限重试和保守重启对账。"""
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
from src.orchestrator.room_scanner import RoomScanner
from src.orchestrator.storage import Store
from src.orchestrator.worker_process import WorkerProcessHandle

logger = logging.getLogger(__name__)


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
                 pool=None, scanner=None, handle_factory=WorkerProcessHandle,
                 clock=time.time, process_probe=inspect_job_process,
                 scan_interval_sec: float = 30.0, max_attempts: int = 3,
                 ended_interval_sec: float = 10.0, auto_scan: bool = True,
                 segment_duration_sec: int = 1800, overlap_duration_sec: float = 0.0,
                 recording_mode: str = "continuous"):
        if not instance_indices or any(i < 0 for i in instance_indices):
            raise ValueError("必须明确配置允许使用的实例索引")
        self.store, self.clock = store, clock
        self.output_dir = Path(output_dir).resolve()
        self.allowed = set(instance_indices)
        self.scan_interval = max(.1, scan_interval_sec)
        self.max_attempts, self.ended_interval = max_attempts, ended_interval_sec
        self.auto_scan, self.segment_duration = auto_scan, segment_duration_sec
        if recording_mode not in {'continuous', 'restart'}:
            raise ValueError('未知录制模式')
        if recording_mode == 'continuous' and overlap_duration_sec:
            raise ValueError('连续采集不能配置交叠；诊断模式需显式选择 restart')
        self.recording_mode = recording_mode
        self.overlap_duration = overlap_duration_sec
        self.scanner = scanner or RoomScanner()
        self.pool = pool or InstancePool()
        self.handle_factory, self.process_probe = handle_factory, process_probe
        self._lease = HostLease(store.path.with_suffix(".host.lock"))
        self._queries = ThreadPoolExecutor(max_workers=max(2, len(self.allowed) + 1), thread_name_prefix="scheduler-query")
        self._stoppers = ThreadPoolExecutor(max_workers=len(self.allowed), thread_name_prefix="scheduler-stop")
        self._events: queue.Queue = queue.Queue(maxsize=8192)
        self._event_pending = None
        self._handles, self._closing, self._verifying = {}, {}, {}
        self._scan_future = None
        self._scan_requested = []
        self._next_scan = 0.0
        self._shutdown = False
        self._closed = False
        try:
            self.store.sync_instances(self.pool.refresh(), self.allowed, self.clock())
            self.reconcile()
        except BaseException:
            self._queries.shutdown(wait=False, cancel_futures=True)
            self._stoppers.shutdown(wait=False, cancel_futures=True)
            self._lease.close()
            raise

    def apply_snapshot(self, snapshot):
        for target in self.store.rows("SELECT * FROM targets WHERE enabled=1"):
            room = snapshot.observations.get(target["raw"])
            if room is None:
                continue
            self.store.observe(target["id"], room, snapshot.timestamp,
                               ended_interval_sec=self.ended_interval)

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
        for command in self.store.rows("SELECT * FROM commands WHERE status='pending' ORDER BY created_at LIMIT 30"):
            kind, subject = command["kind"], command["subject"]
            try:
                if kind == "stop":
                    job = self.store.row("SELECT * FROM jobs WHERE id=?", (subject,))
                    if not job:
                        raise ValueError("任务不存在")
                    if job["status"] == "needs_reconcile":
                        raise ValueError("任务需对账，当前主控没有其控制句柄，不发送不明信号")
                    self._stop_job(subject, "manual_stop")
                elif kind == "record":
                    with self.store.transaction() as db:
                        target = db.execute("SELECT id FROM targets WHERE id=? AND enabled=1", (subject,)).fetchone()
                        if not target:
                            raise ValueError("目标不存在或已移除")
                        active = db.execute("SELECT id FROM jobs WHERE target_id=? AND status IN ('pending','starting','running','stopping','needs_reconcile')", (subject,)).fetchone()
                        if not active:
                            db.execute("UPDATE targets SET manual_requested=1,suppressed_session=NULL,attempts=0,retry_at=0 WHERE id=?", (subject,))
                elif kind == "remove":
                    with self.store.transaction() as db:
                        if not db.execute("UPDATE targets SET enabled=0,manual_requested=0 WHERE id=?", (subject,)).rowcount:
                            raise ValueError("目标不存在")
                    for job in self.store.rows("SELECT * FROM jobs WHERE target_id=? AND status IN ('pending','starting','running','stopping')", (subject,)):
                        self._stop_job(job["id"], "target_removed")
                elif kind == "unblock":
                    idx = int(subject)
                    # 显式人工解封仍需重新发现设备，不能凭文件记录直接变空闲。
                    slots = self.pool.refresh()
                    if idx not in self.allowed or not any(s.vmindex == idx for s in slots):
                        raise ValueError("实例不在允许列表或未发现")
                    with self.store.transaction() as db:
                        if not db.execute("UPDATE instances SET status='idle',reason=NULL WHERE vmindex=? AND job_id IS NULL", (idx,)).rowcount:
                            raise ValueError("实例仍被任务占用，需要先对账")
                elif kind == "reconcile":
                    self.reconcile(subject or None)
                self.store.complete_command(command["id"])
            except (ValueError, RuntimeError) as exc:
                self.store.complete_command(command["id"], str(exc))

    def _scan(self):
        now = self.clock()
        if self._scan_future and self._scan_future.done():
            try:
                result = self._scan_future.result()
            except Exception:
                logger.exception("扫描失败，保留最后可信状态")
            else:
                self.apply_snapshot(result)
            self._scan_future = None
            self._next_scan = now + self.scan_interval
        if self.auto_scan and self._scan_future is None and now >= self._next_scan and not self._shutdown:
            self._scan_requested = [t["raw"] for t in self.store.rows("SELECT raw FROM targets WHERE enabled=1")]
            self._scan_future = self._queries.submit(self.scanner.scan, self._scan_requested)

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
                continue
            if self._shutdown or not target["enabled"]:
                continue
            ready = (target["trusted_status"] == "live" and target["observed_status"] == "live"
                     and now - target["observed_at"] <= max(60, self.scan_interval * 2)
                     and (target["auto_record"] or target["manual_requested"])
                     and target["suppressed_session"] != target["session_key"]
                     and target["attempts"] < self.max_attempts and now >= target["retry_at"])
            if ready:
                self.store.create_pending(target["id"], now)

    def _dispatch(self):
        now = self.clock()
        for job_id, future in list(self._verifying.items()):
            if not future.done():
                continue
            self._verifying.pop(job_id)
            job = self.store.row("SELECT * FROM jobs WHERE id=?", (job_id,))
            if not job:
                continue
            if job["status"] != "starting" or self._shutdown:
                self.store.finish_job(job_id, 0, now, True, "cancelled_before_launch")
                continue
            try:
                room = future.result()
            except Exception:
                room = ResolvedRoom(web_rid="", status=RoomStatus.UNKNOWN)
            target = self.store.row("SELECT * FROM targets WHERE id=?", (job["target_id"],))
            self.store.observe(target["id"], room, now, ended_interval_sec=self.ended_interval)
            if getattr(room.status, "value", room.status) != "live":
                self.store.defer_start(job_id, now, "prelaunch_not_confirmed_live")
                continue
            target = self.store.row("SELECT * FROM targets WHERE id=?", (job["target_id"],))
            if not target["enabled"] or target["session_key"] != job["session_key"]:
                self.store.request_stop(job_id, "live_session_changed")
                self.store.finish_job(job_id, 0, now, True, "live_session_changed")
                continue
            self._launch(job, target, room)
        if self._shutdown:
            return
        for job in self.store.rows("SELECT * FROM jobs WHERE status='pending' ORDER BY created_at,rowid"):
            target = self.store.row("SELECT * FROM targets WHERE id=?", (job["target_id"],))
            if target["observed_status"] != "live" or now < target["retry_at"]:
                continue
            slots = self.store.rows("SELECT * FROM instances WHERE status='idle' AND job_id IS NULL ORDER BY vmindex")
            slot = next((s for s in slots if s["vmindex"] in self.allowed), None)
            if not slot:
                break
            if self.store.claim(job["id"], slot["vmindex"], now):
                # 分配槽位后重新查一次当前场次；后台查询不阻塞其他录制的停止。
                self._verifying[job["id"]] = self._queries.submit(self.scanner.resolver.resolve, target["raw"])

    def _launch(self, job: dict, target: dict, room: ResolvedRoom):
        job_id = job["id"]
        journal = self.store.path.parent / "events" / f"{job_id}.ndjson"
        internal = bool(room.room_id and room.room_id != room.web_rid)
        spec = WorkerLaunchSpec(vmindex=job["vmindex"], target=room.room_id if internal else room.web_rid,
            output_dir=self.output_dir / job_id, target_kind="room_id" if internal else "web_rid",
            web_rid=room.web_rid, expected_anchor_uid=getattr(room, "anchor_uid", None),
            allow_unconfirmed_identity=bool(target["allow_unconfirmed"]), job_id=job_id,
            event_log_path=journal, segment_duration_sec=self.segment_duration,
            overlap_duration_sec=self.overlap_duration, recording_mode=self.recording_mode)
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
                # 启动失败也消耗一次重试，防止 Popen 错误无限重试。
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
            if status != "absent":
                self.store.finish_job(job["id"], 30, self.clock(), False,
                                      "live_or_unowned_process_requires_reconciliation", hold_instance=True)
                continue
            complete = True
            path = Path(job["event_log_path"]) if job["event_log_path"] else None
            if path and path.exists():
                try:
                    with path.open(encoding="utf-8") as stream:
                        for line in stream:
                            if line.strip():
                                self.store.record_event(job["id"], json.loads(line), self.clock())
                except (OSError, ValueError):
                    complete = False
            self.store.finish_job(job["id"], 30, self.clock(), complete, "interrupted_host_restart")
            with self.store.transaction() as db:
                # 未知中断不自动续录同场，先保留证据，由用户显式 record 解除抑制。
                db.execute("UPDATE targets SET suppressed_session=?,manual_requested=0 WHERE id=?", (job["session_key"], job["target_id"]))

    def tick(self):
        self._collect()
        self._commands()
        self._scan()
        self._reconcile_targets()
        self._dispatch()
        self._collect()

    def close(self, timeout_sec: float = 70.0):
        """即使存储故障也先收尾本主控的子进程，不碰其他任务。"""
        if self._closed:
            return
        self._shutdown = True
        shutdown_errors = []
        for job_id, future in list(self._verifying.items()):
            future.cancel()
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
            self._queries.shutdown(wait=False, cancel_futures=True)
            self._stoppers.shutdown(wait=False, cancel_futures=True)
            self._lease.close()
            self._closed = True
        if shutdown_errors:
            raise RuntimeError("调度收尾存在异常，日志与状态保留: " + "; ".join(dict.fromkeys(shutdown_errors)))

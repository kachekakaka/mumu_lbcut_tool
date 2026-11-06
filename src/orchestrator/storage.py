"""最小调度持久化：SQLite 事务、活跃任务/实例唯一约束、幂等事件和人工命令。"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from src.mvp.room_resolver import ResolvedRoom

ACTIVE = ("pending", "starting", "running", "stopping", "needs_reconcile")
SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
 id TEXT PRIMARY KEY, raw TEXT UNIQUE NOT NULL, canonical TEXT UNIQUE NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1, auto_record INTEGER NOT NULL DEFAULT 1,
 allow_unconfirmed INTEGER NOT NULL DEFAULT 0, manual_requested INTEGER NOT NULL DEFAULT 0,
 trusted_status TEXT NOT NULL DEFAULT 'unknown', observed_status TEXT NOT NULL DEFAULT 'unknown',
 observed_at REAL NOT NULL DEFAULT 0, confirmed_at REAL NOT NULL DEFAULT 0,
 room_id TEXT, anchor_uid TEXT, nickname TEXT, session_key TEXT,
 ended_count INTEGER NOT NULL DEFAULT 0, ended_since REAL NOT NULL DEFAULT 0,
 suppressed_session TEXT, attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
 note TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS instances (
 vmindex INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'idle',
 job_id TEXT, reason TEXT, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, target_id TEXT NOT NULL REFERENCES targets(id), session_key TEXT NOT NULL,
 room_id TEXT, status TEXT NOT NULL, vmindex INTEGER, pid INTEGER, process_created REAL,
 command_json TEXT, event_log_path TEXT, created_at REAL NOT NULL, started_at REAL, ended_at REAL,
 stop_reason TEXT, error_reason TEXT, exit_code INTEGER, heartbeat_json TEXT,
 collection_complete INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_target ON jobs(target_id)
 WHERE status IN ('pending','starting','running','stopping','needs_reconcile');
CREATE UNIQUE INDEX IF NOT EXISTS one_active_vm ON jobs(vmindex)
 WHERE vmindex IS NOT NULL AND status IN ('starting','running','stopping','needs_reconcile');
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), kind TEXT NOT NULL,
 payload TEXT NOT NULL, received_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS segments (
 job_id TEXT NOT NULL REFERENCES jobs(id), segment_index INTEGER NOT NULL,
 started INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'started',
 raw_path TEXT, file_path TEXT, duration_sec REAL, file_size_mb REAL,
 is_final INTEGER NOT NULL DEFAULT 0, reason TEXT, risk_json TEXT,
 PRIMARY KEY(job_id, segment_index)
);
CREATE TABLE IF NOT EXISTS commands (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', result TEXT, created_at REAL NOT NULL
);
"""


def canonical_target(raw: str) -> str:
    raw = raw.strip()
    match = re.search(r"live\.douyin\.com/([\w.~-]+)", raw)
    return match.group(1) if match else raw.rstrip("/")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if tables and version != 1:
            self.db.close()
            raise ValueError("数据库不是本调度器格式；拒绝覆盖或自动迁移")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        self.db.execute("PRAGMA user_version=1")

    @contextmanager
    def transaction(self):
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def close(self):
        with self._lock:
            self.db.close()

    def rows(self, sql: str, params=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.db.execute(sql, params)]

    def row(self, sql: str, params=()) -> dict | None:
        rows = self.rows(sql, params)
        return rows[0] if rows else None

    def add_target(self, raw: str, auto_record=True, allow_unconfirmed=False, now=None) -> str:
        raw = raw.strip()
        if not raw:
            raise ValueError("监控目标不能为空")
        canonical = canonical_target(raw)
        now = time.time() if now is None else now
        with self.transaction() as db:
            old = db.execute("SELECT id FROM targets WHERE canonical=?", (canonical,)).fetchone()
            if old:
                return old[0]
            key = uuid.uuid4().hex
            db.execute("INSERT INTO targets(id,raw,canonical,auto_record,allow_unconfirmed,created_at) VALUES(?,?,?,?,?,?)",
                       (key, raw, canonical, int(auto_record), int(allow_unconfirmed), now))
            return key

    def observe(self, target_id: str, room: ResolvedRoom, now: float,
                ended_confirmations=2, ended_interval_sec=10.0):
        """未知保留可信状态；两次间隔达标的明确下播才改变可信状态。"""
        value = getattr(room.status, "value", room.status)
        value = value if value in ("live", "ended") else "unknown"
        with self.transaction() as db:
            t = db.execute("SELECT * FROM targets WHERE id=?", (target_id,)).fetchone()
            if not t or now <= t["observed_at"]:
                return
            canonical = room.web_rid or t["canonical"]
            duplicate = db.execute("SELECT id FROM targets WHERE canonical=? AND id<>?", (canonical, target_id)).fetchone()
            if duplicate:
                db.execute("UPDATE targets SET enabled=0,note=? WHERE id=?", ("duplicate_of:" + duplicate[0], target_id))
                return
            db.execute("UPDATE targets SET observed_status=?,observed_at=?,canonical=? WHERE id=?",
                       (value, now, canonical, target_id))
            if value == "unknown":
                db.execute("UPDATE targets SET ended_count=0,ended_since=0 WHERE id=?", (target_id,))
                return
            if value == "live":
                room_id = room.room_id or room.web_rid
                new_session = t["trusted_status"] != "live" or (room_id and t["room_id"] and room_id != t["room_id"])
                session = ("room:" + str(room_id)) if room_id else ("live:" + uuid.uuid4().hex)
                if not new_session and t["session_key"]:
                    session = t["session_key"]
                db.execute("""UPDATE targets SET trusted_status='live',confirmed_at=?,room_id=?,anchor_uid=?,nickname=?,
                    session_key=?,ended_count=0,ended_since=0 WHERE id=?""",
                    (now, room_id, getattr(room, "anchor_uid", None), room.nickname, session, target_id))
                if new_session:
                    db.execute("UPDATE targets SET suppressed_session=NULL,attempts=0,retry_at=0 WHERE id=?", (target_id,))
            else:
                count = t["ended_count"] + 1
                since = t["ended_since"] if t["ended_count"] else now
                db.execute("UPDATE targets SET ended_count=?,ended_since=? WHERE id=?", (count, since, target_id))
                if count >= ended_confirmations and now - since >= ended_interval_sec:
                    db.execute("""UPDATE targets SET trusted_status='ended',confirmed_at=?,suppressed_session=NULL,
                        manual_requested=0,attempts=0,retry_at=0 WHERE id=?""", (now, target_id))

    def sync_instances(self, slots, allowed: set[int], now: float):
        with self.transaction() as db:
            seen = set()
            for slot in slots:
                if slot.vmindex not in allowed:
                    continue
                seen.add(slot.vmindex)
                observed = getattr(slot.status, "value", slot.status)
                existing = db.execute("SELECT * FROM instances WHERE vmindex=?", (slot.vmindex,)).fetchone()
                if not existing:
                    status = "offline" if observed == "offline" else "idle"
                    db.execute("INSERT INTO instances(vmindex,name,status,updated_at) VALUES(?,?,?,?)",
                               (slot.vmindex, slot.name, status, now))
                else:
                    db.execute("UPDATE instances SET name=?,updated_at=? WHERE vmindex=?", (slot.name, now, slot.vmindex))
            # 已占用或故障隔离的实例不得因为刷新被误恢复为空闲。
            for index in allowed - seen:
                db.execute("UPDATE instances SET status='offline',reason='not_discovered' WHERE vmindex=? AND job_id IS NULL", (index,))

    def create_pending(self, target_id: str, now: float) -> str | None:
        with self.transaction() as db:
            t = db.execute("SELECT * FROM targets WHERE id=?", (target_id,)).fetchone()
            if not t or not t["session_key"]:
                return None
            old = db.execute("SELECT id FROM jobs WHERE target_id=? AND status IN ('pending','starting','running','stopping','needs_reconcile')", (target_id,)).fetchone()
            if old:
                return old[0]
            key = uuid.uuid4().hex
            db.execute("INSERT INTO jobs(id,target_id,session_key,room_id,status,created_at) VALUES(?,?,?,?,'pending',?)",
                       (key, target_id, t["session_key"], t["room_id"], now))
            return key

    def claim(self, job_id: str, vmindex: int, now: float) -> bool:
        with self.transaction() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            slot = db.execute("SELECT * FROM instances WHERE vmindex=?", (vmindex,)).fetchone()
            if not job or job["status"] != "pending" or not slot or slot["status"] != "idle" or slot["job_id"]:
                return False
            db.execute("UPDATE instances SET status='busy',job_id=?,reason=NULL,updated_at=? WHERE vmindex=?", (job_id, now, vmindex))
            db.execute("UPDATE jobs SET status='starting',vmindex=? WHERE id=?", (vmindex, job_id))
            return True

    def defer_start(self, job_id: str, now: float, reason: str):
        with self.transaction() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job or job["status"] != "starting":
                return
            db.execute("UPDATE instances SET status='idle',job_id=NULL,reason=NULL WHERE job_id=?", (job_id,))
            db.execute("UPDATE jobs SET status='pending',vmindex=NULL,error_reason=? WHERE id=?", (reason, job_id))
            db.execute("UPDATE targets SET retry_at=? WHERE id=?", (now + 5, job["target_id"]))

    def mark_launched(self, job_id: str, handle, event_log_path: str, now: float):
        with self.transaction() as db:
            job = db.execute("SELECT target_id FROM jobs WHERE id=?", (job_id,)).fetchone()
            db.execute("""UPDATE jobs SET status='starting',pid=?,process_created=?,command_json=?,event_log_path=?,started_at=? WHERE id=?""",
                       (handle.proc.pid, handle.created_at, json.dumps(handle.command), event_log_path, now, job_id))
            db.execute("UPDATE targets SET attempts=attempts+1,manual_requested=0 WHERE id=?", (job[0],))

    def request_stop(self, job_id: str, reason: str):
        with self.transaction() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job or job["status"] not in ACTIVE:
                return
            if reason in ("manual_stop", "scheduler_shutdown"):
                db.execute("UPDATE targets SET suppressed_session=session_key,manual_requested=0 WHERE id=?", (job["target_id"],))
            if job["status"] == "pending":
                db.execute("UPDATE jobs SET status='cancelled',stop_reason=?,ended_at=? WHERE id=?", (reason, time.time(), job_id))
            else:
                db.execute("UPDATE jobs SET status='stopping',stop_reason=COALESCE(stop_reason,?) WHERE id=?", (reason, job_id))

    def record_event(self, job_id: str, event: dict, now: float):
        payload = json.dumps(event, sort_keys=True, ensure_ascii=False)
        key = job_id + ":" + str(event.get("event_id") or hashlib.sha256(payload.encode()).hexdigest())
        kind = event.get("event", "")
        if event.get("job_id") not in (None, "", job_id):
            raise ValueError("Worker 事件 job_id 不匹配")
        with self.transaction() as db:
            if db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?)", (key, job_id, kind, payload, now)).rowcount == 0:
                return
            if kind == "heartbeat":
                db.execute("UPDATE jobs SET heartbeat_json=? WHERE id=?", (payload, job_id))
            if kind in ("segment_started", "segment_finished", "segment_aborted", "segment_quality_warning"):
                idx = int(event["segment_index"])
                db.execute("INSERT OR IGNORE INTO segments(job_id,segment_index) VALUES(?,?)", (job_id, idx))
                old = db.execute("SELECT * FROM segments WHERE job_id=? AND segment_index=?", (job_id, idx)).fetchone()
                if kind == "segment_started":
                    db.execute("UPDATE segments SET started=1,raw_path=? WHERE job_id=? AND segment_index=?", (event.get("file_path"), job_id, idx))
                    # Popen 成功不等于开始录制；首片开始后才将任务标为 running。
                    db.execute("UPDATE jobs SET status='running' WHERE id=? AND status='starting'", (job_id,))
                elif kind == "segment_quality_warning":
                    db.execute("UPDATE segments SET risk_json=? WHERE job_id=? AND segment_index=?", (payload, job_id, idx))
                else:
                    status = "finished" if kind == "segment_finished" else "aborted"
                    if old["status"] in ("finished", "aborted"):
                        if old["status"] != status or old["file_path"] != event.get("file_path"):
                            raise ValueError("同一切片收到冲突终态")
                        return
                    db.execute("""UPDATE segments SET status=?,file_path=?,duration_sec=?,file_size_mb=?,is_final=?,reason=?
                        WHERE job_id=? AND segment_index=?""", (status, event.get("file_path"), event.get("duration_sec"),
                        event.get("file_size_mb"), int(bool(event.get("is_final"))), event.get("reason"), job_id, idx))

    def finish_job(self, job_id: str, code: int, now: float, collection_complete: bool,
                   reason: str = "", hold_instance: bool = False):
        with self.transaction() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            segs = list(db.execute("SELECT * FROM segments WHERE job_id=?", (job_id,)))
            incomplete = any(not s["started"] or s["status"] == "started" for s in segs)
            too_many_final = sum(s["is_final"] for s in segs) > 1
            if not collection_complete or incomplete or too_many_final:
                code = code or 30
                reason = reason or "event_collection_incomplete"
            if code == 0 and not segs and not job["stop_reason"]:
                code, reason = 30, "no_recorded_segment"
            has_errors = any(s["status"] == "aborted" or s["risk_json"] for s in segs)
            status = "failed" if code else ("finished_with_warnings" if has_errors else "finished")
            if hold_instance:
                status = "needs_reconcile"
            db.execute("UPDATE jobs SET status=?,exit_code=?,error_reason=?,ended_at=?,collection_complete=? WHERE id=?",
                       (status, code, reason or None, now, int(collection_complete and not incomplete), job_id))
            if job["vmindex"] is not None:
                slot_status = "reconcile" if hold_instance else ("blocked" if code == 15 else "offline" if code == 20 else "idle")
                db.execute("UPDATE instances SET status=?,job_id=?,reason=?,updated_at=? WHERE vmindex=?",
                           (slot_status, job_id if hold_instance else None, reason or None, now, job["vmindex"]))
            if not code or job["stop_reason"] or code in (9, 10, 18):
                db.execute("UPDATE targets SET suppressed_session=?,manual_requested=0 WHERE id=?", (job["session_key"], job["target_id"]))
            else:
                attempts = db.execute("SELECT attempts FROM targets WHERE id=?", (job["target_id"],)).fetchone()[0]
                db.execute("UPDATE targets SET retry_at=? WHERE id=?", (now + min(120, 5 * (2 ** min(attempts, 5))), job["target_id"]))

    def enqueue_command(self, kind: str, subject: str, now=None) -> str:
        if kind not in {"record", "stop", "remove", "unblock", "reconcile"}:
            raise ValueError("未知控制命令")
        key = uuid.uuid4().hex
        with self.transaction() as db:
            db.execute("INSERT INTO commands(id,kind,subject,created_at) VALUES(?,?,?,?)", (key, kind, subject, time.time() if now is None else now))
        return key

    def complete_command(self, key: str, error: str = ""):
        with self.transaction() as db:
            db.execute("UPDATE commands SET status=?,result=? WHERE id=?", ("error" if error else "processed", error or "ok", key))

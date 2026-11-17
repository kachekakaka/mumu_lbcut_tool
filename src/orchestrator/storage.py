"""最小调度持久化：SQLite 事务、活跃任务/实例唯一约束、幂等事件、任务结果规则和人工命令。"""
from contextlib import contextmanager
import json
import logging
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from src.mvp.room_resolver import ResolvedRoom
from src.worker.room_locator import valid_identity_id


logger = logging.getLogger(__name__)


def identity_admission(target, now, freshness_sec=60):
    """准入只用同一次最近观测；不改变在播与场次事实。"""
    if target['observed_status'] != 'live':
        return 'prelaunch_not_confirmed_live'
    if not valid_identity_id(target['room_id']) or not valid_identity_id(target['anchor_uid']):
        return 'target_identity_incomplete'
    if now - target['observed_at'] > freshness_sec:
        return 'target_identity_stale'
    return None

ACTIVE = ("pending", "starting", "running", "stopping", "needs_reconcile")
NEEDS_FIX = "needs_fix: "          # 目标无效，直到显式 record 查询确认 LIVE 后解除
RETRY_BASE_SEC, RETRY_MAX_SEC = 5, 300
BOOT_RETRY_BASE_SEC, BOOT_RETRY_MAX_SEC = 30, 300
SCHEMA_VERSION = 3
# 这些停止原因已由目标状态决定后续（移除、确认下播、换场、主控正常退出），无需退避。
SETTLED_STOPS = ("target_removed", "anchor_offline", "live_session_changed", "scheduler_shutdown")
SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
 id TEXT PRIMARY KEY, raw TEXT UNIQUE NOT NULL, canonical TEXT UNIQUE NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1, auto_record INTEGER NOT NULL DEFAULT 1,
 allow_unconfirmed INTEGER NOT NULL DEFAULT 0, record_session_key TEXT,
 control_revision INTEGER NOT NULL DEFAULT 0, latest_observation_seq INTEGER NOT NULL DEFAULT 0,
 applied_observation_seq INTEGER NOT NULL DEFAULT 0,
 trusted_status TEXT NOT NULL DEFAULT 'unknown', observed_status TEXT NOT NULL DEFAULT 'unknown',
 observed_at REAL NOT NULL DEFAULT 0, confirmed_at REAL NOT NULL DEFAULT 0,
 room_id TEXT, anchor_uid TEXT, nickname TEXT, session_key TEXT,
 ended_count INTEGER NOT NULL DEFAULT 0, ended_since REAL NOT NULL DEFAULT 0,
 suppressed_session TEXT, attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
 note TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS instances (
 vmindex INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'idle',
 enabled INTEGER NOT NULL DEFAULT 0, job_id TEXT, reason TEXT,
 boot_attempts INTEGER NOT NULL DEFAULT 0, boot_retry_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, target_id TEXT NOT NULL REFERENCES targets(id), session_key TEXT NOT NULL,
 room_id TEXT, status TEXT NOT NULL, vmindex INTEGER, pid INTEGER, process_created REAL,
 command_json TEXT, event_log_path TEXT, created_at REAL NOT NULL, started_at REAL, ended_at REAL,
 stop_reason TEXT, error_reason TEXT, exit_code INTEGER, heartbeat_json TEXT,
 collection_complete INTEGER NOT NULL DEFAULT 0, reconcile_requested INTEGER NOT NULL DEFAULT 0
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
 created_at REAL NOT NULL, started_at REAL, ended_at REAL, updated_at REAL NOT NULL,
 PRIMARY KEY(job_id, segment_index)
);
CREATE TABLE IF NOT EXISTS commands (
 id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, subject TEXT NOT NULL,
 payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', result_json TEXT,
 error_code TEXT, message TEXT, created_at REAL NOT NULL, started_at REAL, finished_at REAL
);
CREATE INDEX IF NOT EXISTS jobs_query ON jobs(status,target_id,created_at,id);
CREATE INDEX IF NOT EXISTS segments_time ON segments(COALESCE(started_at,created_at),job_id,segment_index);
CREATE INDEX IF NOT EXISTS events_job ON events(job_id,kind,received_at);
CREATE INDEX IF NOT EXISTS commands_pending ON commands(status,created_at,id);
"""


class ControlError(ValueError):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.status = code, status


def _exponential_backoff(attempts: int, base: float, cap: float, max_exp: int) -> float:
    return min(cap, base * 2 ** min(max(attempts - 1, 0), max_exp))


def retry_delay(attempts: int) -> float:
    """失败退避：5 秒起按次翻倍，最长 5 分钟，不限次数。"""
    return _exponential_backoff(attempts, RETRY_BASE_SEC, RETRY_MAX_SEC, 6)


def boot_retry_delay(attempts: int) -> float:
    """开机失败退避：30 秒起按次翻倍，最长 5 分钟，不限次数。"""
    return _exponential_backoff(attempts, BOOT_RETRY_BASE_SEC, BOOT_RETRY_MAX_SEC, 4)


def requested_total_duration(command_json: str | None) -> float:
    try:
        cmd = json.loads(command_json or "[]")
        return float(cmd[cmd.index("--total-duration") + 1])
    except (ValueError, IndexError, TypeError):
        return 0.0


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
        if tables and version != SCHEMA_VERSION:
            self.db.close()
            raise ValueError("数据库不是当前调度器格式；拒绝覆盖或自动迁移，请使用新的独立数据目录")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def transaction(self):
        with self._lock:
            nested = self.db.in_transaction
            savepoint = "s" + uuid.uuid4().hex
            self.db.execute("SAVEPOINT " + savepoint if nested else "BEGIN IMMEDIATE")
            try:
                yield self.db
                if nested:
                    self.db.execute("RELEASE " + savepoint)
                else:
                    self.db.commit()
            except BaseException:
                if nested:
                    self.db.execute("ROLLBACK TO " + savepoint)
                    self.db.execute("RELEASE " + savepoint)
                else:
                    self.db.rollback()
                raise

    @contextmanager
    def read_snapshot(self):
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            yield db
        finally:
            db.close()

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
            old = db.execute("SELECT * FROM targets WHERE canonical=?", (canonical,)).fetchone()
            if old:
                if not old["enabled"]:
                    db.execute("UPDATE targets SET enabled=1,auto_record=?,updated_at=?,control_revision=control_revision+1 WHERE id=?",
                               (int(auto_record), now, old["id"]))
                return old["id"]
            key = uuid.uuid4().hex
            db.execute("INSERT INTO targets(id,raw,canonical,auto_record,allow_unconfirmed,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                       (key, raw, canonical, int(auto_record), int(allow_unconfirmed), now, now))
            return key

    def begin_observation(self, target_id, now):
        with self.transaction() as db:
            db.execute("UPDATE targets SET latest_observation_seq=latest_observation_seq+1 WHERE id=?", (target_id,))
            t = dict(db.execute("SELECT * FROM targets WHERE id=?", (target_id,)).fetchone())
            return dict(target_id=target_id, seq=t["latest_observation_seq"], revision=t["control_revision"],
                        session_key=t["session_key"], observed_at=now)

    def apply_observation(self, meta, room, ended_interval_sec=10):
        with self.transaction() as db:
            t = db.execute("SELECT * FROM targets WHERE id=?", (meta["target_id"],)).fetchone()
            if not t or not t["enabled"] or t["control_revision"] != meta["revision"] or \
                    t["latest_observation_seq"] != meta["seq"] or t["applied_observation_seq"] >= meta["seq"]:
                return False
            db.execute("UPDATE targets SET applied_observation_seq=? WHERE id=?", (meta["seq"], t["id"]))
            self._observe(t["id"], room, meta["observed_at"], ended_interval_sec=ended_interval_sec)
            return True

    def _observe(self, target_id: str, room: ResolvedRoom, now: float,
                ended_confirmations=2, ended_interval_sec=10.0):
        """未知保留可信状态；两次间隔达标的明确下播才改变可信状态。"""
        value = getattr(room.status, "value", room.status)
        value = value if value in ("live", "ended") else "unknown"
        with self.transaction() as db:
            t = db.execute("SELECT * FROM targets WHERE id=?", (target_id,)).fetchone()
            if not t:
                return
            canonical = room.web_rid or t["canonical"]
            duplicate = db.execute("SELECT id FROM targets WHERE canonical=? AND id<>?", (canonical, target_id)).fetchone()
            if duplicate:
                db.execute("UPDATE targets SET enabled=0,record_session_key=NULL,note=?,updated_at=?,control_revision=control_revision+1 WHERE id=?", ("duplicate_of:" + duplicate[0], now, target_id))
                return
            db.execute("UPDATE targets SET observed_status=?,observed_at=?,canonical=?,updated_at=? WHERE id=?",
                       (value, now, canonical, now, target_id))
            if value == "unknown":
                db.execute("UPDATE targets SET ended_count=0,ended_since=0 WHERE id=?", (target_id,))
                return
            if value == "live":
                room_id = room.room_id if valid_identity_id(room.room_id) else None
                uid = getattr(room, 'anchor_uid', None)
                uid = uid if valid_identity_id(uid) else None
                session = ('room:' + room_id) if room_id else (t['session_key'] if t['trusted_status'] == 'live' else None)
                new_session = bool(room_id) and (t['trusted_status'] != 'live' or session != t['session_key'])
                db.execute("""UPDATE targets SET trusted_status='live',confirmed_at=?,room_id=?,anchor_uid=?,nickname=?,
                    session_key=?,ended_count=0,ended_since=0 WHERE id=?""",
                    (now, room_id, uid, room.nickname, session, target_id))
                if new_session:
                    db.execute("UPDATE targets SET record_session_key=NULL,suppressed_session=NULL,attempts=0,retry_at=0,control_revision=control_revision+1 WHERE id=?", (target_id,))
            else:
                count = t["ended_count"] + 1
                since = t["ended_since"] if t["ended_count"] else now
                db.execute("UPDATE targets SET ended_count=?,ended_since=? WHERE id=?", (count, since, target_id))
                if count >= ended_confirmations and now - since >= ended_interval_sec:
                    db.execute("""UPDATE targets SET trusted_status='ended',confirmed_at=?,suppressed_session=NULL,
                        record_session_key=NULL,attempts=0,retry_at=0,control_revision=control_revision+1 WHERE id=?""", (now, target_id))

    def _instance_busy_or_reconcile(self, db, row, *, allow_booting: bool = False) -> bool:
        if row["job_id"] or row["status"] == "reconcile":
            return True
        if not allow_booting and row["status"] == "booting":
            return True
        return bool(db.execute(
            "SELECT 1 FROM jobs WHERE vmindex=? AND status IN ('pending','starting','running','stopping','needs_reconcile')",
            (row["vmindex"],),
        ).fetchone())

    def sync_instances(self, slots, observed_at: float, preset: set[int] | None = None, *, clear_booting: bool = False):
        """按一次扫描结果同步全部探测实例，保留既有的启用配置，并清理已失联的未启用旧记录。

        传入 preset 时（仅启动时可选命令行预设），将预设列表启用并停用无占用的预设外实例；
        有任务占用、正在开机（除非 clear_booting=True）、待对账、或扫描开始后状态又被改写的实例保持原状态。
        """
        found = {slot.vmindex: slot for slot in slots}
        with self.transaction() as db:
            if preset is not None:
                for row in list(db.execute("SELECT * FROM instances")):
                    index = row["vmindex"]
                    if index in preset:
                        db.execute("""UPDATE instances SET enabled=1,
                            boot_attempts=CASE WHEN enabled=0 THEN 0 ELSE boot_attempts END,
                            boot_retry_at=CASE WHEN enabled=0 THEN 0 ELSE boot_retry_at END
                            WHERE vmindex=?""", (index,))
                    elif not self._instance_busy_or_reconcile(db, row, allow_booting=clear_booting):
                        db.execute("UPDATE instances SET enabled=0 WHERE vmindex=?", (index,))

            for index in sorted(found):
                slot = found[index]
                observed = getattr(slot.status, "value", slot.status)
                status = "idle" if observed == "idle" else "offline"
                reason = None if status == "idle" else "android_not_started"
                name = slot.name or ""
                existing = db.execute("SELECT * FROM instances WHERE vmindex=?", (index,)).fetchone()
                if not existing:
                    db.execute(
                        "INSERT INTO instances(vmindex,name,status,enabled,reason,updated_at) VALUES(?,?,?,?,?,?)",
                        (index, name, status, 1 if (preset is not None and index in preset) else 0, reason, observed_at),
                    )
                    continue
                if existing["updated_at"] > observed_at or self._instance_busy_or_reconcile(db, existing, allow_booting=clear_booting):
                    continue
                is_idle = status == "idle"
                # 开机失败冷却期内保留 launch_failed 原因码，避免周期刷新抹掉提示。
                keep_failed = not is_idle and existing["reason"] == "launch_failed" and existing["boot_attempts"] > 0
                next_reason = "launch_failed" if keep_failed else reason
                boot_attempts = 0 if is_idle else existing["boot_attempts"]
                boot_retry_at = 0 if is_idle else existing["boot_retry_at"]
                db.execute(
                    "UPDATE instances SET name=?,status=?,reason=?,boot_attempts=?,boot_retry_at=?,updated_at=? WHERE vmindex=?",
                    (name or existing["name"], status, next_reason, boot_attempts, boot_retry_at, observed_at, index),
                )

            if preset is not None:
                for index in sorted(preset):
                    if index in found:
                        continue
                    if not db.execute("SELECT 1 FROM instances WHERE vmindex=?", (index,)).fetchone():
                        db.execute(
                            "INSERT INTO instances(vmindex,name,status,enabled,reason,updated_at) VALUES(?,?,?,1,?,?)",
                            (index, "", "offline", "not_discovered", observed_at),
                        )

            for row in list(db.execute("SELECT * FROM instances")):
                index = row["vmindex"]
                if index in found or self._instance_busy_or_reconcile(db, row, allow_booting=clear_booting):
                    continue
                if not row["enabled"]:
                    db.execute("DELETE FROM instances WHERE vmindex=?", (index,))
                    continue
                if row["updated_at"] <= observed_at:
                    db.execute(
                        "UPDATE instances SET status='offline',reason='not_discovered',updated_at=? WHERE vmindex=?",
                        (observed_at, index),
                    )

    def claim_boot(self, now: float, freshness_sec=60) -> int | None:
        """存在可派发排队任务且无可用空闲槽位时，按顺序认领 1 台未开机已启用实例进入 booting。"""
        with self.transaction() as db:
            pending = db.execute(
                "SELECT t.* FROM jobs j JOIN targets t ON t.id=j.target_id "
                "WHERE j.status='pending' AND t.enabled=1 AND t.session_key=j.session_key AND t.retry_at<=?",
                (now,),
            ).fetchall()
            if not any(identity_admission(t, now, freshness_sec) is None for t in pending):
                return None
            if db.execute("SELECT 1 FROM instances WHERE enabled=1 AND status='idle' AND job_id IS NULL LIMIT 1").fetchone():
                return None
            if db.execute("SELECT 1 FROM instances WHERE status='booting' LIMIT 1").fetchone():
                return None
            slot = db.execute(
                "SELECT vmindex FROM instances WHERE enabled=1 AND status='offline' AND job_id IS NULL "
                "AND boot_retry_at<=? AND reason IN ('android_not_started','launch_failed') "
                "ORDER BY boot_attempts ASC, vmindex ASC LIMIT 1",
                (now,),
            ).fetchone()
            if not slot:
                return None
            vmindex = int(slot["vmindex"])
            db.execute(
                "UPDATE instances SET status='booting',reason='booting',updated_at=? "
                "WHERE vmindex=? AND status='offline' AND job_id IS NULL",
                (now, vmindex),
            )
            return vmindex

    def finish_boot(self, vmindex: int, ok: bool, now: float, *, cancelled: bool = False):
        """收尾一次按需开机：成功进入空闲并清零冷却；失败回落 offline 并叠加退避冷却。"""
        with self.transaction() as db:
            slot = db.execute(
                "SELECT boot_attempts FROM instances WHERE vmindex=? AND status='booting' AND job_id IS NULL",
                (vmindex,),
            ).fetchone()
            if not slot:
                return
            if cancelled and not ok:
                db.execute("UPDATE instances SET status='offline',reason='boot_interrupted',updated_at=? WHERE vmindex=?",
                           (now, vmindex))
                return
            if ok:
                db.execute(
                    "UPDATE instances SET status='idle',reason=NULL,boot_attempts=0,boot_retry_at=0,updated_at=? "
                    "WHERE vmindex=? AND status='booting' AND job_id IS NULL",
                    (now, vmindex),
                )
                return
            attempts = int(slot["boot_attempts"]) + 1
            retry_at = now + boot_retry_delay(attempts)
            db.execute(
                "UPDATE instances SET status='offline',reason='launch_failed',boot_attempts=?,boot_retry_at=?,updated_at=? "
                "WHERE vmindex=? AND status='booting' AND job_id IS NULL",
                (attempts, retry_at, now, vmindex),
            )

    def create_pending(self, target_id: str, now: float) -> str | None:
        with self.transaction() as db:
            t = db.execute("SELECT * FROM targets WHERE id=?", (target_id,)).fetchone()
            if not t or not t["session_key"]:
                return None
            old = db.execute("SELECT id FROM jobs WHERE target_id=? AND status IN ('pending','starting','running','stopping','needs_reconcile')", (target_id,)).fetchone()
            if old:
                return old[0]
            key = uuid.uuid4().hex
            db.execute("UPDATE targets SET record_session_key=session_key,updated_at=?,control_revision=control_revision+1 WHERE id=? AND record_session_key IS NOT session_key", (now, target_id))
            db.execute("INSERT INTO jobs(id,target_id,session_key,room_id,status,created_at) VALUES(?,?,?,?,'pending',?)",
                       (key, target_id, t["session_key"], t["room_id"], now))
            return key

    def claim(self, job_id: str, vmindex: int, now: float) -> bool:
        with self.transaction() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            slot = db.execute("SELECT * FROM instances WHERE vmindex=?", (vmindex,)).fetchone()
            if not job or job["status"] != "pending" or not slot or not slot["enabled"] or slot["status"] != "idle" or slot["job_id"]:
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
            db.execute("UPDATE targets SET attempts=attempts+1,updated_at=? WHERE id=?", (now, job[0]))

    def request_stop(self, job_id: str, reason: str):
        with self.transaction() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job or job["status"] not in ACTIVE:
                return
            if reason == "manual_stop":
                db.execute("UPDATE targets SET suppressed_session=?,record_session_key=NULL,updated_at=?,control_revision=control_revision+1 WHERE id=? AND session_key=?",
                           (job["session_key"], time.time(), job["target_id"], job["session_key"]))
            if job["status"] == "needs_reconcile":
                return
            if job["status"] == "pending":
                db.execute("UPDATE jobs SET status='cancelled',stop_reason=?,ended_at=? WHERE id=?", (reason, time.time(), job_id))
            else:
                db.execute("UPDATE jobs SET status='stopping',stop_reason=COALESCE(stop_reason,?) WHERE id=?", (reason, job_id))

    def record_event(self, job_id: str, event: dict, now: float):
        payload = json.dumps(event, sort_keys=True, ensure_ascii=False)
        # 无事件编号的旧式测试事件直接比较规范化内容，不引入摘要算法。
        key = job_id + ":" + str(event.get("event_id") or payload)
        kind = event.get("event", "")
        stamp = event.get("timestamp")
        stamp = stamp if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and math.isfinite(stamp) else now
        if event.get("job_id") not in (None, "", job_id):
            raise ValueError("Worker 事件 job_id 不匹配")
        quality_issue = None
        with self.transaction() as db:
            if db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?)", (key, job_id, kind, payload, now)).rowcount == 0:
                return
            if kind == "heartbeat":
                old = json.loads(db.execute("SELECT heartbeat_json FROM jobs WHERE id=?", (job_id,)).fetchone()[0] or "{}")
                numeric = [event.get(k) for k in ("elapsed_sec", "fps")]
                valid = all(v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0) for v in numeric)
                if valid and stamp >= old.get("timestamp", 0):
                    db.execute("UPDATE jobs SET heartbeat_json=? WHERE id=?", (json.dumps(dict(event, timestamp=stamp), ensure_ascii=False), job_id))
            if kind in ("segment_started", "segment_finished", "segment_aborted", "segment_quality_warning"):
                idx = int(event["segment_index"])
                old = db.execute("SELECT * FROM segments WHERE job_id=? AND segment_index=?", (job_id, idx)).fetchone()
                db.execute("INSERT OR IGNORE INTO segments(job_id,segment_index,created_at,updated_at) VALUES(?,?,?,?)", (job_id, idx, stamp, stamp))
                db.execute("UPDATE segments SET updated_at=MAX(updated_at,?) WHERE job_id=? AND segment_index=?", (stamp, job_id, idx))
                if kind == "segment_started":
                    db.execute("UPDATE segments SET started=1,raw_path=?,started_at=? WHERE job_id=? AND segment_index=?", (event.get("file_path"), stamp, job_id, idx))
                    # Popen 成功不等于开始录制；首片开始后才将任务标为 running，并把失败计数清零。
                    if db.execute("UPDATE jobs SET status='running' WHERE id=? AND status='starting'", (job_id,)).rowcount:
                        db.execute("UPDATE targets SET attempts=0 WHERE id=(SELECT target_id FROM jobs WHERE id=?)", (job_id,))
                elif kind == "segment_quality_warning":
                    quality_issue = self._record_quality_warning(db, job_id, idx, old, event, stamp)
                else:
                    status = "finished" if kind == "segment_finished" else "aborted"
                    if old and old["status"] in ("finished", "aborted"):
                        if old["status"] != status or old["file_path"] != event.get("file_path"):
                            raise ValueError("同一切片收到冲突终态")
                        return
                    db.execute("""UPDATE segments SET status=?,file_path=?,duration_sec=?,file_size_mb=?,is_final=?,reason=?,ended_at=?
                        WHERE job_id=? AND segment_index=?""", (status, event.get("file_path"), event.get("duration_sec"),
                        event.get("file_size_mb"), int(bool(event.get("is_final"))), event.get("reason"), stamp, job_id, idx))
        if quality_issue:
            logger.warning("迟到质量事件收集不完整：job_id=%s event_id=%s segment_index=%s reason=%s",
                           job_id, event.get("event_id"), idx, quality_issue)

    def _record_quality_warning(self, db, job_id, idx, old, event, stamp):
        """保留每条独立风险；只更新任务结果，不重跑实例释放或目标重试。"""
        risks = json.loads(old["risk_json"]) if old and old["risk_json"] else []
        risks.append(dict(event, timestamp=stamp))
        db.execute("UPDATE segments SET risk_json=? WHERE job_id=? AND segment_index=?",
                   (json.dumps(risks, sort_keys=True, ensure_ascii=False), job_id, idx))
        job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job["status"] not in ("finished", "finished_with_warnings", "failed", "cancelled"):
            return None
        success = job["status"] in ("finished", "finished_with_warnings")
        issue = ("未知切片" if old is None else "切片未开始" if not old["started"] else
                 "切片未终结" if old["status"] not in ("finished", "aborted") else
                 "原完整性标记异常" if success and not job["collection_complete"] else None)
        if issue:
            db.execute("UPDATE jobs SET collection_complete=0 WHERE id=?", (job_id,))
            if success:
                reason = f"event_collection_incomplete: 迟到质量事件，切片 {idx}：{issue}"
                if job["error_reason"]:
                    reason += f"；原原因：{job['error_reason']}"
                db.execute("UPDATE jobs SET status='failed',exit_code=?,error_reason=? WHERE id=?",
                           (job["exit_code"] or 30, reason, job_id))
            return issue
        db.execute("UPDATE jobs SET status='finished_with_warnings' WHERE id=? AND status='finished' AND exit_code=0", (job_id,))
        return None

    def finish_job(self, job_id: str, code: int, now: float, collection_complete: bool,
                   reason: str = "", hold_instance: bool = False, ended_at: float | None = None):
        """ended_at：Worker 实际退出时刻（对账时取自日志）；缺省为 now。退避仍从 now 起算。"""
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
                       (status, code, reason or None, ended_at or now, int(collection_complete and not incomplete), job_id))
            if job["vmindex"] is not None:
                slot_status = "reconcile" if hold_instance else ("offline" if code == 20 else "idle")
                db.execute("UPDATE instances SET status=?,job_id=?,reason=?,updated_at=? WHERE vmindex=? AND (job_id IS NULL OR job_id=?)",
                           (slot_status, job_id if hold_instance else None, reason or None, now, job["vmindex"], job_id))
            stop = job["stop_reason"]
            if stop == "manual_stop" or (code == 0 and requested_total_duration(job["command_json"]) > 0):
                db.execute("UPDATE targets SET suppressed_session=?,record_session_key=NULL,updated_at=?,control_revision=control_revision+1 WHERE id=? AND session_key=?", (job["session_key"], now, job["target_id"], job["session_key"]))
            elif code == 9:
                db.execute("UPDATE targets SET note=?,record_session_key=NULL,updated_at=?,control_revision=control_revision+1 WHERE id=? AND session_key=?",
                           (NEEDS_FIX + (reason or "target_invalid"), now, job["target_id"], job["session_key"]))
            elif stop not in SETTLED_STOPS:
                attempts = db.execute("SELECT attempts FROM targets WHERE id=?", (job["target_id"],)).fetchone()[0]
                db.execute("UPDATE targets SET retry_at=?,attempts=MAX(1,attempts),updated_at=? WHERE id=? AND session_key=?", (now + retry_delay(attempts), now, job["target_id"], job["session_key"]))

    def mark_waiting_exit(self, job_id: str):
        """旧主控遗留的本任务进程仍在运行：等它发现主控断开后自行封口退出。"""
        with self.transaction() as db:
            db.execute("UPDATE jobs SET status='stopping',error_reason='waiting_old_worker_exit' WHERE id=?", (job_id,))
            db.execute("UPDATE instances SET status='busy' WHERE job_id=?", (job_id,))

    def _check_instance_control(self, db, vmindex: int, enabled: bool):
        """校验实例是否存在，以及停用时是否处于占用、开机或待对账状态。"""
        slot = db.execute("SELECT * FROM instances WHERE vmindex=?", (vmindex,)).fetchone()
        if not slot:
            raise ControlError("instance_not_found", "实例不存在", 404)
        if enabled:
            return slot
        if slot["status"] == "reconcile" or db.execute(
            "SELECT 1 FROM jobs WHERE vmindex=? AND status='needs_reconcile'", (vmindex,)
        ).fetchone():
            raise ControlError("reconcile_required", "实例关联待对账任务，请先对账后再停用")
        if slot["status"] == "booting":
            raise ControlError("instance_busy", "实例正在开机中，请稍后再停用")
        if slot["job_id"] or slot["status"] == "busy" or db.execute(
            "SELECT 1 FROM jobs WHERE vmindex=? AND status IN ('pending','starting','running','stopping')",
            (vmindex,),
        ).fetchone():
            raise ControlError("instance_busy", "实例正在占用中，请先停止对应录播任务后再停用")
        return slot

    def enqueue_command(self, kind: str, subject: str, now=None, *, payload=None, request_id=None) -> str:
        if kind not in {"add", "record", "stop", "stop_target", "remove", "update", "reconcile", "instance"}:
            raise ValueError("未知控制命令")
        request_id = str(uuid.UUID(request_id)) if request_id else str(uuid.uuid4())
        payload = payload or {}
        key = uuid.uuid4().hex
        with self.transaction() as db:
            old = db.execute("SELECT * FROM commands WHERE request_id=?", (request_id,)).fetchone()
            if old:
                if (old["kind"], old["subject"], json.loads(old["payload_json"])) != (kind, subject, payload):
                    raise ControlError("idempotency_conflict", "同一请求编号不能用于不同操作")
                return old["id"]
            self.validate_subject(kind, subject)
            if kind == "record":
                self.existing_record(subject)
            elif kind == "stop" and self.row("SELECT status FROM jobs WHERE id=?", (subject,))["status"] == "needs_reconcile":
                raise ControlError("reconcile_required", "任务需对账，不向未知进程发送信号")
            elif kind == "stop_target" and self.row("SELECT session_key FROM targets WHERE id=?", (subject,))["session_key"] != payload.get("session_key"):
                raise ControlError("session_changed", "主播场次已经变化，请刷新后重试")
            elif kind == "instance":
                self._check_instance_control(db, int(subject), bool(payload.get("enabled")))
            db.execute("INSERT INTO commands(id,request_id,kind,subject,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                       (key, request_id, kind, subject, json.dumps(payload, ensure_ascii=False, sort_keys=True), time.time() if now is None else now))
        return key

    def validate_subject(self, kind, subject):
        if kind in {"record", "stop_target", "remove", "update"}:
            if not self.row("SELECT id FROM targets WHERE id=? AND enabled=1", (subject,)):
                raise ControlError("target_not_found", "主播不存在或已移除", 404)
        elif kind in {"stop", "reconcile"} and subject:
            if not self.row("SELECT id FROM jobs WHERE id=?", (subject,)):
                raise ControlError("job_not_found", "任务不存在", 404)
        elif kind == "instance":
            try:
                vmindex = int(subject)
            except (TypeError, ValueError) as exc:
                raise ControlError("invalid_parameters", "实例编号非法", 422) from exc
            if not self.row("SELECT vmindex FROM instances WHERE vmindex=?", (vmindex,)):
                raise ControlError("instance_not_found", "实例不存在", 404)

    def complete_command(self, key, result=None, error_code=None, message=None, now=None):
        with self.transaction() as db:
            db.execute("UPDATE commands SET status=?,result_json=?,error_code=?,message=?,finished_at=? WHERE id=? AND status IN ('pending','running')",
                       ("failed" if error_code else "succeeded", json.dumps(result, ensure_ascii=False) if result is not None else None,
                        error_code, message or ("操作未生效" if error_code else "调度状态已生效，媒体结果请查看任务"),
                        time.time() if now is None else now, key))

    def interrupt_commands(self, now):
        with self.transaction() as db:
            db.execute("UPDATE commands SET status='interrupted',error_code='service_interrupted',message='服务关闭或重启，尚未应用的操作已中断',finished_at=? WHERE status IN ('pending','running')", (now,))

    def active_job(self, target_id):
        return self.row("SELECT * FROM jobs WHERE target_id=? AND status IN ('pending','starting','running','stopping','needs_reconcile')", (target_id,))

    def existing_record(self, target_id):
        job = self.active_job(target_id)
        if job and job["status"] == "stopping":
            raise ControlError("job_stopping", "任务正在停止，请结束后重试")
        if job and job["status"] == "needs_reconcile":
            raise ControlError("reconcile_required", "遗留任务需要对账")
        return dict(target_id=target_id, job_id=job["id"], outcome="existing") if job else None

    def apply_control(self, command, now):
        """调用者持有命令事务；这里只保存意图，不执行设备或网络动作。"""
        kind, subject = command["kind"], command["subject"]
        payload = json.loads(command["payload_json"])
        self.validate_subject(kind, subject)
        db = self.db
        if kind == "add":
            items = []
            for raw in payload["targets"]:
                raw = raw.strip()
                if not raw or not (re.fullmatch(r"[a-zA-Z0-9._~-]+", raw) or
                        re.search(r"https?://(?:live\.douyin\.com/[\w.~-]+|v\.douyin\.com/[a-zA-Z0-9]+)", raw)):
                    items.append(dict(input=raw, target_id=None, outcome="error", message="地址为空或不支持", code="invalid_target"))
                    continue
                old = self.row("SELECT * FROM targets WHERE canonical=?", (canonical_target(raw),))
                target_id = self.add_target(raw, not payload["manual_only"], payload["allow_unconfirmed_identity"], now)
                outcome = "created" if not old else "existing" if old["enabled"] else "restored"
                items.append(dict(input=raw, target_id=target_id, outcome=outcome, message={"created":"已新增监控", "existing":"已存在", "restored":"已恢复监控"}[outcome], code=None))
            return dict(items=items)
        if kind == "update":
            db.execute("UPDATE targets SET auto_record=?,updated_at=?,control_revision=control_revision+1 WHERE id=?", (int(payload["auto_record"]), now, subject))
            return dict(target_id=subject, auto_record=payload["auto_record"])
        if kind == "remove":
            db.execute("UPDATE targets SET enabled=0,record_session_key=NULL,updated_at=?,control_revision=control_revision+1 WHERE id=?", (now, subject))
            jobs = self.rows("SELECT * FROM jobs WHERE target_id=? AND status IN ('pending','starting','running','stopping')", (subject,))
            for job in jobs:
                self.request_stop(job["id"], "target_removed")
            return dict(target_id=subject, changed=True, affected_job_ids=[j["id"] for j in jobs])
        if kind == "stop":
            job = self.row("SELECT * FROM jobs WHERE id=?", (subject,))
            if job["status"] == "needs_reconcile":
                raise ControlError("reconcile_required", "任务需对账，不向未知进程发送信号")
            outcome = ("already_terminal" if job["status"] not in ACTIVE else "already_stopping" if job["status"] == "stopping" else
                       "cancelled" if job["status"] == "pending" else "stop_requested")
            if job["status"] in ACTIVE:
                self.request_stop(subject, "manual_stop")
            return dict(job_id=subject, outcome=outcome)
        if kind == "stop_target":
            t = self.row("SELECT * FROM targets WHERE id=?", (subject,))
            session = payload["session_key"]
            if t["session_key"] != session:
                raise ControlError("session_changed", "主播场次已经变化，请刷新后重试")
            jobs = self.rows("SELECT * FROM jobs WHERE target_id=? AND session_key=? AND status IN ('pending','starting','running','stopping','needs_reconcile')", (subject, session))
            affected = [j["id"] for j in jobs if j["status"] != "needs_reconcile"]
            reconcile = [j["id"] for j in jobs if j["status"] == "needs_reconcile"]
            already = t["suppressed_session"] == session and not t["record_session_key"] and all(j["status"] in ("stopping", "needs_reconcile") for j in jobs)
            db.execute("UPDATE targets SET record_session_key=NULL,suppressed_session=?,updated_at=?,control_revision=control_revision+1 WHERE id=?", (session, now, subject))
            for job in jobs:
                if job["status"] != "needs_reconcile":
                    self.request_stop(job["id"], "manual_stop")
            return dict(target_id=subject, session_key=session, outcome="already_stopped" if already else "stop_requested" if affected else "intent_cancelled", affected_job_ids=affected, reconcile_job_ids=reconcile)
        if kind == "reconcile":
            jobs = self.rows("SELECT id FROM jobs WHERE status='needs_reconcile'" + (" AND id=?" if subject else ""), (subject,) if subject else ())
            for job in jobs:
                db.execute("UPDATE jobs SET reconcile_requested=1 WHERE id=?", (job["id"],))
            return dict(job_ids=[j["id"] for j in jobs], outcome="reconcile_requested" if jobs else "not_required")
        if kind == "instance":
            vmindex = int(subject)
            enabled = bool(payload["enabled"])
            slot = self._check_instance_control(db, vmindex, enabled)
            if not enabled and slot["reason"] == "not_discovered":
                db.execute("DELETE FROM instances WHERE vmindex=?", (vmindex,))
                return dict(vmindex=vmindex, enabled=False, cleaned=True)
            if enabled:
                reason = "android_not_started" if (slot["status"] == "offline" and slot["reason"] == "launch_failed") else slot["reason"]
                db.execute("UPDATE instances SET enabled=1,boot_attempts=0,boot_retry_at=0,reason=?,updated_at=? WHERE vmindex=?",
                           (reason, now, vmindex))
            else:
                db.execute("UPDATE instances SET enabled=0,updated_at=? WHERE vmindex=?", (now, vmindex))
            return dict(vmindex=vmindex, enabled=enabled, cleaned=False)
        raise ValueError("未知同步命令")

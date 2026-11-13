"""控制台只读投影：一次响应一个 SQLite 快照，固定筛选与排序。"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time

from src.orchestrator.storage import ACTIVE, NEEDS_FIX, ControlError
from src.orchestrator.reasons import reason_object

ACTIVE_SQL = "('pending','starting','running','stopping','needs_reconcile')"
TARGET_SQL = f"""SELECT t.*, j.id active_job_id,j.status active_job_status,
 (SELECT error_reason FROM jobs f WHERE f.target_id=t.id AND f.session_key=t.session_key
  AND (f.status IN ('failed','needs_reconcile') OR (f.status IN ('finished','finished_with_warnings') AND f.stop_reason IS NULL)) ORDER BY ended_at DESC,id DESC LIMIT 1) last_failure_reason,
 (SELECT ended_at FROM jobs f WHERE f.target_id=t.id AND f.session_key=t.session_key
  AND (f.status IN ('failed','needs_reconcile') OR (f.status IN ('finished','finished_with_warnings') AND f.stop_reason IS NULL)) ORDER BY ended_at DESC,id DESC LIMIT 1) failure_at,
 (SELECT COALESCE(ended_at,created_at) FROM jobs f WHERE f.target_id=t.id AND f.exit_code=9 ORDER BY ended_at DESC,id DESC LIMIT 1) fix_at,
 (t.record_session_key IS NOT NULL AND t.record_session_key=t.session_key) recording_intent,
 (t.suppressed_session IS NOT NULL AND t.suppressed_session=t.session_key) suppressed,
 CASE WHEN t.note LIKE 'needs_fix: %' THEN substr(t.note,12) ELSE NULL END needs_fix_reason
 FROM targets t LEFT JOIN jobs j ON j.target_id=t.id AND j.status IN {ACTIVE_SQL}"""
TARGET_VIEW = f"""WITH base AS ({TARGET_SQL}), targets_view AS (SELECT *,
 (recording_intent AND attempts>0 AND failure_at IS NOT NULL AND NOT suppressed AND needs_fix_reason IS NULL
  AND COALESCE(active_job_status,'') NOT IN ('running','stopping','needs_reconcile')) is_retrying FROM base)
 SELECT * FROM targets_view"""
JOB_SQL = """SELECT j.*,t.nickname,t.raw target_raw,
 (SELECT min(started_at) FROM segments s WHERE s.job_id=j.id) recording_started_at,
 (SELECT count(*) FROM segments s WHERE s.job_id=j.id AND s.status='finished') finished_segment_count,
 (SELECT count(*) FROM segments s WHERE s.job_id=j.id AND s.status='aborted') aborted_segment_count,
 (SELECT json_group_array(json(payload)) FROM
   (SELECT e.payload FROM events e WHERE e.job_id=j.id
    AND e.rowid >= (SELECT max(start.rowid) FROM events start WHERE start.job_id=j.id AND start.kind='stall_detected')
    AND e.kind IN ('stall_detected','stall_healed','room_navigation','worker_stopped','worker_failed')
    ORDER BY e.rowid)) recovery_events_json
 FROM jobs j LEFT JOIN targets t ON t.id=j.target_id"""
SEGMENT_SQL = """SELECT s.*,j.target_id,t.nickname,t.raw target_raw,
 COALESCE(s.started_at,s.created_at) sort_time
 FROM segments s JOIN jobs j ON j.id=s.job_id LEFT JOIN targets t ON t.id=j.target_id"""


def fields(row, names):
    return {name: row[name] for name in names.split(",")}


def target_object(row):
    data = fields(row, "id,raw,nickname,created_at,updated_at,trusted_status,observed_status,observed_at,confirmed_at,auto_record,room_id,anchor_uid,session_key,recording_intent,suppressed,needs_fix_reason,note,attempts,retry_at,is_retrying,last_failure_reason,active_job_id,active_job_status")
    for key in ("auto_record", "recording_intent", "suppressed", "is_retrying"):
        data[key] = bool(data[key])
    for key in ("observed_at", "confirmed_at"):
        data[key] = data[key] or None
    data["needs_fix_reason"] = reason_object(data["needs_fix_reason"], "target_invalid")
    data["last_failure_reason"] = reason_object(data["last_failure_reason"])
    return data


def recovery_context(row):
    """摘要来自持久化事件，不从最终 cancelled 倒推中断原因。"""
    events = json.loads(row["recovery_events_json"] or "[]")
    if not events:
        return None
    context = dict(trigger=reason_object(events[0].get("reason")), last_failure=None, outcome="recovering")
    failures = {"retry", "retry_picture", "page_changed", "budget_exhausted", "device_lost", "navigation_exhausted"}
    for event in events[1:]:
        kind, reason = event.get("event"), event.get("reason")
        if kind == "stall_healed":
            context["outcome"] = "recovered"
        elif kind == "room_navigation":
            if reason == "cancelled":
                context["outcome"] = "cancelled"
            elif event.get("action") in failures and reason:
                context["last_failure"] = reason_object(reason)
                if event.get("action") == "navigation_exhausted":
                    context["outcome"] = "failed"
        elif kind in ("worker_stopped", "worker_failed") and context["outcome"] != "recovered":
            context["outcome"] = "cancelled" if reason in ("cancelled", "stop_requested") else "failed"
    if context["outcome"] == "recovering" and row["ended_at"] is not None:
        context["outcome"] = "failed"
    return context


def job_object(row):
    data = fields(row, "id,target_id,nickname,target_raw,vmindex,status,created_at,started_at,recording_started_at,ended_at,exit_code,stop_reason,error_reason,finished_segment_count,aborted_segment_count,reconcile_requested")
    for key in ("stop_reason", "error_reason"):
        data[key] = reason_object(data[key])
    heartbeat = json.loads(row["heartbeat_json"] or "{}")
    for key in ("elapsed_sec", "fps", "room_identity_status"):
        data[key] = heartbeat.get(key)
    data["heartbeat_at"] = heartbeat.get("timestamp")
    data["reconcile_requested"] = bool(data["reconcile_requested"])
    data["recovery_context"] = recovery_context(row)
    return data


def segment_object(row):
    data = fields(row, "job_id,target_id,nickname,target_raw,segment_index,status,file_path,duration_sec,file_size_mb,is_final,reason,created_at,started_at,ended_at,updated_at")
    data["is_final"] = bool(data["is_final"])
    data["reason"] = reason_object(data["reason"])
    if row["status"] == "started":
        data["file_path"] = row["file_path"] or row["raw_path"]
    data["quality_flags"] = [{"flag": r.get("flag"), "suspected_start_time": r.get("suspected_start_time")}
                             for r in json.loads(row["risk_json"] or "[]")]
    return data


def command_object(row):
    data = fields(row, "id,request_id,kind,subject,status,error_code,message,created_at,started_at,finished_at")
    data["result"] = json.loads(row["result_json"]) if row["result_json"] else None
    return data


def page(db, sql, params, order, convert, limit, offset, now):
    total = db.execute(f"SELECT count(*) FROM ({sql})", params).fetchone()[0]
    items = [convert(r) for r in db.execute(sql + " ORDER BY " + order + " LIMIT ? OFFSET ?", (*params, limit, offset))]
    return dict(items=items, total=total, limit=limit, offset=offset, generated_at=now)


class Queries:
    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock

    def listing(self, kind, filters, limit=50, offset=0):
        conditions, values = [], []
        def condition(sql, value):
            conditions.append(sql)
            values.append(value)
        if kind == "targets":
            sql, order, convert = TARGET_VIEW, "created_at DESC,id DESC", target_object
            conditions.append("enabled=1")
            for key in ("trusted_status", "auto_record"):
                if filters.get(key) is not None:
                    condition(key + "=?", filters[key])
            if filters.get("q") is not None:
                conditions.append("(instr(COALESCE(nickname,''),?)>0 OR instr(raw,?)>0)")
                values.extend([filters["q"]] * 2)
            if filters.get("has_notice") is not None:
                condition("(suppressed OR needs_fix_reason IS NOT NULL OR is_retrying)=?", filters["has_notice"])
        elif kind == "instances":
            sql = "SELECT i.*,j.target_id,t.nickname FROM instances i LEFT JOIN jobs j ON j.id=i.job_id LEFT JOIN targets t ON t.id=j.target_id"
            def instance_object(r):
                data = dict(fields(r, "vmindex,name,status,enabled,job_id,updated_at,target_id,nickname"), reason=reason_object(r["reason"]))
                data["enabled"] = bool(data["enabled"])
                return data
            order, convert = "vmindex", instance_object
            if filters.get("status"):
                condition("i.status=?", filters["status"])
            if filters.get("enabled") is not None:
                condition("i.enabled=?", int(filters["enabled"]))
        elif kind == "jobs":
            sql, order, convert = JOB_SQL, "j.created_at DESC,j.id DESC", job_object
            conditions.append("j.status " + ("IN " if filters.get("active", True) else "NOT IN ") + ACTIVE_SQL)
            for key in ("target_id", "status"):
                if filters.get(key) is not None:
                    condition("j." + key + "=?", filters[key])
        else:
            sql, order, convert = SEGMENT_SQL, "sort_time DESC,s.job_id DESC,s.segment_index DESC", segment_object
            for key in ("target_id", "job_id", "status"):
                if filters.get(key) is not None:
                    condition(("j." if key == "target_id" else "s.") + key + "=?", filters[key])
            if filters.get("has_quality_flags") is not None:
                condition("(COALESCE(s.risk_json,'[]')<>'[]')=?", filters["has_quality_flags"])
        for key, op in (("from_time", ">="), ("to_time", "<")):
            if filters.get(key) is not None:
                condition(("j.created_at" if kind == "jobs" else "COALESCE(s.started_at,s.created_at)") + op + "?", filters[key])
        sql += " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.store.read_snapshot() as db:
            return page(db, sql, values, order, convert, limit, offset, self.clock())

    def command(self, key):
        with self.store.read_snapshot() as db:
            row = db.execute("SELECT * FROM commands WHERE id=?", (key,)).fetchone()
            if not row:
                raise ControlError("command_not_found", "命令不存在", 404)
            return command_object(row)

    def segment(self, job_id, segment_index):
        with self.store.read_snapshot() as db:
            row = db.execute(SEGMENT_SQL + " WHERE s.job_id=? AND s.segment_index=?", (job_id, segment_index)).fetchone()
            if not row:
                raise ControlError("segment_not_found", "切片记录不存在", 404)
            return segment_object(row)

    def job(self, key):
        with self.store.read_snapshot() as db:
            row = db.execute(JOB_SQL + " WHERE j.id=?", (key,)).fetchone()
            if not row:
                raise ControlError("job_not_found", "任务不存在", 404)
            data = job_object(row)
            events = db.execute("SELECT * FROM events WHERE job_id=? AND kind<>'heartbeat' AND kind NOT LIKE 'segment_%' ORDER BY received_at DESC,id DESC LIMIT 50", (key,))
            data["recent_events"] = []
            for event in events:
                payload = json.loads(event["payload"])
                item = {k: payload.get(k) for k in ("event_id", "event", "timestamp", "action", "reason", "phase", "attempt")}
                item["received_at"] = event["received_at"]
                item["reason"] = reason_object(item["reason"])
                data["recent_events"].append(item)
            return data

    def log(self, key, lines=200):
        self.job(key)
        root = (self.store.path.parent / "events").resolve()
        path = (root / (key + ".log")).resolve()
        if not path.is_relative_to(root):
            raise ControlError("invalid_log_path", "日志路径越界", 400)
        base = dict(job_id=key, available=False, text="", returned_lines=0, truncated=False, reason=None)
        try:
            with path.open("rb") as stream:
                size = stream.seek(0, 2)
                start = max(0, size - 1024 * 1024)
                stream.seek(start)
                raw = stream.read(1024 * 1024)
        except FileNotFoundError:
            base["reason"] = "任务尚未启动或日志尚未生成"
            return base
        except OSError as exc:
            raise ControlError("log_read_failed", "日志读取失败", 500) from exc
        text = raw.decode("utf-8", errors="replace")
        rows = text.splitlines(keepends=True)
        return dict(job_id=key, available=True, text="".join(rows[-lines:]), returned_lines=min(lines, len(rows)), truncated=start > 0 or len(rows) > lines, reason=None)

    def overview(self, limit=50, offset=0):
        now = self.clock()
        midnight = datetime.fromtimestamp(now, timezone(timedelta(hours=8))).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        with self.store.read_snapshot() as db:
            instances = {state: 0 for state in ("idle", "busy", "booting", "offline", "reconcile")}
            jobs = {state: 0 for state in (*ACTIVE, "finished", "finished_with_warnings", "failed", "cancelled")}
            instances.update({r[0]: r[1] for r in db.execute(
                "SELECT status,count(*) FROM instances WHERE enabled=1 OR status='reconcile' GROUP BY status")})
            jobs.update({r[0]: r[1] for r in db.execute("SELECT status,count(*) FROM jobs GROUP BY status")})
            today = db.execute("SELECT count(*) FROM segments WHERE status='finished' AND ended_at>=? AND ended_at<?", (midnight, midnight + 86400)).fetchone()[0]
            # 告警先形成全部匹配项再统一分页，时间只取持久记录。
            alerts = []
            for t in db.execute(TARGET_VIEW + " WHERE enabled=1 AND (is_retrying OR needs_fix_reason IS NOT NULL)"):
                for kind, message in (("retrying", t["last_failure_reason"] or "retrying"), ("needs_fix", t["needs_fix_reason"])):
                    if (kind == "retrying" and t["is_retrying"]) or (kind == "needs_fix" and message):
                        stamp = t["fix_at"] if kind == "needs_fix" else t["failure_at"]
                        alerts.append(dict(type=kind, object_type="target", target_id=t["id"], nickname=t["nickname"], target_raw=t["raw"], job_id=t["active_job_id"], segment_index=None,
                                           reason=reason_object(message, "target_invalid" if kind == "needs_fix" else "unspecified"), attempts=t["attempts"] if kind == "retrying" else None,
                                           retry_at=t["retry_at"] if kind == "retrying" else None, timestamp=stamp or t["updated_at"]))
            for j in db.execute(JOB_SQL + " WHERE j.status='needs_reconcile'"):
                alerts.append(dict(type="needs_reconcile", object_type="job", target_id=j["target_id"], nickname=j["nickname"], target_raw=j["target_raw"], job_id=j["id"], segment_index=None,
                                   reason=reason_object(j["error_reason"] or "needs_reconcile"), attempts=None, retry_at=None, timestamp=j["ended_at"] or j["created_at"]))
            for s in db.execute(SEGMENT_SQL + " WHERE s.updated_at>=?", (now - 86400,)):
                risks = json.loads(s["risk_json"] or "[]")
                risk_time = max((r.get("timestamp") or s["updated_at"] for r in risks), default=0)
                for kind, stamp, message in (("segment_quality", risk_time, "segment_quality"), ("segment_aborted", s["ended_at"] or 0, s["reason"] or "segment_aborted")):
                    if stamp < now - 86400 or (kind == "segment_aborted" and s["status"] != "aborted"):
                        continue
                    alerts.append(dict(type=kind, object_type="segment", target_id=s["target_id"], nickname=s["nickname"], target_raw=s["target_raw"], job_id=s["job_id"], segment_index=s["segment_index"],
                                       reason=reason_object(message), attempts=None, retry_at=None, timestamp=stamp))
            alerts.sort(key=lambda a: (a["timestamp"], a["type"], a["target_id"] or "", a["job_id"] or "", a["segment_index"] or 0), reverse=True)
            return dict(instance_counts=instances, job_counts=jobs, recording_count=jobs["running"], pending_count=jobs["pending"], today_finished_segments=today,
                        statistics_timezone="Asia/Shanghai", generated_at=now,
                        alerts=dict(items=alerts[offset:offset + limit], total=len(alerts), limit=limit, offset=offset, generated_at=now))

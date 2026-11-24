"""与录制台账共用 SQLite 写事务的交付关联和切片归属。"""

import json
from uuid import uuid4

from .session_ledger import gather_evidence
from .upload_platform import BridgeError


SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
 id TEXT PRIMARY KEY, work_id TEXT NOT NULL, deployment TEXT NOT NULL, account_id TEXT NOT NULL,
 frozen TEXT NOT NULL, auto_target TEXT, auto_session TEXT, manifest_path TEXT, config_version TEXT,
 request_key TEXT NOT NULL UNIQUE, request TEXT, sent INTEGER NOT NULL DEFAULT 0,
 stopped INTEGER NOT NULL DEFAULT 0, rejected INTEGER NOT NULL DEFAULT 0,
 task_id TEXT, observed TEXT, problem TEXT, problem_code TEXT, retry_at REAL NOT NULL DEFAULT 0,
 FOREIGN KEY(auto_target, auto_session) REFERENCES recording_sessions(target_id, session_key)
);
CREATE INDEX IF NOT EXISTS deliveries_task ON deliveries(task_id);
CREATE TABLE IF NOT EXISTS delivery_segments (
 delivery_id TEXT NOT NULL REFERENCES deliveries(id), position INTEGER NOT NULL,
 job_id TEXT NOT NULL, segment_index INTEGER NOT NULL,
 PRIMARY KEY(delivery_id, position),
 FOREIGN KEY(job_id, segment_index) REFERENCES segments(job_id, segment_index)
);
CREATE INDEX IF NOT EXISTS delivery_segments_lookup ON delivery_segments(job_id, segment_index);
CREATE TABLE IF NOT EXISTS upload_drafts (
 job_id TEXT NOT NULL, segment_index INTEGER NOT NULL, deployment TEXT NOT NULL,
 account_id TEXT NOT NULL, work_id TEXT NOT NULL, config_version TEXT,
 PRIMARY KEY(job_id, segment_index, deployment, account_id)
);
"""


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def same_intent(first, second):
    def intent(frozen):
        return (
            frozen["recording_root"], frozen["asset_root"], frozen["parameters"],
            frozen["account"]["id"], frozen["account"]["remote_user_id"],
            [{k: clip[k] for k in ("job_id", "segment_index", "file", "quality_flags")} for clip in frozen["clips"]],
        )
    return intent(first) == intent(second)


def for_segments(db, parts):
    if not parts:
        return []
    values = ",".join("(?,?)" for _ in parts)
    return db.execute(f"""SELECT d.*, s.job_id, s.segment_index,
        EXISTS (SELECT 1 FROM recording_sessions r WHERE r.arrangement_id=d.id AND d.rejected=0) AS session_attached
        FROM delivery_segments s
        JOIN deliveries d ON d.id=s.delivery_id
        WHERE (s.job_id,s.segment_index) IN (VALUES {values})
        ORDER BY s.job_id,s.segment_index,d.id""", [v for part in parts for v in part])


def check_session(db, key, evidence):
    session = db.execute("SELECT * FROM recording_sessions WHERE target_id=? AND session_key=?", key).fetchone()
    if not session or session["status"] not in ("checking", "pending") or session["stopped"]:
        raise BridgeError("posting_stopped", "本场自动投稿已停止")
    clips, problem = gather_evidence(db, session)
    active = db.execute("""SELECT 1 FROM jobs WHERE target_id=? AND session_key=?
        AND status IN ('pending','starting','running','stopping','needs_reconcile')""", key).fetchone()
    if active or problem or clips != evidence:
        raise BridgeError("segment_changed", problem or "本场切片台账发生变化")
    return session


class DeliveryStore:
    def __init__(self, runtime):
        self.runtime = runtime

    @property
    def ledger(self):
        if self.runtime.store is None:
            raise BridgeError("service_unavailable", "录制服务尚未就绪", 503)
        return self.runtime.store

    def get(self, record_id):
        row = self.ledger.row("SELECT * FROM deliveries WHERE id=?", (record_id,))
        if row is None:
            raise BridgeError("arrangement_not_found", "找不到本机安排记录", 404)
        return row

    def for_segments(self, parts):
        with self.ledger.read_snapshot() as db:
            return [dict(row) for row in for_segments(db, parts)]

    def update(self, record_id, **values):
        assignment = ",".join(f"{column}=?" for column in values)
        with self.ledger.transaction() as db:
            db.execute(f"UPDATE deliveries SET {assignment} WHERE id=?", (*values.values(), record_id))

    @staticmethod
    def insert(db, deployment, work_id, frozen, *, session=(None, None), path=None, revision=None, request=None):
        record_id = str(uuid4())
        db.execute("""INSERT INTO deliveries
            (id,work_id,deployment,account_id,frozen,auto_target,auto_session,manifest_path,config_version,request_key,request)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                   (record_id, work_id, deployment, frozen["account"]["id"], encode(frozen), *session,
                    path, revision, str(uuid4()), encode(request) if request else None))
        db.executemany("INSERT INTO delivery_segments VALUES (?,?,?,?)", [
            (record_id, pos, clip["job_id"], clip["segment_index"]) for pos, clip in enumerate(frozen["clips"], 1)
        ])
        return record_id

    def freeze_session(self, key, deployment, frozen):
        parts = [(clip["job_id"], clip["segment_index"]) for clip in frozen["clips"]]
        with self.ledger.transaction() as db:
            session = check_session(db, key, frozen["evidence"])
            if session["arrangement_id"]:
                return session["arrangement_id"]
            # 每次查询只绑定少量参数，长场次的分片数不受 SQLite 参数上限影响。
            overlaps = [row for part in parts for row in for_segments(db, [part])]
            if overlaps:
                original = overlaps[0]
                if (len(parts) != 1 or len(overlaps) != 1 or original["auto_target"]
                    or original["rejected"] or original["deployment"] != deployment
                    or not same_intent(json.loads(original["frozen"]), frozen)):
                    conflict = "、".join(dict.fromkeys(
                        f"任务 {row['job_id']} 第 {row['segment_index']} 片" for row in overlaps
                    ))
                    raise BridgeError("arrangement_conflict", f"{conflict} 已有安排，请查看原单")
                record_id = original["id"]
                status = "delivered" if original["task_id"] else "pending"
            else:
                record_id = self.insert(db, deployment, str(uuid4()), frozen, session=key)
                status = "checking"
            db.execute("""UPDATE recording_sessions SET arrangement_id=?, status=?, problem=NULL
                WHERE target_id=? AND session_key=?""", (record_id, status, *key))
            return record_id

    def save_session_manifest(self, row, path, revision, request):
        with self.ledger.transaction() as db:
            current = db.execute("SELECT stopped FROM deliveries WHERE id=?", (row["id"],)).fetchone()
            if not current["stopped"]:
                db.execute("""UPDATE deliveries SET manifest_path=?, config_version=?, request=?
                    WHERE id=? AND request IS NULL""", (path, revision, encode(request), row["id"]))
                db.execute("""UPDATE recording_sessions SET status='pending'
                    WHERE arrangement_id=? AND status='checking' AND stopped=0""", (row["id"],))

    def confirm(self, preview, current_clip, check_version):
        frozen = preview["frozen"]
        job, index = preview["part"]
        with self.ledger.transaction() as db:
            if current_clip(job, index) != frozen["clips"][0]:
                raise BridgeError("segment_changed", "录像或质量提示已变化，请重新预览")
            for old in for_segments(db, [(job, index)]):
                if old["auto_target"] or old["session_attached"]:
                    return old["id"], False
                if old["deployment"] == preview["deployment"] and old["account_id"] == frozen["account"]["id"]:
                    if old["rejected"] and preview["revise"] == old["id"]:
                        db.execute("DELETE FROM delivery_segments WHERE delivery_id=?", (old["id"],))
                        continue
                    return old["id"], False
            check_version(preview["path"], preview["version"])
            record_id = self.insert(db, preview["deployment"], preview["work_id"], frozen,
                                    path=preview["path"], revision=preview["version"], request=preview["request"])
            db.execute("""DELETE FROM upload_drafts WHERE job_id=? AND segment_index=?
                AND deployment=? AND account_id=?""", (job, index, preview["deployment"], frozen["account"]["id"]))
            return record_id, True

    def mark_sent(self, row, current_clip):
        frozen = json.loads(row["frozen"])
        with self.ledger.transaction() as db:
            current = db.execute("SELECT * FROM deliveries WHERE id=?", (row["id"],)).fetchone()
            if current["stopped"] or current["rejected"] or current["task_id"]:
                return False
            if row["auto_target"]:
                check_session(db, (row["auto_target"], row["auto_session"]), frozen["evidence"])
            else:
                clip = frozen["clips"][0]
                if current_clip(clip["job_id"], clip["segment_index"]) != clip:
                    raise BridgeError("segment_changed", "录像或质量提示已变化，请重新预览")
            db.execute("UPDATE deliveries SET sent=1 WHERE id=?", (row["id"],))
            return True

"""手动与整场安排，以及每个步骤的派发权。"""

from contextlib import closing, contextmanager, nullcontext
import json
import sqlite3
from threading import Lock
import time
from uuid import uuid4

from .upload_core_client import BridgeError


SCHEMA = """
CREATE TABLE IF NOT EXISTS arrangements (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL, segment_index INTEGER NOT NULL,
 deployment TEXT NOT NULL, account_id TEXT NOT NULL, revision INTEGER NOT NULL,
 frozen TEXT NOT NULL, work_id TEXT NOT NULL, register_key TEXT NOT NULL, submit_key TEXT NOT NULL,
 stage TEXT NOT NULL DEFAULT 'register', attempts INTEGER NOT NULL DEFAULT 0,
 generation INTEGER NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
 rejected INTEGER NOT NULL DEFAULT 0, problem TEXT, problem_detail TEXT, task_id TEXT,
 observed TEXT, observed_at REAL, next_check REAL NOT NULL DEFAULT 0,
 auto_target TEXT, auto_session TEXT, follow_session INTEGER NOT NULL DEFAULT 0,
 UNIQUE(auto_target, auto_session)
);
CREATE TABLE IF NOT EXISTS arrangement_segments (
 arrangement_id TEXT NOT NULL REFERENCES arrangements(id), position INTEGER NOT NULL,
 job_id TEXT NOT NULL, segment_index INTEGER NOT NULL,
 PRIMARY KEY(arrangement_id, position), UNIQUE(arrangement_id, job_id, segment_index)
);
CREATE INDEX IF NOT EXISTS arrangement_segments_lookup ON arrangement_segments(job_id, segment_index);
CREATE TABLE IF NOT EXISTS confirmations (
 key TEXT PRIMARY KEY, arrangement_id TEXT NOT NULL REFERENCES arrangements(id),
 revision INTEGER NOT NULL, intent TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
 arrangement_id TEXT NOT NULL REFERENCES arrangements(id), revision INTEGER NOT NULL,
 frozen TEXT NOT NULL, register_key TEXT NOT NULL, submit_key TEXT NOT NULL,
 stage TEXT NOT NULL, attempts INTEGER NOT NULL, rejected INTEGER NOT NULL, problem TEXT,
 PRIMARY KEY(arrangement_id, revision)
);
"""


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def confirmed_input(frozen):
    return {k: frozen[k] for k in ("parameters", "file", "quality_flags")} | {
        "mid": frozen["account"]["remote_user_id"],
    }


def stale():
    return BridgeError("stale_dialog", "安排内容已修订，请重新打开弹窗核对")


def revisable(row, now):
    return (
        not row["auto_target"] and row["stage"] != "task" and row["lease_until"] <= now
        and (row["rejected"] or row["attempts"] == 0)
    )


class ArrangementStore:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = path, clock
        self._lock = Lock()
        self._initialized = False

    @contextmanager
    def connect(self, write=False, guard=nullcontext):
        with self._lock:
            if not self._initialized:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(self.path, timeout=2)) as db:
                    version = db.execute("PRAGMA user_version").fetchone()[0]
                    has_tables = db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone()
                    if has_tables and version != 1:
                        raise BridgeError("upload_db_version", "投递库格式已变更，请使用新的独立数据目录", 503)
                    db.execute("PRAGMA journal_mode=WAL")
                    db.executescript(SCHEMA)
                    db.execute("PRAGMA user_version=1")
                self._initialized = True
        with closing(sqlite3.connect(self.path, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            with guard(), db:
                yield db

    def get(self, record_id):
        with self.connect() as db:
            return self._get(db, record_id)

    @staticmethod
    def _get(db, record_id):
        row = db.execute("SELECT * FROM arrangements WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise BridgeError("arrangement_not_found", "找不到本机安排记录", 404)
        return dict(row)

    def for_segments(self, segments):
        if not segments:
            return []
        values = ",".join(["(?,?)"] * len(segments))
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                f"""SELECT a.*,s.job_id AS member_job_id,s.segment_index AS member_index
                FROM arrangement_segments s JOIN arrangements a ON a.id=s.arrangement_id
                WHERE (s.job_id,s.segment_index) IN (VALUES {values})
                ORDER BY s.job_id,s.segment_index,a.id""",
                [value for pair in segments for value in pair],
            )]

    def confirm(self, key, job, index, deployment, frozen, expected, revise, freeze_requests, *, guard=nullcontext):
        account = frozen["account"]["id"]
        intent = encode({"job": job, "index": index, "deployment": deployment,
                         "account": account, "input": confirmed_input(frozen), "revise": revise})
        with self.connect(write=True, guard=guard) as db:
            old = db.execute(
                """SELECT a.* FROM arrangements a JOIN arrangement_segments s ON s.arrangement_id=a.id
                   WHERE s.job_id=? AND s.segment_index=? AND a.deployment=? AND a.account_id=?""",
                (job, index, deployment, account),
            ).fetchone()
            auto = db.execute("""SELECT a.* FROM arrangements a JOIN arrangement_segments s ON s.arrangement_id=a.id
                WHERE s.job_id=? AND s.segment_index=? AND a.auto_target IS NOT NULL""", (job, index)).fetchone()
            if auto and (old is None or old["id"] != auto["id"]):
                raise BridgeError("arrangement_conflict", "该切片已纳入整场安排，请查看原单")
            prior = db.execute("SELECT * FROM confirmations WHERE key=?", (key,)).fetchone()
            if prior:
                if prior["intent"] != intent:
                    raise BridgeError("confirmation_conflict", "该操作编号已绑定另一份确认内容")
                if old is None or prior["revision"] != old["revision"]:
                    raise stale()
                if old["revision"] not in (expected, expected + 1):
                    raise stale()
                return dict(old), False

            if old and old["auto_target"]:
                saved = json.loads(old["frozen"])
                if (revise or len(saved["clips"]) != 1 or saved["parameters"] != frozen["parameters"]
                    or saved["account"]["remote_user_id"] != frozen["account"]["remote_user_id"]
                    or saved["file"] != frozen["file"] or saved["quality_flags"] != frozen["quality_flags"]):
                    raise BridgeError("arrangement_conflict", "该切片已纳入整场安排，请查看原单")
                record_id, revision, created = old["id"], old["revision"], False
            elif revise:
                if old is None or old["id"] != revise or old["revision"] != expected:
                    raise stale()
                if not revisable(old, self.clock()):
                    raise BridgeError("revision_forbidden", "原请求结果仍待确认，暂不能修订")
                db.execute(
                    """INSERT INTO revisions SELECT id,revision,frozen,register_key,submit_key,
                       stage,attempts,rejected,problem FROM arrangements WHERE id=?""", (revise,),
                )
                revised = freeze_requests(frozen, old["work_id"])
                db.execute(
                    """UPDATE arrangements SET revision=revision+1, frozen=?, register_key=?, submit_key=?,
                       stage='register', attempts=0, rejected=0, problem=NULL, problem_detail=NULL, lease_until=0, next_check=0
                       WHERE id=?""",
                    (encode(revised), str(uuid4()), str(uuid4()), revise),
                )
                record_id, revision, created = revise, expected + 1, True
            elif old is not None:
                if old["revision"] != expected and not (expected == 0 and old["revision"] == 1):
                    raise stale()
                if confirmed_input(frozen) != confirmed_input(json.loads(old["frozen"])):
                    raise BridgeError("arrangement_conflict", "该账号已有安排，请查看原单")
                record_id, revision, created = old["id"], old["revision"], False
            else:
                if expected:
                    raise stale()
                record_id, revision, created = str(uuid4()), 1, True
                work_id = str(uuid4())
                saved = freeze_requests(frozen, work_id)
                db.execute(
                    """INSERT INTO arrangements
                       (id,job_id,segment_index,deployment,account_id,revision,frozen,work_id,register_key,submit_key)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (record_id, job, index, deployment, account, revision, encode(saved),
                     work_id, str(uuid4()), str(uuid4())),
                )
                db.execute("INSERT INTO arrangement_segments VALUES (?,1,?,?)", (record_id, job, index))
            db.execute("INSERT INTO confirmations VALUES (?,?,?,?)", (key, record_id, revision, intent))
            return self._get(db, record_id), created

    def arrange_session(self, session_key, deployment, frozen, freeze_requests, *, guard):
        clips, account = frozen["clips"], frozen["account"]
        with self.connect(write=True, guard=guard) as db:
            old = db.execute("SELECT * FROM arrangements WHERE auto_target=? AND auto_session=?",
                             session_key).fetchone()
            if old:
                return dict(old)
            # 与手动确认共用写事务。任何重叠安排都在这里看到。
            pairs = [(clip["job_id"], clip["segment_index"]) for clip in clips]
            overlaps = {}
            for job, index in pairs:
                for row in db.execute("""SELECT a.* FROM arrangements a
                    JOIN arrangement_segments s ON s.arrangement_id=a.id
                    WHERE s.job_id=? AND s.segment_index=?""", (job, index)):
                    overlaps[row["id"]] = row
            if overlaps:
                if len(pairs) == len(overlaps) == 1:
                    row = next(iter(overlaps.values()))
                    if (not row["auto_target"] and row["deployment"] == deployment
                        and row["account_id"] == account["id"]
                        and confirmed_input(json.loads(row["frozen"])) == confirmed_input(frozen)):
                        db.execute("UPDATE arrangements SET follow_session=1 WHERE id=?", (row["id"],))
                        return dict(row)
                raise BridgeError("arrangement_conflict", "切片已有安排，与整场清单冲突，请查看切片页原单")
            record_id, work_id = str(uuid4()), str(uuid4())
            db.execute("""INSERT INTO arrangements
                (id,job_id,segment_index,deployment,account_id,revision,frozen,work_id,
                 register_key,submit_key,auto_target,auto_session) VALUES (?,?,?,?,?,1,?,?,?,?,?,?)""",
                       (record_id, *pairs[0], deployment, account["id"], encode(freeze_requests(frozen, work_id)),
                        work_id, str(uuid4()), str(uuid4()), *session_key))
            db.executemany("INSERT INTO arrangement_segments VALUES (?,?,?,?)",
                           [(record_id, pos, *pair) for pos, pair in enumerate(pairs, 1)])
            return self._get(db, record_id)

    def stop_session(self, record_id):
        with self.connect(write=True) as db:
            stopped = db.execute("""UPDATE arrangements SET stage='stopped',lease_until=0
                WHERE id=? AND auto_target IS NOT NULL AND (stage='register' OR (stage='submit' AND attempts=0))""",
                                 (record_id,)).rowcount
            if stopped:
                db.execute("DELETE FROM arrangement_segments WHERE arrangement_id=?", (record_id,))

    def find_session(self, key):
        with self.connect() as db:
            row = db.execute("SELECT id FROM arrangements WHERE auto_target=? AND auto_session=?", key).fetchone()
            return row["id"] if row else None

    def due_tasks(self):
        if not self.path.is_file():
            return []
        now = self.clock()
        with self.connect() as db:
            return [row[0] for row in db.execute("""SELECT id FROM arrangements
                WHERE (auto_target IS NOT NULL OR follow_session=1)
                  AND stage='task' AND lease_until<=? AND next_check<=?
                ORDER BY next_check LIMIT 4""", (now, now))]

    def claim(self, record_id, *, dispatch, revision=None, force=False):
        with self.connect(write=True) as db:
            row, now = self._get(db, record_id), self.clock()
            if revision is not None and row["revision"] != revision:
                raise stale()
            if row["stage"] == "stopped" or row["lease_until"] > now or row["rejected"]:
                return None
            if not dispatch or row["stage"] == "task":
                needs_lookup = row["stage"] == "task" or row["stage"] == "submit" and row["attempts"]
                if not needs_lookup or not force and row["next_check"] > now:
                    return None
            db.execute(
                "UPDATE arrangements SET generation=generation+1, lease_until=? WHERE id=?",
                (now + 18, record_id),
            )
            return self._get(db, record_id)

    def _fence(self, row, *, live=True):
        sql = "id=? AND stage=? AND generation=? AND revision=?"
        args = (row["id"], row["stage"], row["generation"], row["revision"])
        return (sql + " AND lease_until>?", (*args, self.clock())) if live else (sql, args)

    def mark_sent(self, row, *, guard=nullcontext):
        with self.connect(write=True, guard=guard) as db:
            where, args = self._fence(row)
            return db.execute(
                f"UPDATE arrangements SET attempts=attempts+1 WHERE {where}", args
            ).rowcount == 1

    def finish(self, row, **changes):
        assignment = ",".join(f"{name}=?" for name in changes)
        with self.connect(write=True) as db:
            where, args = self._fence(row)
            return db.execute(
                f"UPDATE arrangements SET {assignment} WHERE {where}", (*changes.values(), *args)
            ).rowcount == 1

    def release(self, row):
        where, args = self._fence(row, live=False)
        with self.connect(write=True) as db:
            db.execute(f"UPDATE arrangements SET lease_until=0 WHERE {where}", args)

"""独立的扫码请求日志。只保存动作、标识和安全状态。"""

import os
import sqlite3
from contextlib import closing, contextmanager

import psutil

from .upload_core_client import BridgeError
from .upload_core_runtime import live


class LoginStore:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=4)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if (
                version == 0
                and not db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchone()
            ):
                db.execute("""CREATE TABLE requests(key TEXT PRIMARY KEY, deployment TEXT, action TEXT,
                    target TEXT, session TEXT, status TEXT, error TEXT, pid INTEGER, created REAL)""")
                db.execute("PRAGMA user_version=1")
            elif version != 1:
                raise BridgeError("journal_schema", "请求库版本不兼容")
            db.execute("""CREATE TABLE IF NOT EXISTS observed(deployment TEXT, session TEXT, terminal INTEGER,
                PRIMARY KEY(deployment,session))""")
            yield db
            db.commit()

    def entries(self, deployment):
        with self.db() as db:
            return [
                dict(r)
                for r in db.execute(
                    """SELECT key,action,target,session,status,error FROM requests
                WHERE deployment=? ORDER BY rowid DESC""",
                    (deployment,),
                )
            ]

    def pending(self, deployment):
        with self.db() as db:
            if db.execute(
                "SELECT 1 FROM observed WHERE deployment=? AND terminal=0 LIMIT 1",
                (deployment,),
            ).fetchone():
                return True
        return any(
            r["status"] in ("unknown", "linked", "sending")
            for r in self.entries(deployment)
        )

    def observe(self, deployment, views):
        with self.db() as db:
            for v in views:
                db.execute(
                    """INSERT INTO observed VALUES(?,?,?) ON CONFLICT(deployment,session)
                    DO UPDATE SET terminal=max(terminal,excluded.terminal)""",
                    (deployment, v["session_id"], int(v["outcome"] is not None)),
                )
            return [
                r[0]
                for r in db.execute(
                    "SELECT session FROM observed WHERE deployment=? AND terminal=0",
                    (deployment,),
                )
            ]

    def begin(self, deployment, key, action, target):
        with self.db() as db:
            row = db.execute("SELECT * FROM requests WHERE key=?", (key,)).fetchone()
            if row and (row["deployment"], row["action"], row["target"]) != (
                deployment,
                action,
                target,
            ):
                raise BridgeError("key_conflict", "请求编号已用于不同的操作")
            if not row:
                row = db.execute(
                    """SELECT * FROM requests WHERE deployment=? AND action=? AND target IS ?
                    AND status IN ('linked','unknown','sending') LIMIT 1""",
                    (deployment, action, target),
                ).fetchone()
            if row and (
                row["key"] != key
                or row["status"] not in ("sending", "unknown")
                or live(row["pid"], row["created"])
            ):
                return dict(row), False
            db.execute(
                """INSERT INTO requests(key,deployment,action,target,status,pid,created)
                VALUES(?,?,?,?,'sending',?,?) ON CONFLICT(key) DO UPDATE SET status='sending',pid=excluded.pid,created=excluded.created""",
                (
                    key,
                    deployment,
                    action,
                    target,
                    os.getpid(),
                    psutil.Process().create_time(),
                ),
            )
            return {"key": key}, bool(row)

    def finish(self, key, status, session=None, error=None):
        with self.db() as db:
            db.execute(
                "UPDATE requests SET status=?,session=coalesce(?,session),error=?,pid=NULL,created=NULL WHERE key=? AND status='sending'",
                (status, session, error, key),
            )

    def terminal(self, deployment, sid):
        with self.db() as db:
            db.execute(
                "UPDATE requests SET status='done' WHERE deployment=? AND session=? AND status='linked'",
                (deployment, sid),
            )

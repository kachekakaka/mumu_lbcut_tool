"""调度端负责场次编号和录像发布。采集台账只认本任务的独占目录。"""
from datetime import datetime, timedelta, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import stat

from . import session_ledger


log = logging.getLogger(__name__)
BEIJING = timezone(timedelta(hours=8))
RUN = re.compile(r"run_\d+_[a-f0-9]{32}")
CAPTURE = re.compile(r"capture_[a-f0-9]{32}")
SCHEMA = """
CREATE TABLE IF NOT EXISTS recording_anchors (
 recording_root TEXT NOT NULL, anchor_uid TEXT NOT NULL, directory TEXT NOT NULL,
 PRIMARY KEY(recording_root,anchor_uid), UNIQUE(recording_root,directory COLLATE NOCASE)
);
CREATE INDEX IF NOT EXISTS segment_archive_queue ON segments(archive_state,job_id,segment_index);
"""


def component(nickname):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', (nickname or '').strip())[:40].rstrip(' .')
    if not name or name.casefold() == "_录制中":
        return "主播"
    if re.fullmatch(r"CON|PRN|AUX|NUL|(?:COM|LPT)[1-9¹²³]|CONIN\$|CONOUT\$",
                    name.split(".")[0].rstrip(), re.I):
        return "_" + name
    return name


def stable(path):
    return path.is_absolute() and not str(path).startswith("\\\\") and path.resolve() == path


def reserve_directory(parent, name, reserved):
    names = {child.name.casefold() for child in parent.iterdir()} | {n.casefold() for n in reserved}
    suffix = 1
    while True:
        candidate = name if suffix == 1 else f"{name}-{suffix}"
        path = parent / candidate
        if candidate.casefold() not in names:
            try:
                path.mkdir()
                return path
            except FileExistsError:
                pass
        suffix += 1


def number(db, job_id, index):
    job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job or not job["recording_root"]:
        return None
    key = (job["target_id"], job["session_key"])
    session = db.execute("SELECT * FROM recording_sessions WHERE target_id=? AND session_key=?", key).fetchone()
    if not session:
        return None
    root = Path(session["recording_root"] or job["recording_root"])
    if str(root) != job["recording_root"] or not stable(root) or not root.is_dir():
        raise OSError(f"原录像根不可用：{root}")
    if session["recording_directory"]:
        directory = Path(session["recording_directory"])
    else:
        anchor_key = (str(root), session["anchor_uid"])
        anchor = db.execute("SELECT directory FROM recording_anchors WHERE recording_root=? AND anchor_uid=?",
                            anchor_key).fetchone()
        if anchor:
            anchor_dir = root / anchor["directory"]
            if not stable(anchor_dir):
                raise OSError(f"主播目录已指向其他位置：{anchor_dir}")
            anchor_dir.mkdir(exist_ok=True)
        else:
            prior = db.execute("SELECT directory FROM recording_anchors WHERE anchor_uid=? LIMIT 1",
                               (session["anchor_uid"],)).fetchone()
            reserved = [row[0] for row in db.execute(
                "SELECT directory FROM recording_anchors WHERE recording_root=?", (str(root),))]
            anchor_dir = reserve_directory(root, prior[0] if prior else component(session["nickname"]),
                                           ["_录制中", *reserved])
            db.execute("INSERT INTO recording_anchors VALUES(?,?,?)", (*anchor_key, anchor_dir.name))
        name = datetime.fromtimestamp(session["started_at"], BEIJING).strftime("%Y-%m-%d_%H%M")
        reserved = [Path(row[0]).name for row in db.execute(
            "SELECT recording_directory FROM recording_sessions WHERE recording_root=? AND recording_directory IS NOT NULL",
            (str(root),)) if Path(row[0]).parent == anchor_dir]
        directory = reserve_directory(anchor_dir, name, reserved)
        db.execute("""UPDATE recording_sessions SET recording_root=?,recording_directory=?
                   WHERE target_id=? AND session_key=?""", (str(root), str(directory), *key))
    row = db.execute("SELECT session_index FROM segments WHERE job_id=? AND segment_index=?", (job_id, index)).fetchone()
    current = row["session_index"]
    if current is None:
        current = db.execute("""SELECT COALESCE(MAX(s.session_index),0)+1 FROM segments s JOIN jobs j ON j.id=s.job_id
            WHERE j.target_id=? AND j.session_key=?""", key).fetchone()[0]
        db.execute("UPDATE segments SET session_index=? WHERE job_id=? AND segment_index=?", (current, job_id, index))
    return directory, current


def task_directory(job):
    return Path(job["recording_root"]) / "_录制中" / job["id"]


def location(store, job, index):
    return store.row("""SELECT s.archive_state,s.session_index,s.archive_path,s.archive_identity,s.raw_path,s.file_path,s.status,
        j.id,j.recording_root,r.recording_directory,r.recording_root AS session_root
        FROM segments s JOIN jobs j ON j.id=s.job_id LEFT JOIN recording_sessions r
            ON r.target_id=j.target_id AND r.session_key=j.session_key
        WHERE s.job_id=? AND s.segment_index=?""", (job, index))


def capture_record(job, directory):
    if (not stable(directory) or directory.parent.parent != task_directory(job)
            or not RUN.fullmatch(directory.parent.name) or not CAPTURE.fullmatch(directory.name)):
        raise ValueError("不属于该任务的采集目录")
    marker = directory / "capture.json"
    if not stable(marker):
        raise ValueError("采集台账路径变化")
    value = json.loads(marker.read_text(encoding="utf-8"))
    if (value["job_id"] != job["id"] or value["run_id"] != directory.parent.name
            or (job["worker_run_id"] and value["run_id"] != job["worker_run_id"])
            or value["directory"] != str(directory) or value["pattern"] != "part_%06d.mp4"
            or value["room_id"] != job["room_id"] or value["anchor_uid"] != job["anchor_uid"]
            or type(value["generation"]) is not int or value["generation"] < 1
            or type(value["start_index"]) is not int or value["start_index"] < 1
            or not math.isfinite(value["registered_at"])):
        raise ValueError("采集台账归属不符")
    return value


def admitted_job(db, job_id):
    return db.execute("""SELECT j.*,a.anchor_uid FROM jobs j
        LEFT JOIN recording_admissions a ON a.job_id=j.id WHERE j.id=?""", (job_id,)).fetchone()


def identity(path):
    if not stable(path):
        raise ValueError(f"文件路径变化：{path}")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or not info.st_ino:
        raise ValueError(f"无可核对的录像文件：{path}")
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def hold(db, job_id, index, reason, state="review"):
    db.execute("""UPDATE segments SET archive_state=?,file_path=NULL,reason=?
        WHERE job_id=? AND segment_index=?""", (state, reason, job_id, index))


def intent(db, job_id, index, directory, number_, source, interrupted):
    destination = directory / (f"{number_:03d}" + ("-中断" if interrupted else "") + ".mp4")
    info = identity(source)
    db.execute("""UPDATE segments SET archive_path=?,archive_identity=?,archive_state='pending',file_path=NULL
        WHERE job_id=? AND segment_index=?""", (str(destination), json.dumps(info), job_id, index))


def terminal(db, job_id, index, event, stamp, now):
    job = admitted_job(db, job_id)
    if not job["recording_root"]:
        return
    source = Path(event.get("file_path") or "")
    interrupted = event["event"] == "segment_aborted"
    try:
        capture = capture_record(job, source.parent)
        if (event.get("run_id") != capture["run_id"] or source.name != f"part_{index:06d}.mp4"
                or index < capture["start_index"]):
            raise ValueError("切片与采集台账不符")
        clip = db.execute("SELECT * FROM segments WHERE job_id=? AND segment_index=?", (job_id, index)).fetchone()
        if clip["raw_path"] and clip["raw_path"] != str(source):
            raise ValueError("同一切片的开始与终态路径不一致")
        reason = event.get("reason") or ""
        if interrupted and (reason.startswith(("validation_", "runtime_identity")) or reason == "fragment_terminal_missing"):
            hold(db, job_id, index, reason)
            return
        # 正常终态证明媒体已经通过封口检查，身份冲突保留在原位置。
        if event.get("room_identity_status") != "verified" or any(
            risk.get("flag") in ("identity_mismatch", "identity_unconfirmed")
            for risk in json.loads(clip["risk_json"] or "[]")
        ):
            raise ValueError("片段身份待核对")
        if not clip["started"]:
            first = first_media(source.parent)
            session_ledger.first_segment(db, job_id, first, now, True)
        allocated = number(db, job_id, index)
        if allocated is None:
            raise ValueError("缺少已核实的场次")
        intent(db, job_id, index, *allocated, source, interrupted)
    except FileNotFoundError:
        hold(db, job_id, index, f"原片或采集台账缺失：{source}")
    except (ValueError, KeyError, TypeError) as exc:
        hold(db, job_id, index, f"{exc}：{source}")


def first_media(directory):
    marker = directory / "capture.verified"
    if not stable(marker):
        raise ValueError("采集身份记录路径变化")
    stamp = float(marker.read_text(encoding="ascii"))
    if not math.isfinite(stamp) or stamp <= 0:
        raise ValueError("开录时刻无效")
    return stamp


def recover_unfinished(store, job_id, now):
    """须先完成日志重放并确认 Worker 和采集进程都已退出。"""
    with store.transaction() as db:
        job = admitted_job(db, job_id)
        if not job or not job["recording_root"]:
            return
        task = task_directory(job)
        captures = []
        directories = task.glob("run_*/capture_*") if stable(task) and task.is_dir() else ()
        for directory in directories:
            try:
                record = capture_record(job, directory)
                stamp = first_media(directory)
                if not record["registered_at"] <= stamp <= now + 2:
                    raise ValueError("采集开录时间超出登记范围")
                captures.append((record["generation"], directory, record, stamp))
            except (OSError, ValueError, KeyError, TypeError):
                log.info("保留未能核实的采集目录：%s", directory)
        if len({record["run_id"] for _, _, record, _ in captures}) > 1:
            log.warning("任务 %s 有多份 Worker 运行台账，保留原片等待核对", job_id)
            captures.clear()
        for _, directory, record, stamp in sorted(captures):
            db.execute("UPDATE jobs SET worker_run_id=COALESCE(worker_run_id,?) WHERE id=?",
                       (record["run_id"], job_id))
            index = record["start_index"]
            while True:
                source = directory / f"part_{index:06d}.mp4"
                clip = db.execute("SELECT * FROM segments WHERE job_id=? AND segment_index=?", (job_id, index)).fetchone()
                if clip and (clip["terminal_path"] == str(source) or clip["raw_path"] == str(source)):
                    if clip["status"] != "started":
                        index += 1
                        continue
                if not source.exists():
                    break
                try:
                    info = identity(source)
                    if info[3] / 1_000_000_000 + 1 < record["registered_at"] or clip and (
                        clip["status"] != "started" or clip["raw_path"] != str(source)
                        or any(risk.get("flag") in ("identity_unconfirmed", "identity_mismatch")
                               for risk in json.loads(clip["risk_json"] or "[]"))
                    ):
                        break
                    session_ledger.first_segment(db, job_id, stamp, now, True)
                    db.execute("""INSERT OR IGNORE INTO segments(job_id,segment_index,raw_path,created_at,updated_at)
                        VALUES(?,?,?,?,?)""", (job_id, index, str(source), stamp, now))
                    allocated = number(db, job_id, index)
                    if allocated is None:
                        break
                    db.execute("""UPDATE segments SET status='aborted',terminal_path=?,ended_at=?,reason=?
                        WHERE job_id=? AND segment_index=?""",
                               (str(source), now, "缺少终态，待核对", job_id, index))
                    intent(db, job_id, index, *allocated, source, True)
                except (FileNotFoundError, ValueError):
                    break
                index += 1
        db.execute("""UPDATE segments SET reason='缺少终态，原片缺失或归属待核对',archive_state='review'
                   WHERE job_id=? AND status='started'""", (job_id,))


def publish(store):
    """用已提交的意图发布目标。发布记账后才释放原始名字。"""
    rows = store.rows("""SELECT s.*,j.target_id,j.session_key,j.recording_root FROM segments s
        JOIN jobs j ON j.id=s.job_id WHERE s.archive_state IN ('pending','published')
        AND (s.status<>'aborted' OR j.status IN ('finished','finished_with_warnings','failed','cancelled'))
        ORDER BY s.archive_state,s.updated_at""")
    for row in rows:
        key = (row["job_id"], row["segment_index"])
        root, source, target = map(Path, (row["recording_root"], row["terminal_path"], row["archive_path"]))
        if not root.is_dir():
            continue
        expected = json.loads(row["archive_identity"])
        try:
            if not all(stable(p) and p.is_relative_to(root) for p in (source, target)):
                raise ValueError("归档路径已变化")
            pending = row["archive_state"] == "pending"
            if pending:
                if source.exists() and identity(source) != expected:
                    raise ValueError("原片身份已变化")
                try:
                    os.link(source, target)
                except FileExistsError:
                    pass
                except FileNotFoundError:
                    if not target.exists():
                        raise
                if identity(target) != expected:
                    raise ValueError("归档目的地已存在不同文件")
                with store.transaction() as db:
                    db.execute("""UPDATE segments SET archive_state='published',file_path=?
                        WHERE job_id=? AND segment_index=? AND archive_state='pending'""", (str(target), *key))
            if source.exists():
                if identity(source) != expected or identity(target) != expected or not source.samefile(target):
                    raise ValueError("暂存文件与正式文件不一致")
                source.unlink()
            elif identity(target) != expected:
                raise ValueError("正式文件身份已变化")
            with store.transaction() as db:
                db.execute("""UPDATE segments SET archive_state='archived'
                    WHERE job_id=? AND segment_index=? AND archive_state='published'""", key)
        except (ValueError, FileNotFoundError) as exc:
            with store.transaction() as db:
                hold(db, *key, f"{exc}；原片 {source}；目标 {target}")
        except OSError as exc:
            log.warning("暂时无法完成归档 %s → %s：%s", source, target, exc)
    with store.transaction() as db:
        for session in list(db.execute("""SELECT target_id,session_key FROM recording_sessions
                                         WHERE status='waiting_archive'""")):
            session_ledger.close_session(db, *session)

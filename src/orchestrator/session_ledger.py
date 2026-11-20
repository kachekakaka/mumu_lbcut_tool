"""开录资料、正常收尾和自动投稿意图。调用方负责调度库事务。"""

import json


SCHEMA = """
CREATE TABLE IF NOT EXISTS posting_settings (
 target_id TEXT PRIMARY KEY REFERENCES targets(id), enabled INTEGER NOT NULL, binding TEXT
);
CREATE TABLE IF NOT EXISTS recording_admissions (
 job_id TEXT PRIMARY KEY REFERENCES jobs(id), room_id TEXT NOT NULL, anchor_uid TEXT NOT NULL,
 nickname TEXT, web_rid TEXT NOT NULL, observed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS recording_sessions (
 target_id TEXT NOT NULL REFERENCES targets(id), session_key TEXT NOT NULL,
 first_job_id TEXT NOT NULL REFERENCES jobs(id), started_at REAL NOT NULL,
 room_id TEXT NOT NULL, anchor_uid TEXT NOT NULL, nickname TEXT, web_rid TEXT NOT NULL,
 title TEXT, title_observed_at REAL, title_step TEXT NOT NULL, title_deadline REAL,
 close_reason TEXT, status TEXT NOT NULL DEFAULT 'recording', stopped INTEGER NOT NULL DEFAULT 0,
 binding TEXT, evidence TEXT,
 problem TEXT, retry_at REAL NOT NULL DEFAULT 0,
 arrangement_id TEXT REFERENCES deliveries(id),
 PRIMARY KEY(target_id, session_key)
);
CREATE INDEX IF NOT EXISTS recording_sessions_status ON recording_sessions(status, started_at);
CREATE INDEX IF NOT EXISTS jobs_session ON jobs(target_id, session_key);
"""


def first_segment(db, job_id, stamp, now, replay):
    db.execute("""INSERT OR IGNORE INTO recording_sessions
        (target_id,session_key,first_job_id,started_at,room_id,anchor_uid,nickname,web_rid,
         title_step,title_deadline,close_reason)
        SELECT j.target_id,j.session_key,j.id,?,a.room_id,a.anchor_uid,a.nickname,a.web_rid,?,?,
            CASE WHEN j.stop_reason='manual_stop' THEN 'manual_stop'
                 WHEN t.session_key=j.session_key AND t.trusted_status='ended' THEN 'anchor_offline' END
        FROM jobs j JOIN recording_admissions a ON a.job_id=j.id JOIN targets t ON t.id=j.target_id
        WHERE j.id=?""", (stamp, 'opening_done' if replay else 'opening_wait', now + 30, job_id))


def gather_evidence(db, session):
    """按任务首片及台账次序排列，验证完整性和 Worker 记录的身份。"""
    jobs = list(db.execute("""SELECT j.*,a.anchor_uid,a.room_id AS admitted_room
        FROM jobs j LEFT JOIN recording_admissions a ON a.job_id=j.id
        WHERE j.target_id=? AND j.session_key=?
        ORDER BY (SELECT MIN(s.started_at) FROM segments s WHERE s.job_id=j.id) NULLS LAST, j.rowid""",
                           (session['target_id'], session['session_key'])))
    issues, clips, paths = [], [], set()
    previous = None
    for job in jobs:
        job_id = job['id']
        segments = list(db.execute("SELECT * FROM segments WHERE job_id=? ORDER BY segment_index", (job_id,)))
        if not segments and job['status'] == 'cancelled':
            continue
        if not job['collection_complete']:
            issues.append(f"任务 {job_id} 的 Worker 事件未收齐")
        proof = {}
        starts = {}
        for row in db.execute("""SELECT kind,payload FROM events WHERE job_id=?
                AND kind IN ('segment_started','segment_finished') ORDER BY rowid""", (job_id,)):
            event = json.loads(row['payload'])
            if event.get('run_id') == job['worker_run_id']:
                proof.setdefault((event['segment_index'], row['kind']), []).append(event.get('room_identity_status'))
                if row['kind'] == 'segment_started':
                    starts.setdefault(event['segment_index'], event.get('timestamp'))
        for expected, clip in enumerate(segments, 1):
            index = clip['segment_index']
            label = f"任务 {job_id} 第 {index} 片"
            if index != expected:
                issues.append(f"{label} 的片号中断（预期第 {expected} 片）")
            stamp = clip['started_at']
            if (not clip['started'] or stamp is None or stamp != starts.get(index)
                or (previous is not None and stamp < previous)):
                issues.append(f"{label} 缺少有效开录时间或顺序冲突")
            if stamp is not None:
                previous = stamp
            if clip['status'] != 'finished' or not clip['file_path']:
                issues.append(f"{label} 未正常封口（{clip['reason'] or clip['status']}）")
            risks = json.loads(clip['risk_json'] or '[]')
            states = [state for kind in ('segment_started', 'segment_finished')
                      for state in proof.get((index, kind), [None])]
            if (job['admitted_room'], job['anchor_uid']) != (session['room_id'], session['anchor_uid']) or (
                any(state != 'verified' for state in states)
                or any(risk.get('flag') in ('identity_unconfirmed', 'identity_mismatch') for risk in risks)
            ):
                reason = '录错房' if 'mismatch' in states or any(r.get('flag') == 'identity_mismatch' for r in risks) else '身份未核实'
                issues.append(f"{label} {reason}")
            if clip['file_path'] and clip['file_path'] in paths:
                issues.append(f"{label} 与其他切片共用同一个文件")
            paths.add(clip['file_path'])
            clips.append({k: clip[k] for k in ('job_id', 'segment_index', 'file_path', 'started_at')} | {'risks': risks})
    if not clips:
        issues.append('本场没有正式成片')
    return clips, '；'.join(issues) or None


def close_session(db, target_id, session_key, reason=None):
    key = (target_id, session_key)
    if reason:
        db.execute("""UPDATE recording_sessions SET close_reason=COALESCE(close_reason,?)
            WHERE target_id=? AND session_key=?""", (reason, *key))
    session = db.execute("SELECT * FROM recording_sessions WHERE target_id=? AND session_key=?", key).fetchone()
    if not session or session['status'] != 'recording' or not session['close_reason']:
        return
    if db.execute("""SELECT 1 FROM jobs WHERE target_id=? AND session_key=?
        AND status IN ('pending','starting','running','stopping','needs_reconcile')""", key).fetchone():
        return
    setting = db.execute("SELECT * FROM posting_settings WHERE target_id=?", (target_id,)).fetchone()
    binding = setting['binding'] if setting and setting['enabled'] and not session['stopped'] else None
    clips, problem = gather_evidence(db, session) if binding else (None, None)
    status = 'only_record' if not binding else 'review' if problem else 'checking'
    db.execute("""UPDATE recording_sessions SET status=?,binding=?,evidence=?,problem=?
        WHERE target_id=? AND session_key=?""", (status, binding, json.dumps(clips), problem, *key))

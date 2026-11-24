"""整场素材校验、YAML 冻结与后台交付。"""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import sqlite3
import time

from src.mvp.dep_manager import DependencyManager
from src.worker.segment_verifier import verify_segment_file

from .upload_manifest import manifest_for, save_manifest
from .upload_platform import BridgeError


logger = logging.getLogger(__name__)
BEIJING = timezone(timedelta(hours=8))


class SessionDeliveries:
    def __init__(self, runtime, uploads, *, verify_media=None, clock=time.time):
        self.runtime, self.uploads = runtime, uploads
        self.verify_media, self.clock = verify_media, clock

    @property
    def ledger(self):
        return self.runtime.store

    def prepare(self, session):
        evidence = json.loads(session["evidence"])
        binding = json.loads(session["binding"])
        root, asset_root = session["recording_root"], session["recording_directory"]
        clips, problems = [], []
        seen = set()
        verify = self.verify_media
        if verify is None:
            try:
                probe = DependencyManager().get_media_tools().ffprobe
            except (OSError, RuntimeError):
                self.update(session, "review", problem="固定媒体工具不可用，无法校验本场切片")
                return

            def verify(path):
                return verify_segment_file(path, ffprobe_exe=probe, require_mp4=True)

        for clip in evidence:
            try:
                current = self.uploads.clip(clip["job_id"], clip["segment_index"])
                info = current["file"]
                path = Path(info["path"])
                if current["recording_root"] != root or current["asset_root"] != asset_root or (
                    info["path"], current["session_index"]) != (clip["file_path"], clip["session_index"]):
                    raise BridgeError("invalid_path", "文件与本场归档记录不符")
                if path in seen:
                    raise BridgeError("invalid_path", "同一录像被多次引用")
                seen.add(path)
                result = verify(path)
                if not result.is_valid:
                    raise BridgeError("invalid_media", result.reject_reason)
                if self.uploads.clip(clip["job_id"], clip["segment_index"]) != current:
                    raise BridgeError("segment_changed", "文件在校验期间发生变化")
                clips.append(current)
            except BridgeError as exc:
                problems.append(f"任务 {clip['job_id']} 第 {clip['segment_index']} 片：{exc.message}")
        name = (session["nickname"] or "").strip()
        if not name:
            problems.append("开录时缺少已核实的主播昵称")
        if problems:
            self.update(session, "review", problem="；".join(problems))
            return
        try:
            started = datetime.fromtimestamp(session["started_at"], tz=BEIJING).strftime("%Y-%m-%d %H:%M")
        except (OverflowError, OSError, ValueError):
            self.update(session, "review", problem="首片开录时间无效")
            return
        title = "｜".join(filter(None, (name, session["title"], started, "直播回放")))
        frozen = {
            "recording_root": root, "asset_root": asset_root, "clips": clips, "evidence": evidence,
            "account": binding["account"],
            "parameters": {
                "title": title, "description": "", "tags": ["直播回放", name],
                "category_id": binding["category_id"], "copyright": 2,
                "source": "转载", "publish_mode": "self_only",
            },
        }
        return self.uploads.store.freeze_session((session["target_id"], session["session_key"]),
                                                 binding["deployment"], frozen)

    def update(self, session, status, **values):
        values["status"] = status
        assignment = ",".join(f"{column}=?" for column in values)
        with self.ledger.transaction() as db:
            db.execute(f"""UPDATE recording_sessions SET {assignment}
                WHERE target_id=? AND session_key=? AND status=? AND stopped=?""",
                       (*values.values(), session["target_id"], session["session_key"], session["status"], session["stopped"]))

    def write_manifest(self, row):
        frozen = json.loads(row["frozen"])
        root = Path(frozen["asset_root"])
        path, revision = save_manifest(root, manifest_for(root, row["work_id"], frozen))
        reference = f"{row['auto_target']}:{row['auto_session']}"
        request = {
            "manifest_path": path, "asset_root": str(root), "expected_version": revision,
            "execution": {"type": "immediate"},
            "source": {"application": "mumu", "kind": "session", "external_reference": reference[:200]},
        }
        self.uploads.store.save_session_manifest(row, path, revision, request)

    async def deliver(self, session):
        uploads = self.uploads
        try:
            record_id = session["arrangement_id"]
            if session["status"] == "checking":
                record_id = record_id or await asyncio.to_thread(self.prepare, session)
                if record_id:
                    row = await asyncio.to_thread(uploads.store.get, record_id)
                    if row["auto_target"] and not row["stopped"]:
                        await asyncio.to_thread(self.write_manifest, row)
                return
            row = await asyncio.to_thread(uploads.store.get, record_id)
            replay = bool(row["auto_target"] and not session["stopped"] and session["status"] == "pending")
            row = await uploads.drive(record_id, replay=replay)
            if row["task_id"]:
                # 开关可能在请求发出后关闭，此时仍关联无法撤回的原任务。
                with self.ledger.transaction() as db:
                    db.execute("""UPDATE recording_sessions SET status='delivered', problem=NULL
                        WHERE arrangement_id=? AND status IN ('pending','review')""", (record_id,))
            else:
                review = row["rejected"] or session["stopped"] or row["problem_code"] not in (None, "platform_offline")
                status = "review" if review else session["status"]
                await asyncio.to_thread(self.update, session, status, problem=row["problem"], retry_at=self.clock() + 5)
        except BridgeError as exc:
            if exc.code != "posting_stopped":
                await asyncio.to_thread(self.update, session, "review", problem=exc.message)
        except (OSError, RuntimeError):
            await asyncio.to_thread(self.update, session, "review", problem="无法保存本场作品 YAML")

    async def run(self, stop):
        while not stop.is_set():
            if self.ledger is not None and self.uploads.writable():
                try:
                    sessions = await asyncio.to_thread(self.ledger.rows, """SELECT s.* FROM recording_sessions s
                        LEFT JOIN deliveries d ON d.id=s.arrangement_id
                        WHERE (s.status='pending' OR (s.status='checking' AND s.title_step='done')
                           OR (s.status='review' AND (d.task_id IS NOT NULL OR (d.sent=1 AND d.rejected=0))))
                          AND s.retry_at<=? ORDER BY s.retry_at,s.started_at LIMIT 4""", (self.clock(),))
                    results = await asyncio.gather(*(self.deliver(s) for s in sessions), return_exceptions=True)
                    for result in results:
                        if isinstance(result, Exception):
                            logger.error("后台处理整场交付失败", exc_info=result)
                    manual = await asyncio.to_thread(self.ledger.rows, """SELECT id FROM deliveries
                        WHERE auto_target IS NULL AND sent=1 AND rejected=0 AND task_id IS NULL AND retry_at<=?
                        ORDER BY retry_at LIMIT 4""", (self.clock(),))
                    await asyncio.gather(*(self.uploads.drive(row["id"]) for row in manual))
                except (sqlite3.Error, BridgeError):
                    logger.exception("后台交付暂不可用")
            try:
                await asyncio.wait_for(stop.wait(), .5)
            except TimeoutError:
                pass

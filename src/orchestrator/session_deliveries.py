"""服务后台处理整场交付。已冻结的原单由切片投递服务登记、提交和查回。"""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import sqlite3
import time

from src.mvp.dep_manager import DependencyManager
from src.worker.segment_verifier import verify_segment_file

from .queries import quality_flags
from .segment_upload_store import confirmed_input, encode
from .segment_uploads import file_snapshot, freeze_requests
from .storage import ControlError
from .upload_core_client import BridgeError


logger = logging.getLogger(__name__)
BEIJING = timezone(timedelta(hours=8))


def problem_text(code):
    return {
        "core_offline": "原 Core 当前离线",
        "core_unreachable": "无法连接原 Core",
        "core_timeout": "连接原 Core 超时",
        "binding": "请连接本场绑定的原部署",
        "posting_unavailable": "原部署的普通投稿能力不可用",
        "b18_deployment": "原部署使用专用名额",
        "invalid_account": "原账号不可用或 MID 已变化",
        "invalid_submission": "标题、标签、分 P 标题或发布范围不符合原部署能力",
        "too_many_parts": "本场切片超过原部署的分 P 上限",
        "segment_changed": "本场录像或切片风险记录已变化",
        "segment_file_unavailable": "本场有录像不存在、不可读或不在原输出目录中",
        "invalid_path": "原 Core 无法访问录像",
        "invalid_manifest": "原 Core 拒绝了作品清单",
        "invalid_metadata": "原 Core 拒绝了投稿字段",
        "unsupported_option": "原 Core 不支持保存的投稿参数",
        "engine_not_configured": "原 Core 未配置投稿引擎",
        "task_mismatch": "原作品出现冲突任务",
        "task_ambiguous": "原作品出现多条任务",
        "submission_not_found": "已停止提交，暂未查到原平台任务",
        "protocol": "原 Core 回执或分页结果无法完整核对",
    }.get(code, code)


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
        root = self.uploads.output_dir.resolve()
        clips, problems = [], []
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
                info = file_snapshot(clip["file_path"], root)
                path = Path(info["path"])
                if not path.is_relative_to(root / clip["job_id"]):
                    raise BridgeError("invalid_path", "文件位于其他录制任务目录")
                result = verify(path)
                if not result.is_valid:
                    raise BridgeError("invalid_media", result.reject_reason)
                if file_snapshot(info["path"], root) != info:
                    raise BridgeError("segment_changed", "文件在校验期间发生变化")
                clips.append({**clip, "file": info})
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
            "directory": str(root), "clips": clips, "evidence": evidence,
            "file": clips[0]["file"], "quality_flags": quality_flags(clips[0]["risks"]),
            "account": binding["account"],
            "parameters": {
                "title": title, "description": "", "tags": ["直播回放", name],
                "category_id": binding["category_id"], "copyright": 2,
                "source": "转载", "publish_mode": "self_only",
            },
        }
        self.update(session, "pending", prepared=encode(frozen), problem=None)

    def update(self, session, status, **values):
        values["status"] = status
        assignment = ",".join(f"{column}=?" for column in values)
        with self.ledger.transaction() as db:
            db.execute(f"""UPDATE recording_sessions SET {assignment}
                WHERE target_id=? AND session_key=? AND status=? AND stopped=?""",
                       (*values.values(), session["target_id"], session["session_key"], session["status"], session["stopped"]))

    def attach(self, key, row):
        submitting = bool(row["task_id"] or row["stage"] == "submit" and row["attempts"])
        with self.ledger.transaction() as db:
            db.execute("""UPDATE recording_sessions SET arrangement_id=?,
                submit_started=CASE WHEN status='pending' AND ? THEN 1 ELSE submit_started END
                WHERE target_id=? AND session_key=? AND status IN ('pending','only_record')""",
                       (row["id"], submitting, *key))

    async def deliver(self, session):
        uploads = self.uploads
        key = session["target_id"], session["session_key"]
        try:
            if session["status"] == "checking":
                await asyncio.to_thread(self.prepare, session)
                return
            record_id = session["arrangement_id"]
            if session["status"] == "only_record":
                original = record_id or await asyncio.to_thread(uploads.store.find_session, key)
                if original:
                    await asyncio.to_thread(uploads.store.stop_session, original)
                await asyncio.to_thread(self.update, session, "only_record", stopped=2)
                return
            frozen = json.loads(session["prepared"])
            if record_id:
                row = await asyncio.to_thread(uploads.store.get, record_id)
            else:
                deployment = json.loads(session["binding"])["deployment"]
                row = await asyncio.to_thread(uploads.store.arrange_session, key, deployment, frozen, freeze_requests,
                    guard=lambda: self.ledger.session_send_guard(key, frozen["evidence"]))
                record_id = row["id"]
            await asyncio.to_thread(self.attach, key, row)
            if row["auto_target"]:
                if row["task_id"] is None and not row["rejected"]:
                    await uploads.drive(record_id, row["revision"], automatic=True, lookup_only=bool(session["stopped"]))
                    row = await asyncio.to_thread(uploads.store.get, record_id)
            else:
                if confirmed_input(json.loads(row["frozen"])) != confirmed_input(frozen):
                    raise BridgeError("arrangement_conflict", "原手动安排已变化，请查看切片页原单")
                row = await uploads.refresh(record_id)
                if row["rejected"]:
                    raise BridgeError("arrangement_conflict", "原手动安排被拒绝，请查看切片页原单")
            if row["task_id"]:
                await asyncio.to_thread(self.update, session, "delivered", task_id=row["task_id"], problem=None)
            else:
                issue = row["problem_detail"] or (problem_text(row["problem"]) if row["problem"] else None)
                await asyncio.to_thread(self.update, session, "pending", problem=issue, retry_at=self.clock() + 5)
        except (BridgeError, ControlError) as exc:
            status = "review" if exc.code in ("arrangement_conflict", "segment_changed") else session["status"]
            await asyncio.to_thread(self.update, session, status, problem=exc.message if isinstance(exc, BridgeError) else str(exc),
                                    retry_at=self.clock() + 5)

    async def run(self, stop):
        while not stop.is_set():
            if self.ledger is not None and self.uploads.writable():
                try:
                    sessions = await asyncio.to_thread(self.ledger.rows, """SELECT * FROM recording_sessions
                        WHERE ((status='pending' OR (status='checking' AND title_step='done'))
                           OR (status='only_record' AND stopped=1))
                          AND retry_at<=? ORDER BY retry_at,started_at LIMIT 4""", (self.clock(),))
                    results = await asyncio.gather(*(self.deliver(s) for s in sessions), return_exceptions=True)
                    for result in results:
                        if isinstance(result, Exception):
                            logger.error("后台处理整场交付失败", exc_info=result)
                    for record_id in await asyncio.to_thread(self.uploads.store.due_tasks):
                        if stop.is_set():
                            break
                        await self.uploads.refresh(record_id)
                except (sqlite3.Error, BridgeError):
                    logger.exception("后台交付暂不可用")
            try:
                await asyncio.wait_for(stop.wait(), .5)
            except TimeoutError:
                pass

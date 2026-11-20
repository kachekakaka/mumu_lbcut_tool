"""手动预览与确认，以及手动和自动共用的原单恢复。"""

import asyncio
import json
from pathlib import Path
import re
import time
from uuid import uuid4

from .queries import Queries
from .segment_upload_store import DeliveryStore, for_segments
from .storage import ControlError
from .upload_manifest import file_snapshot, manifest_for, read_yaml, save_manifest, version
from .upload_platform import BridgeError, DEFINITE_REJECTIONS, identifier, protocol


def public(row):
    data = {k: row[k] for k in ("id", "work_id", "task_id", "problem")}
    data.update(automatic=bool(row["auto_target"] or row.get("session_attached")), rejected=bool(row["rejected"]),
                observed=row["observed"],
                can_continue=not any(row[k] for k in ("auto_target", "task_id", "rejected", "stopped")),
                can_revise=not row["auto_target"] and bool(row["rejected"]))
    if "job_id" in row:
        data.update(job_id=row["job_id"], segment_index=row["segment_index"])
    return data


class SegmentUploads:
    def __init__(self, runtime, core, output_dir):
        self.runtime, self.core, self.output_dir = runtime, core, output_dir
        self.store = DeliveryStore(runtime)
        self.previews = {}
        self.in_flight = set()

    def writable(self):
        return self.runtime.state == "running"

    def require_writable(self):
        if not self.writable():
            raise BridgeError("upload_paused", "控制台尚未就绪或正在关闭", 503)

    def current_clip(self, job, index):
        try:
            row = Queries(self.store.ledger).segment(job, index)
        except ControlError as exc:
            raise BridgeError(exc.code, str(exc), exc.status) from None
        if row["status"] != "finished" or row["started_at"] is None:
            raise BridgeError("segment_unfinished", "只有已开录且已封口的正式切片可安排")
        root = self.output_dir.resolve()
        file = file_snapshot(row["file_path"], root)
        if not Path(file["path"]).is_relative_to(root / job):
            raise BridgeError("invalid_path", "正式录像位于其他录制任务目录")
        return row, {"job_id": job, "segment_index": index, "file": file, "quality_flags": row["quality_flags"]}

    def clip(self, job, index):
        return self.current_clip(job, index)[1]

    async def context(self, job, index, revise=None):
        self.require_writable()
        row, clip = await asyncio.to_thread(self.current_clip, job, index)
        deployment = await self.core.binding()
        accounts = await self.core.accounts(deployment)
        records = await asyncio.to_thread(self.store.for_segments, [(job, index)])
        original = self.original(records, deployment, revise)
        occupied = {r["account_id"] for r in records if r["deployment"] == deployment}
        saved = json.loads(original["frozen"]) if original else None
        if saved:
            accounts = [a for a in accounts if (a["id"], a["remote_user_id"]) == (
                original["account_id"], saved["account"]["remote_user_id"]
            )]
            self.check_revision_source(original, saved, clip)
        else:
            accounts = [a for a in accounts if a["id"] not in occupied]
        filename = Path(clip["file"]["path"]).name
        defaults = saved["parameters"] if saved else {
            "title": Path(filename).stem, "description": "", "tags": ["直播回放"],
            "source": "转载", "copyright": 2, "publish_mode": "self_only",
        }
        account_id = original["account_id"] if original else None
        if not original:
            setting = await asyncio.to_thread(self.store.ledger.row,
                "SELECT binding FROM posting_settings WHERE target_id=?", (row["target_id"],))
            if setting and setting["binding"]:
                saved = json.loads(setting["binding"])
                defaults["category_id"] = saved["category_id"]
                if saved["deployment"] == deployment and any(a["id"] == saved["account"]["id"]
                    and a["remote_user_id"] == saved["account"]["remote_user_id"] for a in accounts):
                    account_id = saved["account"]["id"]
        return {"deployment": deployment, "accounts": accounts, "filename": filename,
                "duration_sec": row["duration_sec"], "quality_flags": clip["quality_flags"],
                "defaults": defaults, "account_id": account_id}

    @staticmethod
    def original(records, deployment, revise):
        if any(r["auto_target"] or r["session_attached"] for r in records):
            raise BridgeError("arrangement_conflict", "该切片已有整场安排，请查看原单")
        if revise:
            original = next((r for r in records if r["id"] == revise and r["rejected"]
                             and r["deployment"] == deployment), None)
            if original is None:
                raise BridgeError("revision_forbidden", "原单已变化或未明确拒绝，请查看原单")
            return original
        return None

    def check_revision_source(self, original, saved, clip):
        if original["problem_code"] == "preview_expired" and (
            saved["root"] != str(self.output_dir.resolve()) or saved["clips"][0]["file"] != clip["file"]
        ):
            raise BridgeError("segment_changed", "原安排的录像已变化，请核对")

    def draft(self, job, index, deployment, frozen, revise):
        root = Path(frozen["root"])
        slot = (job, index, deployment, frozen["account"]["id"])
        with self.store.ledger.transaction() as db:
            records = list(for_segments(db, [(job, index)]))
            original = self.original(records, deployment, revise)
            if any(r["deployment"] == deployment and r["account_id"] == slot[3]
                   and (not original or original["id"] != r["id"]) for r in records):
                raise BridgeError("arrangement_conflict", "该账号已有安排，请查看原单")
            clip = self.clip(job, index)
            if clip != frozen["clips"][0]:
                raise BridgeError("segment_changed", "切片已变化，请重新打开安排窗口")
            work_id, revision = str(uuid4()), None
            if original:
                saved = json.loads(original["frozen"])
                if (slot[3], frozen["account"]["remote_user_id"]) != (
                    original["account_id"], saved["account"]["remote_user_id"]
                ):
                    raise BridgeError("invalid_account", "请使用原安排的账号和 MID")
                self.check_revision_source(original, saved, clip)
                work_id, revision = original["work_id"], original["config_version"]
            db.execute("INSERT OR IGNORE INTO upload_drafts VALUES (?,?,?,?,?,?)", (*slot, work_id, revision))
            row = db.execute("""SELECT work_id,config_version FROM upload_drafts WHERE
                job_id=? AND segment_index=? AND deployment=? AND account_id=?""", slot).fetchone()
        return self.write_draft(slot, row, root, frozen)

    def write_draft(self, slot, row, root, frozen):
        with self.store.ledger.transaction() as db:
            self.require_writable()
            live = db.execute("""SELECT work_id,config_version FROM upload_drafts WHERE
                job_id=? AND segment_index=? AND deployment=? AND account_id=?""", slot).fetchone()
            if live != row:
                raise BridgeError("stale_preview", "该切片的预览已变化，请重新预览")
            work_id = row["work_id"]
            manifest = manifest_for(root, work_id, frozen)
            path, revision = save_manifest(root, work_id, manifest, row["config_version"])
            db.execute("""UPDATE upload_drafts SET config_version=? WHERE
                job_id=? AND segment_index=? AND deployment=? AND account_id=?""", (revision, *slot))
        return work_id, path, revision, manifest

    async def preview(self, job, index, deployment, account, params, revise=None):
        self.require_writable()
        if not params["title"] or any(not tag or "," in tag for tag in params["tags"]):
            raise BridgeError("invalid_metadata", "请填写标题和有效标签", 422)
        if (params["copyright"] == 2) != bool(params["source"]):
            raise BridgeError("invalid_metadata", "转载需填写来源，自制请留空", 422)
        account = await self.core.check_account(deployment, account)
        clip = await asyncio.to_thread(self.clip, job, index)
        frozen = {"root": str(self.output_dir.resolve()), "clips": [clip], "account": account, "parameters": params}
        try:
            work_id, path, revision, manifest = await asyncio.to_thread(
                self.draft, job, index, deployment, frozen, revise
            )
        except OSError:
            raise BridgeError("manifest_write", "无法保存作品 YAML", 503) from None
        data = await self.core.preview(deployment, {"manifest_path": path, "asset_root": frozen["root"]})
        platform_preview = identifier(data.get("preview_id"))
        warnings = data.get("warnings")
        if (data.get("work_id") != work_id or data.get("config_version") != revision
            or not isinstance(data.get("expires_at"), str) or not isinstance(warnings, list)
            or not all(isinstance(w, str) for w in warnings) or not self.matches_preview(data.get("manifest"), manifest)):
            raise protocol()
        now = time.monotonic()
        self.previews = {key: item for key, item in self.previews.items() if item["expires"] > now}
        if len(self.previews) >= 256:
            self.previews.pop(next(iter(self.previews)))
        ticket = str(uuid4())
        self.previews[ticket] = {
            "expires": now + 600, "part": (job, index), "deployment": deployment, "work_id": work_id,
            "path": path, "version": revision, "frozen": frozen, "revise": revise,
            "request": {"preview_id": platform_preview, "execution": {"type": "immediate"},
                        "source": {"application": "mumu", "kind": "manual", "external_reference": f"{job}:{index}"}},
        }
        return {"ticket": ticket, "expires_at": data["expires_at"], "warnings": warnings,
                "work": data["manifest"]["work"], "video": data["manifest"]["assets"]["videos"][0]["path"],
                "options": data["manifest"]["upload_units"][0]["targets"][0]["options"],
                "account": account, "quality_flags": clip["quality_flags"],
                "media_url": f"/api/uploads/preview/{ticket}/media"}

    @staticmethod
    def matches_preview(actual, expected):
        try:
            unit, = actual["upload_units"]
            target, = unit["targets"]
            video, = actual["assets"]["videos"]
            original = expected["upload_units"][0]
            return (
                actual["version"] == 1
                and all(actual["work"][k] == v for k, v in expected["work"].items())
                and all(video[k] == v for k, v in expected["assets"]["videos"][0].items())
                and all(unit[k] == original[k] for k in ("id", "mode", "videos"))
                and all(target[k] == original["targets"][0][k] for k in ("platform", "account", "enabled"))
                and all(target["options"][k] == v for k, v in original["targets"][0]["options"].items())
            )
        except (KeyError, TypeError, ValueError):
            return False

    def ticket(self, key):
        preview = self.previews.get(key)
        if not preview or preview["expires"] <= time.monotonic():
            raise BridgeError("stale_preview", "预览已失效，请重新预览")
        return preview

    def preview_media(self, key):
        preview = self.ticket(key)
        clip = self.clip(*preview["part"])
        if clip != preview["frozen"]["clips"][0]:
            raise BridgeError("segment_changed", "录像或质量提示已变化，请重新预览")
        return clip["file"]["path"]

    async def confirm(self, key, quality_confirmed):
        self.require_writable()
        preview = self.ticket(key)
        if record := preview.get("record"):
            return public(await asyncio.to_thread(self.store.get, record))
        frozen = preview["frozen"]
        if frozen["clips"][0]["quality_flags"] and not quality_confirmed:
            raise BridgeError("quality_confirmation", "请先核对切片质量提示")
        original = await asyncio.to_thread(self.store.for_segments, [preview["part"]])
        automatic = next((r for r in original if r["auto_target"] or r["session_attached"]), None)
        if automatic:
            return public(automatic)
        await self.core.check_account(preview["deployment"], frozen["account"])
        self.require_writable()
        record, created = await asyncio.to_thread(self.store.confirm, preview, self.clip, self.check_version)
        preview["record"] = record
        return public(await self.drive(record, replay=True) if created else await asyncio.to_thread(self.store.get, record))

    @staticmethod
    def check_version(path, expected):
        try:
            if version(read_yaml(Path(path))) == expected:
                return
        except OSError:
            pass
        raise BridgeError("version_conflict", "作品 YAML 已变化或不可读，请重新预览")

    def preflight(self, row):
        frozen = json.loads(row["frozen"])
        root = self.output_dir.resolve()
        if str(root) != frozen["root"]:
            raise BridgeError("segment_changed", "录像输出目录已变化")
        for clip in frozen["clips"]:
            if file_snapshot(clip["file"]["path"], root) != clip["file"]:
                raise BridgeError("segment_changed", f"任务 {clip['job_id']} 第 {clip['segment_index']} 片已变化")
        self.check_version(row["manifest_path"], row["config_version"])

    async def link(self, row, result):
        tasks = result.get("task_ids")
        if (result.get("work_id") != row["work_id"] or result.get("config_version") != row["config_version"]
            or result.get("status") != "accepted" or not isinstance(tasks, list) or len(tasks) != 1):
            raise protocol()
        await asyncio.to_thread(self.store.update, row["id"], task_id=identifier(tasks[0]), problem=None,
                                problem_code=None, retry_at=0)

    async def drive(self, record_id, *, replay=False):
        row = await asyncio.to_thread(self.store.get, record_id)
        if record_id in self.in_flight or row["task_id"] or row["rejected"] or row["stopped"] or not row["request"]:
            return row
        self.in_flight.add(record_id)
        sent_now = False
        expired_unaccepted = False
        try:
            if row["sent"]:
                result = await self.core.result(row)
                if result:
                    await self.link(row, result)
                    return await asyncio.to_thread(self.store.get, record_id)
                if not replay:
                    raise BridgeError("submission_not_found", "暂未查到原任务，请核对原安排")
            if not replay:
                return row
            self.require_writable()
            await asyncio.to_thread(self.preflight, row)
            await self.core.check_account(row["deployment"], json.loads(row["frozen"])["account"])
            self.require_writable()
            if await asyncio.to_thread(self.store.mark_sent, row, self.clip):
                sent_now = True
                try:
                    receipt = await self.core.submit(row)
                except BridgeError as exc:
                    if not (row["sent"] and not row["auto_target"]
                            and exc.status == 409 and exc.code == "preview_expired"):
                        raise
                    # 已查回为空并重放原请求。再查一次，优先关联可能在此期间写入的回执。
                    receipt = await self.core.result(row)
                    if receipt is None:
                        expired_unaccepted = True
                        raise
                await self.link(row, receipt)
        except BridgeError as exc:
            rejected = expired_unaccepted or (
                not row["sent"] and ((not sent_now and exc.code in {
                    "invalid_account", "version_conflict", "segment_changed", "segment_file_unavailable", "invalid_path",
                }) or (sent_now and exc.code in DEFINITE_REJECTIONS and 400 <= exc.status < 500))
            )
            if exc.code != "posting_stopped":
                await asyncio.to_thread(self.store.update, record_id, rejected=int(rejected),
                                        problem=exc.message, problem_code=exc.code, retry_at=time.time() + 5)
        finally:
            self.in_flight.discard(record_id)
        return await asyncio.to_thread(self.store.get, record_id)

    async def detail(self, record_id):
        row = await self.drive(record_id)
        if not row["task_id"]:
            return public(row)
        try:
            task = await self.core.task(row["deployment"], row["task_id"])
            status = task.get("status")
            if (task.get("id") != row["task_id"] or task.get("work_id") != row["work_id"]
                or not isinstance(status, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,47}", status)):
                raise protocol()
            await asyncio.to_thread(self.store.update, record_id, observed=status)
            row["observed"] = status
        except BridgeError as exc:
            row["problem"] = exc.message
        return public(row)

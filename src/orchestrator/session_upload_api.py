"""主播投稿设置和场次结果只在受保护的本机控制台提供。"""

import asyncio
import json

from pydantic import Field

from .segment_upload_api import JobId
from .storage import ControlError
from .upload_core_client import BridgeError, DTO


class PostingSettings(DTO):
    enabled: bool
    deployment: str | None = Field(default=None, min_length=1, max_length=128)
    account_id: str | None = Field(default=None, min_length=1, max_length=128)
    remote_user_id: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,31}$")
    category_id: int | None = Field(default=None, ge=1, le=65535)


def install_session_uploads(router, runtime, uploads):
    base = "/session-uploads/{target_id}"

    def ledger():
        if runtime.store is None:
            raise BridgeError("service_unavailable", "录制服务尚未就绪", 503)
        return runtime.store

    @router.get(base + "/settings")
    async def get_settings(target_id: JobId):
        row = await asyncio.to_thread(ledger().row, "SELECT * FROM posting_settings WHERE target_id=?", (target_id,))
        saved = json.loads(row["binding"]) if row and row["binding"] else None
        result = {"enabled": bool(row and row["enabled"]), "saved": saved, "deployment": None, "accounts": []}
        try:
            binding = await uploads.core.binding()
            modes, _ = await uploads.core.capabilities(binding)
            if "self_only" not in modes:
                raise BridgeError("posting_unavailable", "当前部署未开启仅自己可见投稿")
            accounts = await uploads.core.accounts(binding)
            result.update(deployment=binding["deployment"], accounts=[
                a.model_dump(include={"id", "remote_user_id", "display_name"}) for a in accounts if a.remote_user_id
            ])
        except BridgeError as exc:
            result["problem"] = exc.message
        return result

    @router.post(base + "/settings")
    async def set_settings(target_id: JobId, body: PostingSettings):
        uploads.require_writable()
        saved = None
        if body.enabled:
            if not (body.deployment and body.account_id and body.remote_user_id and body.category_id):
                raise BridgeError("invalid_request", "开启自动投稿需选择部署、账号和分区", 422)
            binding = await uploads.core.binding(body.deployment)
            modes, _ = await uploads.core.capabilities(binding)
            if "self_only" not in modes:
                raise BridgeError("posting_unavailable", "原部署未开启仅自己可见投稿")
            accounts = await uploads.core.accounts(binding)
            account = next((a for a in accounts if a.id == body.account_id and a.remote_user_id == body.remote_user_id), None)
            if not account:
                raise BridgeError("invalid_account", "所选账号目前不可用或 MID 已变化")
            saved = {"deployment": body.deployment, "category_id": body.category_id,
                     "account": account.model_dump(include={"id", "remote_user_id", "display_name"})}
        uploads.require_writable()
        try:
            await asyncio.to_thread(ledger().save_posting_settings, target_id, body.enabled, saved)
        except ControlError as exc:
            raise BridgeError(exc.code, str(exc), exc.status) from None
        return {"enabled": body.enabled}

    def recent(target_id):
        rows = ledger().rows("""SELECT s.session_key,s.started_at,s.title,s.status,s.stopped,s.problem,s.arrangement_id,s.task_id,
            EXISTS (SELECT 1 FROM jobs j WHERE j.target_id=s.target_id AND j.session_key=s.session_key
                AND j.status IN ('pending','starting','running','stopping','needs_reconcile')) AS active
            FROM recording_sessions s WHERE s.target_id=? ORDER BY s.started_at DESC,s.session_key LIMIT 30""", (target_id,))
        for row in rows:
            stopped = row.pop("stopped")
            active = row.pop("active")
            if row["status"] == "recording" and not active:
                row["status"] = "waiting_close"
            if row["status"] == "pending":
                if stopped or row["problem"]:
                    row["status"] = "review"
                    if stopped and not row["problem"]:
                        row["problem"] = "提交已开始，继续查回原平台任务"
                elif row["arrangement_id"] and uploads.store.get(row["arrangement_id"])["stage"] == "submit":
                    row["status"] = "registered"
            if row["task_id"] and row["arrangement_id"]:
                observed = uploads.public(uploads.store.get(row["arrangement_id"]), detail=True)
                row.update(bvid=observed.get("bvid"), platform_status=observed["status"])
        return {"items": rows}

    @router.get(base)
    async def session_status(target_id: JobId):
        return await asyncio.to_thread(recent, target_id)

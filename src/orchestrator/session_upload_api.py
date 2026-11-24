"""主播的自动投稿设置及场次交付摘要。"""

import asyncio
import json
from typing import Annotated

from fastapi import Path
from pydantic import Field

from .storage import ControlError
from .upload_platform import BridgeError, DTO


TargetId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]


class PostingSettings(DTO):
    enabled: bool
    deployment: str | None = Field(default=None, min_length=1, max_length=128)
    account_id: str | None = Field(default=None, min_length=1, max_length=128)
    remote_user_id: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,31}$")
    category_id: int | None = Field(default=None, ge=1, le=65535)


def install_session_uploads(router, uploads):
    base = "/session-uploads/{target_id}"

    @router.get(base + "/settings")
    async def get_settings(target_id: TargetId):
        row = await asyncio.to_thread(uploads.store.ledger.row,
                                      "SELECT * FROM posting_settings WHERE target_id=?", (target_id,))
        saved = json.loads(row["binding"]) if row and row["binding"] else None
        result = {"enabled": bool(row and row["enabled"]), "saved": saved, "deployment": None, "accounts": []}
        try:
            deployment = await uploads.core.binding(uploads.expected_deployment())
            result.update(deployment=deployment, accounts=await uploads.core.accounts(deployment))
        except BridgeError as exc:
            result["problem"] = exc.message
        return result

    @router.post(base + "/settings")
    async def set_settings(target_id: TargetId, body: PostingSettings):
        uploads.require_writable()
        saved = None
        if body.enabled:
            if not (body.deployment and body.account_id and body.remote_user_id and body.category_id):
                raise BridgeError("invalid_request", "开启自动投稿需选择部署、账号和分区", 422)
            uploads.expected_deployment(body.deployment)
            account = await uploads.core.check_account(body.deployment, {
                "id": body.account_id, "remote_user_id": body.remote_user_id,
            })
            saved = {"deployment": body.deployment, "category_id": body.category_id, "account": account}
        uploads.require_writable()
        try:
            await asyncio.to_thread(uploads.store.ledger.save_posting_settings, target_id, body.enabled, saved)
        except ControlError as exc:
            raise BridgeError(exc.code, str(exc), exc.status) from None
        return {"enabled": body.enabled}

    @router.get(base)
    def recent(target_id: TargetId):
        rows = uploads.store.ledger.rows("""SELECT s.session_key,s.started_at,s.title,s.status,s.stopped,s.problem,
            s.arrangement_id,d.task_id,d.observed,
            EXISTS (SELECT 1 FROM jobs j WHERE j.target_id=s.target_id AND j.session_key=s.session_key
                AND j.status IN ('pending','starting','running','stopping','needs_reconcile')) AS active
            FROM recording_sessions s LEFT JOIN deliveries d ON d.id=s.arrangement_id
            WHERE s.target_id=? ORDER BY s.started_at DESC,s.session_key LIMIT 30""", (target_id,))
        for row in rows:
            stopped = row.pop("stopped")
            active = row.pop("active")
            if row["status"] == "recording" and not active:
                row["status"] = "waiting_close"
            if row["status"] == "pending" and stopped and not row["problem"]:
                row["problem"] = "仅查回原请求"
        return {"items": rows}

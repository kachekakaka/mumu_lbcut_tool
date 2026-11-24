"""本机控制台的上传入口。所有平台调用固定到本机回环端口。"""

import asyncio
import ipaddress
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Path, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field, StringConstraints

from .segment_uploads import SegmentUploads, public
from .session_deliveries import SessionDeliveries
from .session_upload_api import install_session_uploads
from .upload_platform import BridgeError, DTO, ORIGIN, UploadPlatform

BASE = "/api/uploads"
CONSOLE_ORIGIN = "http://127.0.0.1:8000"
JobId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]
Index = Annotated[int, Path(ge=1, le=2**31 - 1)]
SegmentKey = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}:[0-9]{1,10}$")]


class Parameters(DTO):
    title: str = Field(min_length=1, max_length=512)
    description: str = Field(default="", max_length=8192)
    tags: list[Annotated[str, StringConstraints(min_length=1, max_length=128)]] = Field(min_length=1, max_length=30)
    category_id: int = Field(ge=1, le=65535)
    copyright: Literal[1, 2]
    source: str = Field(default="", max_length=2048)
    publish_mode: Literal["public", "self_only"]


class PreviewRequest(DTO):
    deployment: str = Field(min_length=1, max_length=128)
    account_id: str = Field(min_length=1, max_length=128)
    remote_user_id: str = Field(pattern=r"^[1-9][0-9]{0,31}$")
    parameters: Parameters
    revise: str | None = Field(default=None, max_length=36)


class Confirmation(DTO):
    quality_confirmed: bool = False


def failure(code, message, status):
    return JSONResponse({"code": code, "message": message}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def install_uploads(app, config, runtime, platform=None):
    router = APIRouter(prefix=BASE)
    service = SegmentUploads(runtime, platform or UploadPlatform(), config.output_dir)
    app.state.session_deliveries = SessionDeliveries(runtime, service)

    @app.middleware("http")
    async def protect(req, handler):
        if req.url.path != BASE and not req.url.path.startswith(BASE + "/"):
            response = await handler(req)
            if response.headers.get("content-type", "").startswith("text/html"):
                response.headers["Content-Security-Policy"] = f"frame-ancestors 'none'; frame-src {ORIGIN}"
            return response
        headers = req.headers
        try:
            loopback = ipaddress.ip_address(req.client.host).is_loopback
        except (ValueError, AttributeError):
            loopback = False
        if (not loopback or headers.get("host") != "127.0.0.1:8000"
            or headers.get("origin", CONSOLE_ORIGIN) != CONSOLE_ORIGIN
            or headers.get("sec-fetch-site", "same-origin") != "same-origin"
            or any(k.startswith("x-forwarded") or k in ("forwarded", "via", "x-real-ip") for k in headers)):
            return failure("local_only", "请在录制机打开 http://127.0.0.1:8000", 403)
        if req.method == "POST" and headers.get("content-type", "").split(";")[0] != "application/json":
            return failure("invalid_request", "上传设置需要 JSON 请求", 415)
        try:
            response = await handler(req)
        except BridgeError as exc:
            return failure(exc.code, exc.message, exc.status)
        if response.status_code == 422:
            return failure("invalid_request", "请检查账号、分区和投稿字段", 422)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @router.get("/panel")
    async def panel(page: Literal["accounts", "works", "tasks"], task_id: UUID | None = None):
        route = page
        deployment = service.expected_deployment()
        await service.core.binding(deployment)
        if task_id:
            if page != "tasks":
                raise BridgeError("invalid_request", "请打开任务页面", 422)
            task = str(task_id)
            row = await asyncio.to_thread(service.store.ledger.row,
                                         "SELECT deployment FROM deliveries WHERE task_id=?", (task,))
            if not row:
                raise BridgeError("task_not_found", "找不到这个本机交付任务", 404)
            service.expected_deployment(row["deployment"])
            route += "/" + task
        await service.core.panel(route)
        return {"ready": True, "deployment": deployment}

    @router.get("/segment-uploads")
    async def summaries(keys: Annotated[list[SegmentKey], Query(min_length=1, max_length=200)]):
        parts = [(job, int(index)) for job, index in (key.split(":") for key in keys)]
        rows = await asyncio.to_thread(service.store.for_segments, parts)
        return {"items": [public(row) for row in rows]}

    @router.get("/segment-uploads/context/{job_id}/{segment_index}")
    async def context(job_id: JobId, segment_index: Index, revise: UUID | None = None):
        return await service.context(job_id, segment_index, str(revise) if revise else None)

    @router.post("/segment-uploads/preview/{job_id}/{segment_index}")
    async def preview(job_id: JobId, segment_index: Index, body: PreviewRequest):
        params = body.parameters.model_dump()
        for key in ("title", "description", "source"):
            params[key] = params[key].strip()
        params["tags"] = [tag.strip() for tag in params["tags"]]
        return await service.preview(job_id, segment_index, body.deployment, {
            "id": body.account_id, "remote_user_id": body.remote_user_id,
        }, params, body.revise)

    @router.get("/preview/{ticket}/media")
    def media(ticket: UUID):
        return FileResponse(service.preview_media(str(ticket)), media_type="video/mp4")

    @router.post("/segment-uploads/confirm/{ticket}")
    async def confirm(ticket: UUID, body: Confirmation):
        return await service.confirm(str(ticket), body.quality_confirmed)

    @router.get("/segment-uploads/{record_id}")
    async def detail(record_id: UUID):
        return await service.detail(str(record_id))

    @router.get("/tasks/{task_id}")
    async def observed_task(task_id: UUID):
        row = await asyncio.to_thread(service.store.ledger.row,
            "SELECT id FROM deliveries WHERE task_id=?", (str(task_id),))
        if not row:
            raise BridgeError("task_not_found", "找不到这个本机交付任务", 404)
        return await service.detail(row["id"])

    @router.post("/segment-uploads/{record_id}/continue")
    async def resume(record_id: UUID, body: Confirmation):
        service.require_writable()
        row = await asyncio.to_thread(service.store.get, str(record_id))
        if row["auto_target"]:
            raise BridgeError("invalid_request", "整场安排由主播设置控制", 409)
        return public(await service.drive(str(record_id), replay=True))

    install_session_uploads(router, service)
    app.include_router(router)
    return service

"""复用账号页本机保护的切片投递接口。"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import Field, StringConstraints

from .queries import Queries
from .segment_upload_core import PostingCore
from .segment_upload_store import ArrangementStore
from .segment_uploads import SegmentUploads
from .upload_core_client import BridgeError, DTO


BASE = "/segment-uploads"
JobId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]
Index = Annotated[int, Path(ge=0, le=2**31 - 1)]
SegmentKey = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}:[0-9]{1,10}$")]


class Parameters(DTO):
    title: str = Field(min_length=1, max_length=512)
    description: str = Field(default="", max_length=8192)
    tags: list[Annotated[str, StringConstraints(min_length=1, max_length=128)]] = Field(min_length=1, max_length=30)
    category_id: int = Field(ge=1, le=65535)
    copyright: Literal[1, 2]
    source: str = Field(default="", max_length=2048)
    publish_mode: Literal["public", "self_only"]


class Confirmation(DTO):
    ticket: str = Field(min_length=36, max_length=36)
    account_id: str = Field(min_length=1, max_length=128)
    parameters: Parameters
    quality_confirmed: bool = False


class Continue(DTO):
    revision: int = Field(ge=1)


def install_segment_uploads(router, config, runtime, core):
    def segment(job, index):
        if runtime.store is None:
            raise BridgeError("service_unavailable", "录制服务尚未就绪", 503)
        return Queries(runtime.store).segment(job, index)

    service = SegmentUploads(
        ArrangementStore(config.data_dir / "upload-core" / "segment-uploads.sqlite3"),
        PostingCore(core), segment, config.output_dir, writable=lambda: runtime.state == "running",
        guard=lambda job, index: runtime.store.segment_send_guard(job, index),
        session_guard=lambda *args, **kwargs: runtime.store.session_send_guard(*args, **kwargs),
    )

    @router.get(BASE)
    async def summaries(keys: Annotated[list[SegmentKey], Query(min_length=1, max_length=200)]):
        segments = [(job, int(index)) for job, index in (key.split(":") for key in keys)]
        return {"items": await service.summaries(segments)}

    @router.get(BASE + "/context/{job_id}/{segment_index}")
    async def context(job_id: JobId, segment_index: Index, revise: UUID | None = None):
        return await service.context(job_id, segment_index, str(revise) if revise else None)

    @router.post(BASE)
    async def confirm(req: Request, body: Confirmation):
        params = body.parameters.model_dump()
        for key in ("title", "description", "source"):
            params[key] = params[key].strip()
        params["tags"] = [tag.strip() for tag in params["tags"]]
        result = await service.confirm(req.state.key, body.ticket, body.account_id, params, body.quality_confirmed)
        return JSONResponse(result, status_code=200 if result["task_id"] or result["rejected"] else 202)

    @router.get(BASE + "/{record_id}")
    async def detail(record_id: UUID):
        return service.public(await service.refresh(str(record_id), force=True), detail=True)

    @router.post(BASE + "/{record_id}/continue")
    async def resume(record_id: UUID, body: Continue):
        return await service.drive(str(record_id), body.revision)

    return service

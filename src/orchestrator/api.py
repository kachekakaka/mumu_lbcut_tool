"""HTTP 薄接口：严格输入、统一错误、只提交持久命令。"""
import logging
from pathlib import Path as FilePath
from typing import Annotated, Generic, Literal, TypeVar
from uuid import UUID

from fastapi import APIRouter, Body, FastAPI, Header, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, model_validator
from starlette.exceptions import HTTPException

from src.orchestrator.desktop import reveal_in_file_manager
from src.orchestrator.storage import ControlError
from src.orchestrator.queries import Queries

logger = logging.getLogger(__name__)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Empty(StrictModel):
    pass


class Add(StrictModel):
    targets: list[Annotated[str, Field(max_length=2048)]] = Field(min_length=1, max_length=100)
    manual_only: StrictBool = False
    allow_unconfirmed_identity: StrictBool = False


class Update(StrictModel):
    auto_record: StrictBool


class InstanceUpdate(StrictModel):
    enabled: StrictBool


class StopTarget(StrictModel):
    session_key: str = Field(min_length=1, max_length=2048, pattern=r"\S")


class Reconcile(StrictModel):
    job_id: str | None = Field(default=None, min_length=1, max_length=128)


class Pagination(StrictModel):
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class Instances(Pagination):
    status: Literal["idle", "busy", "booting", "offline", "reconcile"] | None = None
    enabled: bool | None = None


class Targets(Pagination):
    q: str | None = Field(default=None, max_length=2048)
    trusted_status: Literal["live", "ended", "unknown"] | None = None
    auto_record: bool | None = None
    has_notice: bool | None = None


class Timed(Pagination):
    from_time: float | None = None
    to_time: float | None = None

    @model_validator(mode="after")
    def ordered(self):
        if self.from_time is not None and self.to_time is not None and self.from_time >= self.to_time:
            raise ValueError("开始时间必须早于结束时间")
        return self


class Jobs(Timed):
    active: bool = True
    target_id: str | None = Field(default=None, max_length=128)
    status: Literal["pending", "starting", "running", "stopping", "needs_reconcile", "finished", "finished_with_warnings", "failed", "cancelled"] | None = None


class Segments(Timed):
    target_id: str | None = Field(default=None, max_length=128)
    job_id: str | None = Field(default=None, max_length=128)
    status: Literal["started", "finished", "aborted"] | None = None
    has_quality_flags: bool | None = None


class LogQuery(StrictModel):
    lines: int = Field(default=200, ge=1, le=2000)


class ErrorResponse(StrictModel):
    code: str
    message: str
    details: object = None


class CommandReceipt(StrictModel):
    command_id: str
    status: Literal["pending", "running", "succeeded", "failed", "interrupted"]


class ReasonResponse(StrictModel):
    code: str
    detail: str | None


class InstanceResponse(StrictModel):
    vmindex: int
    name: str
    status: str
    enabled: bool
    job_id: str | None
    reason: ReasonResponse | None
    updated_at: float
    target_id: str | None
    nickname: str | None


class TargetResponse(StrictModel):
    id: str
    raw: str
    nickname: str | None
    created_at: float
    updated_at: float
    trusted_status: str
    observed_status: str
    observed_at: float | None
    confirmed_at: float | None
    auto_record: bool
    room_id: str | None
    anchor_uid: str | None
    session_key: str | None
    recording_intent: bool
    suppressed: bool
    needs_fix_reason: ReasonResponse | None
    note: str | None
    attempts: int
    retry_at: float
    is_retrying: bool
    last_failure_reason: ReasonResponse | None
    active_job_id: str | None
    active_job_status: str | None


class RecoveryContextResponse(StrictModel):
    trigger: ReasonResponse | None
    last_failure: ReasonResponse | None
    outcome: Literal["recovering", "recovered", "failed", "cancelled"]


class JobResponse(StrictModel):
    id: str
    target_id: str
    nickname: str | None
    target_raw: str | None
    vmindex: int | None
    status: str
    created_at: float
    started_at: float | None
    recording_started_at: float | None
    ended_at: float | None
    exit_code: int | None
    stop_reason: ReasonResponse | None
    error_reason: ReasonResponse | None
    recovery_context: RecoveryContextResponse | None
    elapsed_sec: float | None
    fps: float | None
    room_identity_status: str | None
    room_identity_checked_at: float | None
    room_identity_confirmed_at: float | None
    room_identity_age_sec: float | None
    room_identity_stale: bool | None
    room_identity_reason: str | None
    heartbeat_at: float | None
    finished_segment_count: int
    aborted_segment_count: int
    reconcile_requested: bool


class EventResponse(StrictModel):
    event_id: str | None
    event: str
    timestamp: float | None
    received_at: float
    action: str | None
    reason: ReasonResponse | None
    phase: str | None
    attempt: int | None


class JobDetailResponse(JobResponse):
    recent_events: list[EventResponse]


class QualityResponse(StrictModel):
    flag: str | None
    suspected_start_time: float | None


class SegmentResponse(StrictModel):
    job_id: str
    target_id: str
    nickname: str | None
    target_raw: str | None
    segment_index: int
    status: str
    file_path: str | None
    duration_sec: float | None
    file_size_mb: float | None
    is_final: bool
    reason: ReasonResponse | None
    created_at: float
    started_at: float | None
    ended_at: float | None
    updated_at: float
    quality_flags: list[QualityResponse]


class CommandResponse(StrictModel):
    id: str
    request_id: str
    kind: str
    subject: str
    status: str
    result: dict | None
    error_code: str | None
    message: str | None
    created_at: float
    started_at: float | None
    finished_at: float | None


class LogResponse(StrictModel):
    job_id: str
    available: bool
    text: str
    returned_lines: int
    truncated: bool
    reason: str | None


class OpenFolderResponse(StrictModel):
    """启动请求回执，不代表文件管理器已在桌面展示或选中文件。"""
    status: Literal["succeeded"]
    file_path: str
    folder_path: str
    revealed: bool


T = TypeVar("T")


class PageResponse(StrictModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int
    generated_at: float


def error(code, message, status, details=None):
    return JSONResponse(ErrorResponse(code=code, message=message, details=details).model_dump(), status_code=status)


def parameters(request, model):
    values = dict(request.query_params)
    if len(values) != len(request.query_params.multi_items()):
        raise ControlError("invalid_parameters", "不允许重复查询参数", 422)
    for key in ("active", "auto_record", "enabled", "has_notice", "has_quality_flags"):
        if key in values and values[key] not in ("true", "false"):
            raise ControlError("invalid_parameters", "布尔查询参数只能使用 true 或 false", 422)
    try:
        return model.model_validate(values).model_dump()
    except ValidationError as exc:
        raise ControlError("invalid_parameters", "查询字段、类型或范围错误", 422) from exc


def install_api(app: FastAPI, runtime):
    @app.exception_handler(ControlError)
    async def control_error(request, exc):
        return error(exc.code, str(exc), exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        malformed = any(e["type"] == "json_invalid" for e in exc.errors())
        return error("invalid_json" if malformed else "invalid_parameters", "请求 JSON 无法解析" if malformed else "请求字段、类型或范围错误", 400 if malformed else 422)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error("not_found" if exc.status_code == 404 else "http_error", "接口或资源不存在" if exc.status_code == 404 else "请求方法或资源不支持", exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        logger.exception("HTTP 接口异常", exc_info=exc)
        return error("internal_error", "服务内部错误，请查看服务日志", 500)

    router = APIRouter(prefix="/api")

    def query():
        if runtime.store is None:
            raise ControlError("service_unavailable", "服务尚未就绪", 503)
        return Queries(runtime.store)

    def submit(kind, subject, payload, key):
        if key is None:
            raise ControlError("invalid_request_id", "必须提供 UUID 格式的 Idempotency-Key", 422)
        try:
            key = str(UUID(key))
        except ValueError as exc:
            raise ControlError("invalid_request_id", "Idempotency-Key 必须是 UUID", 422) from exc
        command_id = runtime.submit(kind, subject, payload, key)
        result = query().command(command_id)
        receipt = CommandReceipt(command_id=command_id, status=result["status"])
        return JSONResponse(receipt.model_dump(), status_code=202 if result["status"] in ("pending", "running") else 200)

    @router.get("/system")
    def system(request: Request):
        parameters(request, Empty)
        return runtime.system()

    @router.get("/overview")
    def overview(request: Request):
        return query().overview(**parameters(request, Pagination))

    def list_route(kind, model, response):
        def listing(request: Request):
            params = parameters(request, model)
            limit, offset = params.pop("limit"), params.pop("offset")
            return query().listing(kind, params, limit, offset)
        router.add_api_route("/" + kind, listing, methods=["GET"], response_model=PageResponse[response])

    for kind, model, response in (("targets", Targets, TargetResponse), ("instances", Instances, InstanceResponse), ("jobs", Jobs, JobResponse), ("segments", Segments, SegmentResponse)):
        list_route(kind, model, response)

    @router.get("/jobs/{key}", response_model=JobDetailResponse)
    def job(key: str, request: Request):
        parameters(request, Empty)
        return query().job(key)

    @router.get("/jobs/{key}/log", response_model=LogResponse)
    def log(key: str, request: Request):
        return query().log(key, **parameters(request, LogQuery))

    @router.get("/commands/{key}", response_model=CommandResponse)
    def command(key: str, request: Request):
        parameters(request, Empty)
        return query().command(key)

    @router.post("/targets")
    def add(body: Add, request: Request, idempotency_key: str | None = Header(default=None)):
        parameters(request, Empty)
        payload = body.model_dump()
        payload["targets"] = [t.strip() for t in payload["targets"]]
        return submit("add", "", payload, idempotency_key)

    @router.patch("/targets/{key}")
    def update(key: str, body: Update, request: Request, idempotency_key: str | None = Header(default=None)):
        parameters(request, Empty)
        return submit("update", key, body.model_dump(), idempotency_key)

    @router.patch("/instances/{vmindex}")
    def update_instance(vmindex: Annotated[int, Path(ge=0)], body: InstanceUpdate, request: Request, idempotency_key: str | None = Header(default=None)):
        parameters(request, Empty)
        return submit("instance", str(vmindex), body.model_dump(), idempotency_key)

    @router.delete("/targets/{key}")
    def remove(key: str, request: Request, body: Empty | None = Body(default=None), idempotency_key: str | None = Header(default=None)):
        parameters(request, Empty)
        return submit("remove", key, {}, idempotency_key)

    @router.post("/targets/{key}/record")
    def record(key: str, request: Request, body: Empty | None = Body(default=None), idempotency_key: str | None = Header(default=None)):
        parameters(request, Empty)
        return submit("record", key, {}, idempotency_key)

    @router.post("/targets/{key}/stop")
    def stop_target(key: str, body: StopTarget, request: Request, idempotency_key: str | None = Header(default=None)):
        parameters(request, Empty)
        return submit("stop_target", key, body.model_dump(), idempotency_key)

    @router.post("/jobs/{key}/stop")
    def stop(key: str, request: Request, body: Empty | None = Body(default=None), idempotency_key: str | None = Header(default=None)):
        parameters(request, Empty)
        return submit("stop", key, {}, idempotency_key)

    @router.post("/reconcile")
    def reconcile(body: Reconcile, request: Request, idempotency_key: str | None = Header(default=None)):
        parameters(request, Empty)
        return submit("reconcile", body.job_id or "", body.model_dump(), idempotency_key)

    @router.post("/segments/{job_id}/{segment_index}/open-folder", response_model=OpenFolderResponse)
    def open_segment_folder(job_id: str, segment_index: Annotated[int, Path(ge=0)], request: Request, body: Empty | None = Body(default=None)):
        parameters(request, Empty)
        seg = query().segment(job_id, segment_index)
        file_path_raw = seg.get("file_path")
        if not file_path_raw:
            raise ControlError("segment_file_not_ready", "切片文件尚未落盘", 404)
        output_root = runtime.config.output_dir.resolve()
        file_path = FilePath(file_path_raw).resolve()
        if not file_path.is_relative_to(output_root):
            raise ControlError("path_forbidden", "切片路径不在录像输出目录内", 403)
        if file_path.is_file():
            revealed = True
        elif file_path.parent.is_dir():
            revealed = False
        else:
            raise ControlError("folder_not_found", "切片所在目录不存在", 404)
        try:
            reveal_in_file_manager(file_path if revealed else file_path.parent, is_file=revealed)
        except Exception as exc:
            # 只转换桌面启动调用的异常；查询、路径校验及进程退出信号不在此边界内。
            logger.warning("拉起宿主机文件管理器失败: %s", exc, exc_info=True)
            raise ControlError("desktop_open_failed", "无法启动录制机文件管理器，请查看服务日志", 500) from exc
        return OpenFolderResponse(status="succeeded", file_path=str(file_path), folder_path=str(file_path.parent), revealed=revealed)

    app.include_router(router)

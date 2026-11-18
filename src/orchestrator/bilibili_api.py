"""本机账号页专用路由。来源与短期凭证在解析业务请求之前验证。"""

import asyncio
import ipaddress
import re
import secrets
import time
from contextvars import ContextVar
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from .account_login_store import LoginStore
from .upload_core_client import (
    DTO,
    PREFIX,
    Accounts,
    BridgeError,
    call,
    safe_code,
    session_view,
)
from .upload_core_runtime import CoreRuntime

BASE = "/api/v1/bilibili"


class Settings(DTO):
    python: str
    data_dir: str
    engine_config: str
    port: int
    create: bool = False


class Action(DTO):
    action: Literal["create", "resume", "cancel"]
    target: str | None
    deployment: str


def failure(code, message, status):
    return JSONResponse(
        {"code": code, "message": message, "details": None},
        status_code=status,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def install_accounts(app, config, runtime=None):
    core = CoreRuntime(
        config.core_profile or config.data_dir / "upload-core" / "profile.json"
    )
    store = LoginStore(config.data_dir / "upload-core" / "account-login-bridge.sqlite3")
    router = APIRouter(prefix=BASE)
    tokens = {}
    limit = asyncio.Semaphore(12)
    requested_deployment = ContextVar("account_deployment", default=None)

    @app.middleware("http")
    async def protect(req, handler):
        if not req.url.path.startswith(BASE):
            resp = await handler(req)
            if resp.headers.get("content-type", "").startswith("text/html"):
                resp.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
            return resp
        h = req.headers
        host = h.get("host", "")
        try:
            local = ipaddress.ip_address(req.client.host).is_loopback
        except (ValueError, AttributeError):
            local = False
        hosts = {f"127.0.0.1:{config.port}", f"[::1]:{config.port}"}
        if (
            not local
            or host not in hosts
            or h.get("origin", "http://" + host) != "http://" + host
            or (
                h.get("sec-fetch-site", "same-origin") != "same-origin"
                or h.get("sec-fetch-dest", "empty") != "empty"
                or any(
                    k.startswith("x-forwarded")
                    or k in ("forwarded", "via", "x-real-ip")
                    for k in h
                )
            )
        ):
            return failure("local_only", "请在服务所在电脑打开本机控制台", 403)
        if (
            req.url.path != BASE + "/bootstrap"
            and tokens.get(h.get("x-mumu-page", ""), 0) <= time.monotonic()
        ):
            return failure("page_expired", "请重新连接本机账号服务", 401)
        if req.method == "POST":
            try:
                req.state.key = str(UUID(h["idempotency-key"]))
                if h.get("content-type", "").split(";")[0] != "application/json":
                    raise ValueError
            except (KeyError, ValueError):
                return failure("invalid_request", "请求需要 JSON 和有效操作编号", 400)
        if limit.locked():
            return failure("busy", "账号服务繁忙，请稍后重试", 503)
        async with limit:
            binding = requested_deployment.set(h.get("x-mumu-deployment"))
            try:
                resp = await handler(req)
            except BridgeError as e:
                return failure(e.code, e.message, e.status)
            except Exception:  # noqa: BLE001 - 登录边界禁止输出原始异常和请求材料
                return failure(
                    "account_service", "本机账号服务异常，请检查配置并重新连接", 503
                )
            finally:
                requested_deployment.reset(binding)
        if resp.status_code == 422:
            return failure("invalid_request", "账号请求格式错误", 422)
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    @router.get("/bootstrap")
    async def bootstrap():
        now = time.monotonic()
        for t in list(tokens):
            if tokens[t] <= now:
                del tokens[t]
        if len(tokens) >= 512:
            raise BridgeError("busy", "页面连接过多，请稍后重试", 429)
        token = secrets.token_urlsafe(32)
        tokens[token] = now + 600
        return {"token": token}

    async def status(ensure=False):
        info = await asyncio.to_thread(core.inspect, ensure)
        if info["state"] == "online":
            port = info["port"]
            health = await call(port, "/api/v1/system/health", probe=True)
            caps = (await call(port, "/api/v1/system/capabilities", probe=True)).get(
                "gui_login"
            )
            if health.get("api") != "ready" or health.get("database") != "ready":
                raise BridgeError("core_starting", "Core 数据库尚未就绪", 503)
            if (
                not isinstance(caps, dict)
                or type(caps.get("version")) is not int
                or caps["version"] != 1
                or (
                    "bilibili" not in (caps.get("platforms") or [])
                    or type(caps.get("available")) is not bool
                )
            ):
                raise BridgeError("incompatible", "Core 扫码协议不兼容")
            info["available"] = caps["available"]
            if not info["available"]:
                info["reason"] = safe_code(caps.get("reason"))
        return info

    async def bound(deployment=None):
        s = await status()
        if s["state"] != "online" or any(
            v and v != s["deployment"] for v in (deployment, requested_deployment.get())
        ):
            raise BridgeError("binding", "请先连接原部署")
        return s

    @router.get("/status")
    async def get_status():
        return await status()

    @router.post("/ensure")
    async def ensure():
        return await status(True)

    @router.post("/config")
    def configure(body: Settings):
        core.configure(body.model_dump(exclude={"create"}), body.create, store.pending)
        return {"saved": True}

    @router.get("/config")
    def settings():
        p = core.profile()
        return (
            {k: p[k] for k in ("python", "data_dir", "engine_config", "port")}
            if p
            else {}
        )

    @router.get("/accounts")
    async def accounts():
        s = await bound()
        try:
            items = Accounts.model_validate(
                await call(s["port"], "/api/v1/accounts?view=summary")
            ).items
        except ValidationError:
            raise BridgeError("protocol", "账号摘要格式不兼容", 502) from None
        return {"items": [item for item in items if item.platform == "bilibili"]}

    @router.get("/operations")
    def operations():
        profile = core.profile()
        return {"items": store.entries(profile["id"]) if profile else []}

    @router.get("/sessions")
    async def sessions():
        s = await bound()
        data = await call(s["port"], PREFIX + "?active=true")
        views = [session_view(i) for i in data["items"]]
        known = await asyncio.to_thread(store.observe, s["deployment"], views)
        present = {v["session_id"] for v in views}
        for sid in known:
            if sid not in present:
                v = session_view(await call(s["port"], f"{PREFIX}/{sid}"), sid)
                views.append(v)
                if v["outcome"]:
                    await asyncio.to_thread(store.observe, s["deployment"], [v])
                    await asyncio.to_thread(store.terminal, s["deployment"], sid)
        return {"items": views}

    @router.get("/sessions/{sid}")
    async def session(sid: UUID):
        s = await bound()
        view = session_view(await call(s["port"], f"{PREFIX}/{sid}"), str(sid))
        await asyncio.to_thread(store.observe, s["deployment"], [view])
        if view["outcome"]:
            await asyncio.to_thread(store.terminal, s["deployment"], str(sid))
        return view

    @router.get("/sessions/{sid}/qr")
    async def qr(sid: UUID, challenge_id: str):
        s = await bound()
        path = f"{PREFIX}/{sid}"
        view = session_view(await call(s["port"], path), str(sid))
        if not view["challenge"] or challenge_id != view["challenge"]["challenge_id"]:
            raise BridgeError("challenge_gone", "请重新查询原会话", 410)
        image = await call(
            s["port"], path + "/qr?challenge_id=" + challenge_id, png=True
        )
        return Response(image, media_type="image/png")

    @router.post("/operations")
    async def action(req: Request, op: Action):
        target = op.target
        if (target or op.action != "create") and not re.fullmatch(
            r"[\w-]{1,128}", target or "", re.ASCII
        ):
            raise BridgeError("invalid_target", "账号或会话标识无效", 422)
        s = await bound(op.deployment)
        path, body = PREFIX, {"platform": "bilibili", "account_id": target}
        if op.action != "create":
            path, body = f"{PREFIX}/{target}/{op.action}", {}
        key = req.state.key
        # 发送权登记后立即进入异常收尾区，避免中途取消留下本进程仍持有的发送权。
        row, retry = store.begin(op.deployment, key, op.action, target)
        if "status" in row:
            return {k: row[k] for k in ("key", "session", "status", "error")}
        try:
            if op.action == "create" and not s["available"]:
                raise BridgeError(
                    "engine_interaction_incompatible", "当前引擎暂不能创建扫码会话"
                )
            if op.action != "create" and not retry:
                current = session_view(
                    await call(s["port"], f"{PREFIX}/{target}"), target
                )
                if not current["can_" + op.action]:
                    raise BridgeError("action_unavailable", "当前会话不能执行此操作")
            view = session_view(
                await call(s["port"], path, body=body, key=key),
                target if op.action != "create" else None,
            )
            sid = view["session_id"]
            status_value = (
                "linked" if op.action == "create" and not view["outcome"] else "done"
            )
            await asyncio.to_thread(store.finish, key, status_value, sid)
            await asyncio.to_thread(store.observe, op.deployment, [view])
            if view["outcome"]:
                await asyncio.to_thread(store.terminal, op.deployment, sid)
            return {"key": key, "status": status_value, "session": sid}
        except BaseException as exc:
            code = exc.code if isinstance(exc, BridgeError) else "unknown"
            rejected = not retry and code in (
                "action_unavailable",
                "engine_interaction_incompatible",
                "maintenance_active",
                "invalid_account",
                "login_finalizing",
                "login_session_not_found",
            )
            store.finish(key, "rejected" if rejected else "unknown", error=code)
            raise

    if runtime is not None:
        from .segment_upload_api import install_segment_uploads

        install_segment_uploads(router, config, runtime, core)
    app.include_router(router)

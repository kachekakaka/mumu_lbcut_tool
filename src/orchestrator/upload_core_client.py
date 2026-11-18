"""仅向已核验的本机 Core 转发扫码协议，响应按白名单解码。"""

import asyncio
import logging
import re
from typing import Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

PREFIX = "/api/v1/account-login-sessions"
PNG_LIMIT = 128 * 1024

# httpx 默认按 INFO 输出含查询参数的完整 URL。协议错误由桥接层返回安全错误码。
for _name in ("httpx", "httpcore"):
    _logger = logging.getLogger(_name)
    _logger.addHandler(logging.NullHandler())
    _logger.propagate = False


class BridgeError(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status = code, message, status


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Account(DTO):
    id: str
    reference: str
    platform: str
    remote_user_id: str | None
    display_name: str
    enabled: bool
    availability: Literal["ready", "disabled", "blocked"]
    block_reasons: list[str]


class Accounts(DTO):
    items: list[Account]


class Challenge(DTO):
    challenge_id: str
    image_path: str
    deadline_at: str
    expiry_source: Literal["local"] = "local"
    expires_at: None = None


class LoginError(DTO):
    code: str
    message: str


class Session(DTO):
    session_id: str
    operation_id: str
    account_id: str | None
    platform: Literal["bilibili"] = "bilibili"
    revision: int
    phase: Literal[
        "queued",
        "waiting_for_executions",
        "starting",
        "waiting_confirmation",
        "verifying_identity",
        "waiting_for_account",
        "finalizing",
        "cancelling",
        "recovering",
        "finished",
    ]
    outcome: Literal["succeeded", "cancelled", "expired", "failed"] | None
    created_at: str
    updated_at: str
    can_cancel: bool
    can_resume: bool
    can_retry: bool
    challenge: Challenge | None
    account_summary: Account | None
    error: LoginError | None


def session_view(data, expected=None):
    try:
        s = Session.model_validate(data)
        sid = str(UUID(s.session_id))
        if sid != s.session_id or expected and sid != expected:
            raise ValueError
        view = s.model_dump()
        if s.challenge:
            cid = s.challenge.challenge_id
            path = f"{PREFIX}/{sid}/qr?challenge_id={cid}"
            if (
                not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", cid)
                or s.challenge.image_path != path
            ):
                raise ValueError
            del view["challenge"]["image_path"]
        if s.error:
            view["error"] = {"code": safe_code(s.error.code)}
        return view
    except (ValidationError, ValueError):
        raise BridgeError("protocol", "会话响应与扫码协议不符", 502) from None


def safe_code(code):
    return (
        code
        if isinstance(code, str) and re.fullmatch("[a-z][a-z0-9_]{0,63}", code)
        else "core_error"
    )


async def call(port, path, *, body=None, key=None, png=False, probe=False):
    return await _call(port, path, body=body, key=key, png=png, probe=probe)


async def _call(port, path, *, body=None, key=None, png=False, probe=False, posting=False):
    headers = {
        "X-Upload-Platform-Client": "cli-v1" if posting else "gui-bridge-v1",
        "Accept-Encoding": "identity",
    }
    if key:
        headers["Idempotency-Key"] = key
    deadline = 3 if probe else 10
    try:
        async with (
            asyncio.timeout(deadline),
            httpx.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(deadline, connect=2),
            ) as client,
            client.stream(
                "POST" if body is not None else "GET",
                f"http://127.0.0.1:{port}" + path,
                json=body,
                headers=headers,
            ) as response,
        ):
            good = response.status_code in ((200, 201) if posting else (200, 202))
            image = png and good
            limit = PNG_LIMIT if image else 1024 * 1024
            mime = response.headers.get("content-type", "").split(";")[0]
            if (
                response.is_redirect
                or response.headers.get("content-encoding", "identity") != "identity"
            ):
                raise ValueError
            if (
                mime != ("image/png" if image else "application/json")
                or int(response.headers.get("content-length", 0)) > limit
            ):
                raise ValueError
            buf = bytearray()
            async for chunk in response.aiter_raw():
                if len(buf) + len(chunk) > limit:
                    raise ValueError
                buf.extend(chunk)
            if image:
                if not buf.startswith(bytes.fromhex("89504e470d0a1a0a")):
                    raise ValueError
                return bytes(buf)
            import json

            data = json.loads(buf)
            if not isinstance(data, dict):
                raise TypeError
            if not good:
                raw = data.get("code")
                code = safe_code(raw.lower() if posting and isinstance(raw, str) else raw)
                raise BridgeError(code, "Core 未完成本次请求", response.status_code)
            return data
    except (TimeoutError, httpx.HTTPError):
        raise BridgeError(
            "core_unreachable", "Core 连接中断，请检查原服务", 503
        ) from None
    except (TypeError, ValueError, RecursionError):
        raise BridgeError("protocol", "Core 返回了无法验证的响应", 502) from None

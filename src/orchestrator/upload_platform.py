"""上传平台的 HTTP 契约。部署标识须随原数据库保留。"""

import asyncio
import json
import re
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict


ORIGIN = "http://127.0.0.1:8765"
API = "/api/v1"
DEFINITE_REJECTIONS = {
    "invalid_request", "invalid_manifest", "invalid_path", "invalid_metadata",
    "invalid_account", "duplicate_target", "unsupported_option", "engine_not_configured",
    "engine_incompatible", "work_id_mismatch", "no_targets", "b18_not_permitted",
    "version_conflict", "preview_expired",
}
MESSAGES = {
    "submission_not_found": "暂未查到原请求的受理回执",
    "version_conflict": "作品 YAML 已变化，请重新预览",
    "preview_expired": "平台预览已失效，请重新预览",
    "invalid_path": "上传平台拒绝了配置或录像目录",
    "invalid_manifest": "上传平台拒绝了作品 YAML",
    "invalid_metadata": "上传平台拒绝了投稿字段，请检查分区、标题和标签",
    "invalid_account": "投稿账号不可用或 MID 已变化",
    "unsupported_option": "当前平台部署不支持所选投稿参数或可见范围",
    "engine_not_configured": "上传平台尚未配置投稿引擎",
}


class BridgeError(Exception):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def protocol():
    return BridgeError("protocol", "上传平台回执无法核对", 502)


def identifier(value):
    try:
        if str(UUID(value)) == value:
            return value
    except (ValueError, AttributeError, TypeError):
        pass
    raise protocol()


class UploadPlatform:
    def __init__(self, *, transport=None):
        self.transport = transport

    async def request(self, path, *, body=None, key=None, deployment=None, html=False):
        headers = {"Accept-Encoding": "identity"}
        if path.startswith(API) and not path.startswith(API + "/submissions"):
            headers["X-Upload-Platform-Client"] = "cli-v1"
        if key:
            headers["Idempotency-Key"] = key
        if deployment:
            headers["X-Upload-Platform-Deployment"] = deployment
        try:
            async with (
                asyncio.timeout(8),
                httpx.AsyncClient(transport=self.transport, trust_env=False, follow_redirects=False,
                                  timeout=httpx.Timeout(8, connect=2)) as client,
                client.stream("GET" if body is None else "POST", ORIGIN + path,
                              json=body, headers=headers) as response,
            ):
                mime = response.headers.get("content-type", "").split(";")[0]
                if html:
                    if response.status_code != 200 or mime != "text/html":
                        raise BridgeError("panel_unavailable", "上传平台页面暂不可用", 503)
                    return None
                if mime != "application/json" or response.headers.get("content-encoding", "identity") != "identity":
                    raise protocol()
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 2 * 1024 * 1024:
                        raise protocol()
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise protocol()
                reported = response.headers.get("X-Upload-Platform-Deployment")
                if deployment and reported and reported != deployment:
                    raise BridgeError("binding", "上传平台部署已切换，请核对原单")
                if response.status_code not in (200, 201):
                    code = data.get("code")
                    code = code.lower() if isinstance(code, str) and re.fullmatch(r"[a-zA-Z0-9_]{1,64}", code) else "platform_error"
                    raise BridgeError(code, MESSAGES.get(code, "上传平台未完成本次请求"), response.status_code)
                return data
        except (httpx.HTTPError, TimeoutError):
            raise BridgeError("platform_offline", "无法连接录制机上的上传平台", 503) from None
        except (ValueError, RecursionError):
            raise protocol() from None

    async def binding(self, expected=None):
        data = await self.request(API + "/system/health")
        deployment = data.get("deployment_id")
        if not isinstance(deployment, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", deployment):
            raise BridgeError("binding", "上传平台未提供可核对的部署标识", 503)
        if expected is not None and expected != deployment:
            raise BridgeError("binding", "请连接这笔安排的原上传平台部署")
        if data.get("api") != "ready" or data.get("database") != "ready":
            raise BridgeError("platform_offline", "上传平台尚未就绪", 503)
        return deployment

    async def bound_request(self, deployment, path, **kwargs):
        await self.binding(deployment)
        try:
            return await self.request(API + path, deployment=deployment, **kwargs)
        finally:
            await self.binding(deployment)

    async def accounts(self, deployment):
        data = await self.bound_request(deployment, "/accounts?view=summary")
        items = data.get("items")
        if not isinstance(items, list) or any(not isinstance(a, dict) for a in items):
            raise protocol()
        return [
            {k: a[k] for k in ("id", "remote_user_id", "display_name")}
            for a in items if a.get("platform") == "bilibili" and a.get("enabled") is True
            and a.get("availability") == "ready" and isinstance(a.get("id"), str)
            and isinstance(a.get("display_name"), str) and isinstance(a.get("remote_user_id"), str)
            and re.fullmatch(r"[1-9][0-9]{0,31}", a["remote_user_id"])
        ]

    async def check_account(self, deployment, expected):
        for account in await self.accounts(deployment):
            if (account["id"], account["remote_user_id"]) == (expected["id"], expected["remote_user_id"]):
                return account
        raise BridgeError("invalid_account", MESSAGES["invalid_account"])

    async def preview(self, deployment, paths):
        return await self.bound_request(deployment, "/submissions/preview", body=paths)

    async def submit(self, row):
        return await self.bound_request(row["deployment"], "/submissions",
                                        body=json.loads(row["request"]), key=row["request_key"])

    async def result(self, row):
        try:
            return await self.bound_request(row["deployment"], "/submissions/result", key=row["request_key"])
        except BridgeError as exc:
            if exc.status == 404 and exc.code == "submission_not_found":
                return None
            raise

    async def task(self, deployment, task_id):
        return await self.bound_request(deployment, f"/tasks/{identifier(task_id)}")

    async def panel(self, route):
        await self.request("/embed/" + route, html=True)

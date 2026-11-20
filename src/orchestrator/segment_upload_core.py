"""切片投递专用 Core 客户端。路由和目标地址由后端固定。"""

import asyncio
import json
from pathlib import Path
from urllib.parse import urlencode
from uuid import UUID

from pydantic import ValidationError

from .upload_core_client import Accounts, BridgeError, _call


def protocol():
    return BridgeError("protocol", "Core 返回了无法核验的数据", 502)


def identifier(value):
    try:
        parsed = str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise protocol() from None
    if parsed != value:
        raise protocol()
    return parsed


def posting_capabilities(config):
    try:
        with Path(config["engine_config"]).open(encoding="utf-8") as stream:
            engine = json.load(stream)
        if engine.get("b18_allowlist_file") is not None:
            raise BridgeError("b18_deployment", "该部署使用 B18 专用名额，不能安排普通投稿")
        allow_self_only = engine.get("allow_self_only", False)
        with Path(config["engine"]["capabilities_file"]).open(encoding="utf-8") as stream:
            caps = json.loads(stream.read(65537))
        verified_modes = caps.get("verified_publish_modes")
        if (
            type(caps.get("protocol_version")) is not int
            or caps["protocol_version"] != 1
            or caps["engine_build_id"] != config["engine"]["build_id"]
            or caps["submit_api"] != "web"
            or caps.get("upload_ready") is not True
            or not isinstance(verified_modes, list)
            or type(allow_self_only) is not bool
        ):
            raise ValueError
        modes = ["public"] if "public" in verified_modes else []
        if caps.get("supports_self_only") is True and (allow_self_only or "self_only" in verified_modes):
            modes.append("self_only")
        names = ("title", "part_title", "description", "tag", "tag_count", "part_count")
        limits = {name: caps["metadata_limits"][name] for name in names}
        if not modes or not all(type(n) is int and n > 0 for n in limits.values()):
            raise ValueError
        return modes, limits
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise BridgeError("posting_unavailable", "原部署尚未提供可用的普通投稿能力") from None


class PostingCore:
    def __init__(self, runtime):
        self.runtime = runtime

    async def binding(self, deployment=None):
        info = await asyncio.to_thread(self.runtime.inspect, posting=True)
        if deployment is not None and info.get("deployment") != deployment:
            raise BridgeError("binding", "部署已切换，请连接原部署或重新打开弹窗")
        if info["state"] != "online":
            raise BridgeError("core_offline", "请在本机账号页连接原 Core 部署", 503)
        return info

    async def verify(self, binding):
        if await self.binding(binding["deployment"]) != binding:
            raise BridgeError("binding", "Core 部署连接已变化，请重新核对")

    async def _request(self, binding, path, *, body=None, key=None, probe=False):
        await self.verify(binding)
        try:
            result = await _call(binding["port"], "/api/v1" + path, body=body, key=key,
                                 posting=True, probe=probe)
        except BridgeError:
            await self.verify(binding)
            raise
        await self.verify(binding)
        return result

    async def health(self, binding):
        health = await self._request(binding, "/system/health", probe=True)
        if health.get("api") != "ready" or health.get("database") != "ready":
            raise BridgeError("core_starting", "Core 数据库尚未就绪", 503)
        return any(isinstance(w, dict) and w.get("active") is True for w in health.get("workers", []))

    async def capabilities(self, binding):
        return await asyncio.to_thread(posting_capabilities, binding["config"])

    async def accounts(self, binding):
        try:
            items = Accounts.model_validate(await self._request(binding, "/accounts?view=summary")).items
        except ValidationError:
            raise protocol() from None
        return [a for a in items if a.platform == "bilibili" and a.enabled and a.availability == "ready"]

    async def register(self, binding, body, key):
        return await self._request(binding, "/works", body=body, key=key)

    async def submit(self, binding, work, body, key):
        return await self._request(binding, f"/works/{identifier(work)}/submit", body=body, key=key)

    async def work_tasks(self, binding, work, cursor):
        query = urlencode({"limit": 50} | ({"cursor": identifier(cursor)} if cursor else {}))
        return await self._request(binding, f"/works/{identifier(work)}/tasks?{query}")

    async def task(self, binding, task):
        return await self._request(binding, f"/tasks/{identifier(task)}")

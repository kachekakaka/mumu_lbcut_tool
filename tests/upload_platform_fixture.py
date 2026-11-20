"""模拟统一提交，而录制、文件及 MuMu HTTP 路由使用实际代码。"""

import json
from pathlib import Path
from uuid import uuid4

import httpx
import yaml

from src.orchestrator.upload_manifest import version
from src.orchestrator.upload_platform import UploadPlatform


ACCOUNT = {"id": str(uuid4()), "remote_user_id": "42", "display_name": "测试账号"}


class Platform:
    def __init__(self):
        self.deployment = "A"
        self.account = ACCOUNT.copy()
        self.available = True
        self.private = True
        self.previews = {}
        self.tasks = {}
        self.trace = []
        self.on_submit = None
        self.after_accept = None
        self.client = UploadPlatform(transport=httpx.MockTransport(self.handle))

    @staticmethod
    def error(code, status):
        return httpx.Response(status, json={"code": code})

    def read(self, paths):
        path = Path(paths["manifest_path"])
        root = Path(paths["asset_root"])
        if (path.resolve() != path or not path.is_relative_to(root / "sessions")
            or path.name != "upload.yaml"):
            raise ValueError
        content = path.read_bytes()
        manifest = yaml.safe_load(content)
        for video in manifest["assets"]["videos"]:
            candidate = (root / video["path"]).resolve(strict=True)
            if not candidate.is_relative_to(root) or not candidate.is_file():
                raise ValueError
        return manifest, version(content)

    async def handle(self, request):
        if not self.available:
            raise httpx.ConnectError("模拟平台离线", request=request)
        path = request.url.path
        key = request.headers.get("idempotency-key")
        if path.startswith("/embed/"):
            return httpx.Response(200, text="模拟内嵌页", headers={"content-type": "text/html"})
        if path == "/api/v1/system/health":
            return httpx.Response(200, json={"api": "ready", "database": "ready", "deployment_id": self.deployment})
        if request.headers.get("X-Upload-Platform-Deployment") != self.deployment:
            return self.error("binding", 409)
        if path == "/api/v1/accounts":
            return httpx.Response(200, json={"items": [self.account | {
                "platform": "bilibili", "enabled": True, "availability": "ready",
            }]})
        if path.startswith("/api/v1/tasks/"):
            task = next(r for _, r in self.tasks.values() if r["task_ids"][0] == path.rsplit("/", 1)[1])
            return httpx.Response(200, json={"id": task["task_ids"][0], "work_id": task["work_id"], "status": "QUEUED"})
        if path.endswith("/result"):
            self.trace.append(("GET", key, None))
            return httpx.Response(200, json=self.tasks[key][1]) if key in self.tasks else self.error("submission_not_found", 404)
        body = json.loads(request.content)
        if path.endswith("/preview"):
            self.trace.append(("PREVIEW", None, body))
            expected = None
        else:
            assert path == "/api/v1/submissions"
            self.trace.append(("POST", key, body))
            if key in self.tasks:
                old, receipt = self.tasks[key]
                return httpx.Response(201, json=receipt) if body == old else self.error("idempotency_conflict", 409)
            if self.on_submit:
                await self.on_submit(request)
            expected = body.get("expected_version")
            if "preview_id" in body:
                saved = self.previews.get(body["preview_id"])
                if not saved:
                    return self.error("preview_expired", 409)
                paths, expected = saved
            else:
                paths = body
        try:
            manifest, revision = self.read(body if expected is None else paths)
        except (ValueError, OSError):
            return self.error("invalid_path", 422)
        if expected is not None and expected != revision:
            return self.error("version_conflict", 409)
        options = manifest["upload_units"][0]["targets"][0]["options"]
        if options["category_id"] != 171:
            return self.error("invalid_metadata", 422)
        if not self.private and options["publish_mode"] == "self_only":
            return self.error("unsupported_option", 422)
        work_id = manifest["work"]["id"]
        if path.endswith("/preview"):
            preview_id = str(uuid4())
            self.previews[preview_id] = (body, revision)
            return httpx.Response(200, json={"preview_id": preview_id, "work_id": work_id, "config_version": revision,
                                            "expires_at": "2026-11-20T14:00:00Z", "warnings": [], "manifest": manifest})
        receipt = {"work_id": work_id, "task_ids": [str(uuid4())], "status": "accepted", "config_version": revision}
        self.tasks[key] = body, receipt
        if self.after_accept:
            await self.after_accept(request)
        return httpx.Response(201, json=receipt)

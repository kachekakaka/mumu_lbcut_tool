"""切片安排、原单恢复和安全状态投影。"""

import asyncio
import json
import os
from pathlib import Path
import re
import stat
import time
from uuid import uuid4

from .segment_upload_core import identifier, protocol
from .segment_upload_store import encode, revisable
from .storage import ControlError
from .upload_core_client import BridgeError


DEFINITE_REJECTIONS = {
    "invalid_request", "invalid_metadata", "invalid_manifest", "invalid_path",
    "invalid_account", "duplicate_target", "unsupported_option", "engine_not_configured",
    "engine_incompatible", "work_id_mismatch", "no_targets", "b18_not_permitted",
}
FINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"}


def file_snapshot(path, root):
    try:
        resolved = Path(path).resolve(strict=True)
        if (
            not Path(path).is_absolute() or resolved.drive.startswith("\\\\")
            or resolved != Path(os.path.normpath(path))
            or not resolved.is_relative_to(root.resolve())
            or resolved.suffix.lower() != ".mp4"
            or not resolved.is_file()
        ):
            raise ValueError
        with resolved.open("rb") as handle:
            info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
            raise ValueError
        return {"path": str(resolved), "size": info.st_size, "mtime_ns": info.st_mtime_ns}
    except (OSError, ValueError, RuntimeError, TypeError):
        raise BridgeError("segment_file_unavailable", "正式录像不存在、不可读或不在录像输出目录内") from None


def form_errors(values, modes, limits, filename):
    if values["publish_mode"] not in modes:
        return "当前部署不支持所选发布范围"
    if (values["copyright"] == 2) != bool(values["source"]):
        return "转载需填写来源，自制请留空"
    if not values["title"] or len(values["title"]) > limits["title"]:
        return "请填写符合部署字数限制的标题"
    if len(Path(filename).stem) > limits["part_title"] or len(values["description"]) > limits["description"]:
        return "分 P 标题或简介超出部署字数限制"
    tags = values["tags"]
    if not 1 <= len(tags) <= limits["tag_count"] or any(
        not tag or "," in tag or len(tag) > limits["tag"] for tag in tags
    ):
        return "请填写符合部署数量和字数限制的标签"
    return None


def freeze_requests(frozen, work_id):
    params, path = frozen["parameters"], Path(frozen["file"]["path"])
    registration = {
        "directory": str(path.parent),
        "manifest": {
            "version": 1,
            "work": {"id": work_id, "ready": True,
                     **{k: params[k] for k in ("title", "description", "tags")}},
            "assets": {"videos": [{"id": "video", "path": path.name}]},
            "upload_units": [{
                "id": "segment", "mode": "independent", "videos": ["video"],
                "targets": [{
                    "platform": "bilibili", "account": frozen["account"]["id"], "enabled": True,
                    "options": {k: params[k] for k in ("category_id", "copyright", "source", "publish_mode")},
                }],
            }],
        },
    }
    submit = {**registration, "execution_type": "immediate", "retry_policy": {
        "max_attempts": 3, "retry_delays_seconds": [30, 120],
    }}
    return {**frozen, "registration": registration, "submission": submit}


def matches(row, snapshot):
    frozen = json.loads(row["frozen"])
    file = Path(frozen["file"]["path"])
    manifest = frozen["registration"]["manifest"]
    unit = manifest["upload_units"][0]
    video = unit["videos"][0]
    return (
        snapshot.get("unit_id") == unit["id"] and snapshot.get("item_key") == video
        and snapshot.get("mode") == unit["mode"]
        and snapshot["target"]["account_id"] == row["account_id"]
        and snapshot["target"]["platform"] == "bilibili"
        and snapshot["target"].get("expected_mid") == frozen["account"]["remote_user_id"]
        and [p["video_id"] for p in snapshot["parts"]] == [video]
        and snapshot["assets"] == [
            {"asset_id": video, "role": "video", "video_id": video, "relative_path": file.name}
        ]
        and isinstance(dir_value := snapshot.get("directory"), str)
        and os.path.normcase(dir_value) == os.path.normcase(str(file.parent))
        and "b18_run_id" not in snapshot
    )


def task_matches(row, task):
    if task["work_id"] != row["work_id"]:
        raise protocol()
    task_id = identifier(task["id"])
    return task_id if task["account_id"] == row["account_id"] and matches(row, task["snapshot"]) else None


def observation(task, worker):
    status = task["status"]
    if not isinstance(status, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,47}", status):
        raise protocol()
    result = {"status": status, "worker_online": worker}
    attempt = task.get("attempt")
    if isinstance(attempt, dict) and status in {"RETRY_WAIT", "RECOVERY_PENDING", "UNCERTAIN", "FAILED", "CANCELLED"}:
        reason = attempt.get("error")
        if isinstance(reason, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", reason):
            result["reason"] = reason
    receipt = task.get("receipt")
    if isinstance(receipt, dict) and isinstance(evidence := receipt.get("evidence"), dict):
        bvid, aid = evidence.get("bvid"), evidence.get("aid")
        if isinstance(bvid, str) and re.fullmatch(r"BV[A-Za-z0-9]{1,30}", bvid):
            result["bvid"] = bvid
        if type(aid) is int and aid > 0:
            result["aid"] = aid
    return result


class SegmentUploads:
    def __init__(self, store, core, segment, output_dir, *, writable=lambda: True):
        self.store, self.core, self.segment, self.output_dir = store, core, segment, output_dir
        self.writable = writable
        self.dialogs = {}
        self.poll_limit = asyncio.Semaphore(4)

    def current_segment(self, job, index):
        try:
            segment = self.segment(job, index)
        except ControlError as exc:
            raise BridgeError(exc.code, str(exc), exc.status) from None
        if segment["status"] != "finished" or segment["started_at"] is None:
            raise BridgeError("segment_unfinished", "只有已开始且已封口的正式切片可安排")
        current = {
            "file": file_snapshot(segment["file_path"], self.output_dir),
            "quality_flags": segment["quality_flags"],
        }
        return segment, current

    def public(self, row, *, detail=False):
        state = json.loads(row["observed"]) if row["observed"] else {}
        value = {k: row[k] for k in (
            "id", "job_id", "segment_index", "stage", "revision", "task_id", "observed_at", "problem",
        )} | {
            "status": state.get("status"), "worker_online": state.get("worker_online"),
            "rejected": bool(row["rejected"]), "can_revise": bool(revisable(row, self.store.clock())),
            "can_continue": row["stage"] != "task" and not row["rejected"] and row["lease_until"] <= self.store.clock(),
        }
        if detail:
            frozen = json.loads(row["frozen"])
            value.update({k: state[k] for k in ("bvid", "aid", "reason") if k in state},
                         deployment=row["deployment"], work_id=row["work_id"],
                         account=frozen["account"], parameters=frozen["parameters"],
                         filename=Path(frozen["file"]["path"]).name)
        return value

    def require_writable(self):
        if not self.writable():
            raise BridgeError("upload_paused", "控制台正在关闭，仍可查看已保存安排", 503)

    async def context(self, job, index, revise=None):
        self.require_writable()
        segment, source = self.current_segment(job, index)
        binding = await self.core.binding()
        modes, limits = await self.core.capabilities(binding)
        worker = await self.core.health(binding)
        accounts = await self.core.accounts(binding)
        present = self.store.for_segments([(job, index)])
        rows = {r["account_id"]: r for r in present if r["deployment"] == binding["deployment"]}
        if revise:
            row = self.store.get(revise)
            if (row["job_id"], row["segment_index"], row["deployment"]) != (job, index, binding["deployment"]):
                raise BridgeError("binding", "请连接这条安排的原部署后再修订")
            if not revisable(row, self.store.clock()):
                raise BridgeError("revision_forbidden", "原请求结果仍待确认，暂不能修订")
            accounts = [a for a in accounts if a.id == row["account_id"]]
        else:
            accounts = [a for a in accounts if a.id not in rows]
        now = time.monotonic()
        self.dialogs = {key: ctx for key, ctx in self.dialogs.items() if ctx["expires"] > now}
        if len(self.dialogs) >= 256:
            raise BridgeError("busy", "打开的确认页过多，请稍后重试", 429)
        ticket, filename = str(uuid4()), Path(source["file"]["path"]).name
        allowed = {a.id: {"id": a.id, "remote_user_id": a.remote_user_id, "display_name": a.display_name}
                   for a in accounts}
        self.dialogs[ticket] = dict(
            expires=now + 600, job=job, index=index, binding=binding, source=source,
            allowed=allowed, revisions={a: rows[a]["revision"] if a in rows else 0 for a in allowed},
            revise=revise,
        )
        defaults = (json.loads(row["frozen"])["parameters"] if revise else {
            "title": Path(filename).stem[:limits["title"]], "description": "", "tags": [],
        })
        return {
            "ticket": ticket, "deployment": binding["deployment"], "port": binding["port"],
            "accounts": list(allowed.values()),
            "worker_online": worker, "publish_modes": modes, "limits": limits,
            "nickname": segment["nickname"] or segment["target_raw"],
            "filename": filename, "duration_sec": segment["duration_sec"],
            "quality_flags": source["quality_flags"], "defaults": defaults,
        }

    async def confirm(self, key, ticket, account_id, parameters, quality_confirmed):
        self.require_writable()
        ctx = self.dialogs.get(ticket)
        if not ctx or ctx["expires"] <= time.monotonic():
            raise BridgeError("stale_dialog", "确认页已失效，请重新打开弹窗核对")
        binding = ctx["binding"]
        await self.core.verify(binding)
        account = ctx["allowed"].get(account_id)
        if account is None:
            raise BridgeError("invalid_account", "请重新打开弹窗选择可用账号", 422)
        expected = ctx["revisions"][account_id]
        job, index, revise = ctx["job"], ctx["index"], ctx["revise"]
        source = ctx["source"]
        existing = next((r for r in self.store.for_segments([(job, index)])
                         if r["deployment"] == binding["deployment"] and r["account_id"] == account_id), None)
        if not existing or (revise and existing["revision"] == expected):
            modes, limits = await self.core.capabilities(binding)
            if problem := form_errors(parameters, modes, limits, Path(source["file"]["path"]).name):
                raise BridgeError("invalid_submission", problem, 422)
            accounts = await self.core.accounts(binding)
            if not any(a.id == account_id and a.remote_user_id == account["remote_user_id"] for a in accounts):
                raise BridgeError("invalid_account", "账号已停用、阻塞或身份变化，请重新核对")
            _, current = self.current_segment(job, index)
            if current != source or source["quality_flags"] and not quality_confirmed:
                raise BridgeError("segment_changed", "录像或质量提示需重新核对后确认")
        await self.core.verify(binding)
        self.require_writable()
        frozen = {**source, "parameters": parameters, "account": account}
        row, dispatch = self.store.confirm(
            key, job, index, binding["deployment"], frozen, expected, revise, freeze_requests,
        )
        if dispatch:
            return await self.drive(row["id"], row["revision"], binding)
        return self.public(row, detail=True)

    async def lookup(self, binding, row):
        cursor, found = None, set()
        conflict = False
        unit = json.loads(row["frozen"])["registration"]["manifest"]["upload_units"][0]
        while True:
            page = await self.core.work_tasks(binding, row["work_id"], cursor)
            items, next_cursor = page["items"], page["next_cursor"]
            if not isinstance(items, list):
                raise protocol()
            for task in items:
                if task_id := task_matches(row, task):
                    if task_id in found:
                        raise protocol()
                    found.add(task_id)
                else:
                    snapshot = task["snapshot"]
                    if (
                        task["account_id"] == row["account_id"]
                        and snapshot.get("unit_id") == unit["id"]
                        and snapshot.get("item_key") == unit["videos"][0]
                        and snapshot.get("mode") == unit["mode"]
                    ):
                        conflict = True
            if next_cursor is None:
                break
            if (cursor and identifier(next_cursor) <= cursor) or not items or next_cursor != items[-1]["id"]:
                raise protocol()
            cursor = identifier(next_cursor)
        if conflict:
            raise BridgeError("task_mismatch", "作品下有身份冲突的任务")
        if len(found) > 1:
            raise BridgeError("task_ambiguous", "作品下有多条匹配任务，请核对原单")
        return next(iter(found), None)

    def link(self, row, task_id):
        return self.store.finish(row, stage="task", task_id=task_id, attempts=0,
                                 lease_until=0, problem=None, next_check=0)

    async def drive(self, record_id, revision, binding=None):
        self.require_writable()
        while row := self.store.claim(record_id, dispatch=True, revision=revision, force=True):
            if row["stage"] == "task":
                await self.refresh_claim(row, binding)
                break
            sent = False
            try:
                async with asyncio.timeout(14):
                    original = binding or await self.core.binding(row["deployment"])
                    if row["stage"] == "submit" and row["attempts"]:
                        if task_id := await self.lookup(original, row):
                            if self.link(row, task_id):
                                continue
                            break
                    frozen = json.loads(row["frozen"])
                    _, current = self.current_segment(row["job_id"], row["segment_index"])
                    if current != {k: frozen[k] for k in ("file", "quality_flags")}:
                        raise BridgeError("segment_changed", "录像或质量提示已变化，请核对原安排")
                    modes, limits = await self.core.capabilities(original)
                    if problem := form_errors(
                        frozen["parameters"], modes, limits, Path(frozen["file"]["path"]).name,
                    ):
                        raise BridgeError("invalid_submission", problem, 422)
                    accounts = await self.core.accounts(original)
                    if not any(a.id == row["account_id"] and a.remote_user_id == frozen["account"]["remote_user_id"] for a in accounts):
                        raise BridgeError("invalid_account", "原账号目前不可用，请核对原部署")
                    self.require_writable()
                    sent = self.store.mark_sent(row)
                    if not sent:
                        break
                    if row["stage"] == "register":
                        result = await self.core.register(original, frozen["registration"], row["register_key"])
                        if result["work_id"] != row["work_id"]:
                            raise protocol()
                        if self.store.finish(row, stage="submit", attempts=0, lease_until=0, problem=None):
                            continue
                    else:
                        result = await self.core.submit(original, row["work_id"], frozen["submission"], row["submit_key"])
                        if result["work_id"] != row["work_id"] or len(result["tasks"]) != 1:
                            raise protocol()
                        task = result["tasks"][0]
                        if type(task["created"]) is not bool or not matches(row, task["snapshot"]):
                            raise protocol()
                        if self.link(row, identifier(task["task_id"])):
                            continue
            except (BridgeError, TimeoutError, KeyError, TypeError, AttributeError) as exc:
                code = exc.code if isinstance(exc, BridgeError) else "core_timeout" if isinstance(exc, TimeoutError) else "protocol"
                rejected = (
                    sent and row["attempts"] == 0 and isinstance(exc, BridgeError)
                    and exc.status in (400, 409, 422) and code in DEFINITE_REJECTIONS
                )
                self.store.finish(row, problem=code, rejected=int(rejected), next_check=self.store.clock() + 5)
            finally:
                self.store.release(row)
            break
        return self.public(self.store.get(record_id), detail=True)

    async def refresh_claim(self, row, binding=None):
        try:
            async with asyncio.timeout(12):
                original = binding or await self.core.binding(row["deployment"])
                if row["stage"] != "task":
                    task_id = await self.lookup(original, row)
                    if task_id is None:
                        self.store.finish(row, next_check=self.store.clock() + 10)
                        return
                    if self.link(row, task_id):
                        await self.refresh(row["id"], force=True)
                    return
                task = await self.core.task(original, row["task_id"])
                if task_matches(row, task) != row["task_id"]:
                    raise protocol()
                worker = None
                if task.get("status") == "QUEUED":
                    try:
                        worker = await self.core.health(original)
                    except BridgeError as exc:
                        if exc.code == "binding":
                            raise
                value = observation(task, worker)
                current = json.loads(row["observed"]) if row["observed"] else {}
                version = task["version"]
                if type(version) is not int or version < current.get("version", 0):
                    raise protocol()
                now = self.store.clock()
                next_check = float("inf") if value["status"] in FINAL else now + 4
                self.store.finish(row, observed=encode(value | {"version": version}), observed_at=now,
                                  problem=None, next_check=next_check)
        except (BridgeError, TimeoutError, KeyError, TypeError, AttributeError) as exc:
            code = exc.code if isinstance(exc, BridgeError) else "core_timeout" if isinstance(exc, TimeoutError) else "protocol"
            self.store.finish(row, problem=code, next_check=self.store.clock() + 15)
        finally:
            self.store.release(row)

    async def refresh(self, record_id, *, force=False):
        if row := self.store.claim(record_id, dispatch=False, force=force):
            await self.refresh_claim(row)
        return self.store.get(record_id)

    async def summaries(self, segments):
        rows = self.store.for_segments(segments)
        now = self.store.clock()
        due = [row for row in rows if row["lease_until"] <= now and row["next_check"] <= now
               and not row["rejected"] and (row["stage"] == "task" or row["stage"] == "submit" and row["attempts"])]

        async def poll(row):
            async with self.poll_limit:
                return await self.refresh(row["id"])

        updated = await asyncio.gather(*(poll(row) for row in sorted(due, key=lambda r: r["next_check"])[:4]))
        by_id = {r["id"]: r for r in updated}
        return [self.public(by_id.get(r["id"], r)) for r in rows]

"""有界外网查询进程。阻塞 IPC 只在自有线程中执行，独立监控负责期限和回收。"""
from concurrent.futures import Future
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from src.mvp.room_resolver import ResolvedRoom, RoomResolver, RoomStatus
from src.mvp.windows_job import WindowsJob, close_process_handle

MAX_MESSAGE = 65536
CAPACITY = {"scan": 5, "verify": 1, "manual": 2, "login": 1}


@dataclass
class QueryResult:
    room: ResolvedRoom
    error: str | None = None


class Slot:
    def __init__(self, source):
        self.source = source
        self.proc = None
        self.thread = None
        self.future = None
        self.deadline = 0.0
        self.expired_at = None
        self.raw = None


class QueryProcesses:
    def __init__(self, timeout=30.0, reclaim_timeout=5.0, child_command=None):
        self.timeout, self.reclaim_timeout = timeout, reclaim_timeout
        self.command = child_command or [sys.executable, "-m", "src.orchestrator.room_query_process"]
        self.job = WindowsJob()
        self.slots = [Slot(source) for source, count in CAPACITY.items() for _ in range(count)]
        self.lock = threading.RLock()
        self.closing = threading.Event()
        self.close_lock = threading.Lock()
        self.close_deadline = None
        self.fatal_error = None
        self.monitor = threading.Thread(target=self._monitor, name="room-query-monitor", daemon=True)
        self.monitor.start()

    def available(self, source):
        with self.lock:
            return not self.closing.is_set() and not self.fatal_error and any(
                s.source == source and s.future is None for s in self.slots)

    def submit(self, source, raw):
        with self.lock:
            if not self.available(source):
                return None
            slot = next(s for s in self.slots if s.source == source and s.future is None)
            future = slot.future = Future()
            slot.raw = raw
            slot.deadline, slot.expired_at = time.monotonic() + self.timeout, None
            slot.thread = threading.Thread(target=self._exchange, args=(slot, raw), name="room-query-ipc", daemon=True)
            slot.thread.start()
            return future

    def _kill(self, slot):
        proc = slot.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                if proc.poll() is None:
                    raise

    def _exchange(self, slot, raw):
        result = QueryResult(ResolvedRoom(raw, RoomStatus.UNKNOWN), "live_query_failed")
        broken = False
        try:
            if slot.proc is None:
                proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, cwd=Path(__file__).resolve().parents[2],
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                with self.lock:
                    slot.proc = proc
                    try:
                        self.job.assign(proc)
                    except Exception as exc:
                        self.fatal_error = f"查询进程 Job Object 关联失败: {exc}"
                        raise
            with self.lock:
                if slot.expired_at is not None or self.closing.is_set():
                    raise RuntimeError("查询已取消")
            data = json.dumps({"raw": raw}, ensure_ascii=False).encode("utf-8") + b"\n"
            if len(data) > MAX_MESSAGE:
                raise ValueError("查询参数超过上限")
            slot.proc.stdin.write(data)
            slot.proc.stdin.flush()
            line = slot.proc.stdout.readline(MAX_MESSAGE + 1)
            received = time.monotonic()
            if len(line) > MAX_MESSAGE or not line.endswith(b"\n"):
                raise ValueError("查询响应不完整或超过上限")
            response = json.loads(line)
            room = response["room"]
            room["status"] = RoomStatus(room["status"])
            result = QueryResult(ResolvedRoom(**room), response.get("error"))
            with self.lock:
                if received >= slot.deadline:
                    slot.expired_at = slot.expired_at or received
        except Exception:
            broken = True
        finally:
            with self.lock:
                discard = broken or slot.expired_at is not None or self.closing.is_set()
                proc = slot.proc if discard else None
                deadline = self.close_deadline or (time.monotonic() + self.reclaim_timeout)
            if proc is not None:
                try:
                    self._kill(slot)
                    self._reap(proc, deadline)
                except Exception as exc:
                    self.fatal_error = f"查询进程回收失败（{slot.source}）: {exc}"
                else:
                    with self.lock:
                        slot.proc = None
            with self.lock:
                if not slot.future.done():
                    if slot.expired_at is not None:
                        result = QueryResult(ResolvedRoom(raw, RoomStatus.UNKNOWN), "live_query_timeout")
                    elif self.closing.is_set():
                        result = QueryResult(ResolvedRoom(raw, RoomStatus.UNKNOWN), "service_interrupted")
                    slot.future.set_result(result)
            # 槽位由监控线程在本线程确实退出后归还。

    def _monitor(self):
        while not self.closing.wait(.02):
            now = time.monotonic()
            with self.lock:
                for slot in self.slots:
                    if slot.future is None:
                        continue
                    if not slot.thread.is_alive():
                        slot.thread.join()
                        slot.thread, slot.future, slot.raw = None, None, None
                        continue
                    if now >= slot.deadline:
                        slot.expired_at = slot.expired_at or now
                        try:
                            self._kill(slot)
                        except Exception as exc:
                            self.fatal_error = f"查询终止失败: {exc}"
                        if now - slot.expired_at > self.reclaim_timeout:
                            self.fatal_error = "查询进程或 IPC 线程未在回收预算内退出"

    @staticmethod
    def _reap(proc, deadline):
        proc.wait(timeout=max(0, deadline - time.monotonic()))
        try:
            proc.stdin.close()
        finally:
            proc.stdout.close()
            close_process_handle(proc)

    def close(self):
        with self.close_lock:
            if self.closing.is_set():
                return
            deadline = time.monotonic() + self.reclaim_timeout
            errors = [self.fatal_error] if self.fatal_error else []
            try:
                with self.lock:
                    self.close_deadline = deadline
                    self.closing.set()
                    for slot in self.slots:
                        try:
                            self._kill(slot)
                        except Exception as exc:
                            errors.append(f"通知 {slot.source} 查询终止失败: {exc}")
                for slot in self.slots:
                    with self.lock:
                        thread = slot.thread
                    if thread:
                        thread.join(max(0, deadline - time.monotonic()))
                        if thread.is_alive():
                            errors.append(f"{slot.source} 查询 IPC 线程超过共用回收预算")
                            with self.lock:
                                if not slot.future.done():
                                    slot.future.set_result(QueryResult(
                                        ResolvedRoom(slot.raw, RoomStatus.UNKNOWN), "service_interrupted"))
                            continue
                    with self.lock:
                        proc = slot.proc
                        slot.thread, slot.future, slot.raw = None, None, None
                    if proc is not None:
                        try:
                            self._reap(proc, deadline)
                        except Exception as exc:
                            errors.append(f"{slot.source} 空闲查询进程回收失败: {exc}")
                        else:
                            with self.lock:
                                slot.proc = None
                self.monitor.join(max(0, deadline - time.monotonic()))
                if self.monitor.is_alive():
                    errors.append("查询监控线程超过共用回收预算")
            finally:
                self.job.close()
            if self.fatal_error and self.fatal_error not in errors:
                errors.append(self.fatal_error)
            if errors:
                raise RuntimeError("；".join(errors))


def child_main():
    resolver = RoomResolver()
    while True:
        line = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
        if not line:
            return
        if len(line) > MAX_MESSAGE or not line.endswith(b"\n"):
            return
        raw = json.loads(line)["raw"]
        error = None
        try:
            room = resolver.resolve(raw)
        except Exception:
            room, error = ResolvedRoom(raw, RoomStatus.UNKNOWN), "live_query_failed"
        payload = asdict(room)
        payload["status"] = room.status.value
        data = json.dumps(dict(room=payload, error=error), ensure_ascii=False).encode("utf-8") + b"\n"
        if len(data) > MAX_MESSAGE:
            return
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    child_main()

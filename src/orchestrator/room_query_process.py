"""有界外网查询进程。阻塞 IPC 只在自有线程中执行，独立监控负责期限和回收。"""
from concurrent.futures import Future
import ctypes
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from src.mvp.room_resolver import ResolvedRoom, RoomResolver, RoomStatus

MAX_MESSAGE = 65536
CAPACITY = {"scan": 5, "verify": 1, "manual": 2, "login": 1}


class QueryJob:
    """只收纳查询子进程；句柄不继承，不包含录制 Worker。"""
    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", w.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                        ("active", w.DWORD), ("affinity", ctypes.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("r", "w", "o", "rb", "wb", "ob")]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_mem", ctypes.c_size_t),
                        ("job_mem", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.api.CreateJobObjectW.restype = w.HANDLE
        self.api.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, proc):
        if self.handle and not self.api.AssignProcessToJobObject(self.handle, int(proc._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


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


class QueryProcesses:
    def __init__(self, timeout=30.0, reclaim_timeout=5.0, child_command=None):
        self.timeout, self.reclaim_timeout = timeout, reclaim_timeout
        self.command = child_command or [sys.executable, "-m", "src.orchestrator.room_query_process"]
        self.job = QueryJob()
        self.slots = [Slot(source) for source, count in CAPACITY.items() for _ in range(count)]
        self.lock = threading.RLock()
        self.closing = threading.Event()
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
                if discard:
                    self._kill(slot)
            if discard and slot.proc is not None:
                try:
                    slot.proc.wait(timeout=self.reclaim_timeout)
                    slot.proc.stdin.close()
                    slot.proc.stdout.close()
                    slot.proc = None
                except Exception as exc:
                    self.fatal_error = f"查询进程回收失败: {exc}"
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
                        slot.thread, slot.future = None, None
                        continue
                    if now >= slot.deadline:
                        slot.expired_at = slot.expired_at or now
                        try:
                            self._kill(slot)
                        except Exception as exc:
                            self.fatal_error = f"查询终止失败: {exc}"
                        if now - slot.expired_at > self.reclaim_timeout:
                            self.fatal_error = "查询进程或 IPC 线程未在回收预算内退出"

    def close(self):
        deadline = time.monotonic() + self.reclaim_timeout
        self.closing.set()
        with self.lock:
            for slot in self.slots:
                self._kill(slot)
        for slot in self.slots:
            if slot.thread:
                slot.thread.join(max(0, deadline - time.monotonic()))
            if slot.proc:
                self._kill(slot)
                try:
                    slot.proc.wait(timeout=max(0, deadline - time.monotonic()))
                    slot.proc.stdin.close()
                    slot.proc.stdout.close()
                except subprocess.TimeoutExpired:
                    self.fatal_error = "查询关闭超过共用回收预算"
        self.monitor.join(max(0, deadline - time.monotonic()))
        self.job.close()
        if any(s.thread and s.thread.is_alive() for s in self.slots):
            self.fatal_error = "查询 IPC 线程关闭超过共用回收预算"
        if self.fatal_error:
            raise RuntimeError(self.fatal_error)


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

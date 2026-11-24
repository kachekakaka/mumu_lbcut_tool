"""Worker 子进程句柄：独立排空管道、隔离慢回调、统一停机和事件收尾。"""
from collections import deque
import json
import logging
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
from typing import Callable

import psutil
from src.orchestrator.models import WorkerLaunchSpec

logger = logging.getLogger(__name__)
REPO_ROOT = Path(__file__).resolve().parents[2]


class EventStreamIncomplete(RuntimeError):
    """进程已退出，但事件交付/管道消费未能可靠完成。"""


class WorkerProcessHandle:
    def __init__(self, spec: WorkerLaunchSpec, on_event: Callable[[dict], None] | None = None,
                 python_exe: str | None = None):
        self.spec = spec
        self.on_event = on_event
        self.python_exe = python_exe or sys.executable
        self.proc: subprocess.Popen | None = None
        self._stdout_thread = self._stderr_thread = self._callback_thread = None
        self._lock = threading.Lock()
        self._finished_segments: list[str] = []
        self._latest_heartbeat = self._started_event = self._terminal_event = None
        self._stderr_lines: deque[str] = deque(maxlen=200)
        self._errors: deque[str] = deque(maxlen=20)
        self._callbacks: queue.Queue = queue.Queue(maxsize=4096)
        self._stdout_done, self._stderr_done = threading.Event(), threading.Event()
        self._callback_done = threading.Event()
        self._stop_requested = False
        self._stop_sent = False
        self._control_ready = threading.Event()
        self.forced_stop = False
        self.command: list[str] = []
        self.created_at: float | None = None
        self._journal = None

    def _error(self, text: str) -> None:
        with self._lock:
            self._errors.append(text)
        logger.error(text)

    def start(self) -> None:
        if self.proc is not None:
            raise RuntimeError("WorkerProcessHandle 不允许重复启动")
        from src.worker.room_locator import validate_navigation_timeout
        validate_navigation_timeout(self.spec.navigation_timeout_sec)
        cmd = [self.python_exe, "-B", "-X", "utf8", "-u", "-m", "src.worker.cli",
               "--vmindex", str(self.spec.vmindex), "--target", self.spec.target,
               "--output-dir", str(self.spec.output_dir.resolve()),
               "--navigation-timeout", str(self.spec.navigation_timeout_sec),
               "--segment-duration", str(self.spec.segment_duration_sec),
               "--stall-timeout", str(self.spec.stall_timeout_sec),
               "--heartbeat-interval", str(self.spec.heartbeat_interval_sec),
               "--window-mode", self.spec.window_mode]
        if self.spec.total_duration_sec > 0:
            cmd += ["--total-duration", str(self.spec.total_duration_sec)]
        if self.spec.allow_unconfirmed_identity:
            cmd += ["--allow-unconfirmed-identity"]
        if self.spec.require_local_tools:
            cmd.append("--require-local-tools")
        for flag, value in (("--window-pos-x", self.spec.window_pos_x),
                            ("--window-pos-y", self.spec.window_pos_y),
                            ("--target-kind", self.spec.target_kind),
                            ("--web-rid", self.spec.web_rid),
                            ("--expected-anchor-uid", self.spec.expected_anchor_uid),
                            ("--job-id", self.spec.job_id),
                            ("--event-log", self.spec.event_log_path),
                            ("--log-file", self.spec.log_path)):
            if value is not None and value != "":
                cmd += [flag, str(value)]
        if self.spec.event_log_path:
            path = Path(self.spec.event_log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._journal = path.open("x", encoding="utf-8", buffering=1)
        env = dict(os.environ, PYTHONPATH=str(REPO_ROOT), PYTHONIOENCODING="utf-8")
        self.command = cmd
        try:
            self.proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
            try:
                self.created_at = psutil.Process(self.proc.pid).create_time()
            except (psutil.Error, ValueError, TypeError):
                # 快速退出时可能已无法查询；不能伪造进程出生时间。
                self.created_at = None
        except Exception:
            if self._journal:
                self._journal.close()
            raise
        self._stdout_thread = threading.Thread(target=self._drain_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._callback_thread = threading.Thread(target=self._dispatch_callbacks, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()
        self._callback_thread.start()

    def _drain_stdout(self) -> None:
        try:
            if not self.proc or self.proc.stdout is None:
                return
            for line in self.proc.stdout:
                raw = line.strip()
                if not raw or not raw.startswith("{"):
                    continue
                try:
                    event = json.loads(raw)
                    if not isinstance(event, dict) or "event" not in event:
                        raise ValueError("事件必须为带 event 的对象")
                except (ValueError, TypeError):
                    self._error("invalid_ndjson_event")
                    continue
                if self._journal:
                    try:
                        self._journal.write(json.dumps(event, ensure_ascii=False) + "\n")
                        self._journal.flush()
                        if event["event"] in ("segment_started", "segment_finished", "segment_aborted"):
                            os.fsync(self._journal.fileno())
                    except OSError:
                        self._error("event_journal_write_failed")
                with self._lock:
                    kind = event["event"]
                    if kind == "worker_started":
                        self._started_event = event
                        # Worker 必须在注册 SIGINT/SIGBREAK 后才发此事件。
                        self._control_ready.set()
                    elif kind == "heartbeat":
                        self._latest_heartbeat = event
                    elif kind == "segment_finished":
                        path = event.get("file_path")
                        if path and path not in self._finished_segments:
                            self._finished_segments.append(path)
                    elif kind in ("worker_stopped", "worker_failed", "stream_ended",
                                  "login_blocked", "recording_failed", "fatal_error"):
                        self._terminal_event = event
                if self.on_event:
                    try:
                        self._callbacks.put_nowait(event)
                    except queue.Full:
                        # 原始事件仍留在日志中；明确失败，不能静默丢失后宣布成功。
                        self._error("event_callback_queue_overflow")
        except Exception as exc:
            self._error(f"stdout_read_failed: {exc}")
        finally:
            if self._journal:
                self._journal.close()
            if self.proc and hasattr(self.proc.stdout, "close"):
                self.proc.stdout.close()
            self._stdout_done.set()

    def _dispatch_callbacks(self) -> None:
        try:
            while not self._stdout_done.is_set() or not self._callbacks.empty():
                try:
                    event = self._callbacks.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    self.on_event(event)
                except Exception as exc:
                    self._error(f"event_callback_failed: {exc}")
                finally:
                    self._callbacks.task_done()
        finally:
            self._callback_done.set()

    def _drain_stderr(self) -> None:
        try:
            if self.proc and self.proc.stderr is not None:
                for line in self.proc.stderr:
                    with self._lock:
                        self._stderr_lines.append(line.rstrip())
        except Exception as exc:
            self._error(f"stderr_read_failed: {exc}")
        finally:
            if self.proc and hasattr(self.proc.stderr, "close"):
                self.proc.stderr.close()
            self._stderr_done.set()

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def poll_exit_code(self) -> int | None:
        return self.proc.poll() if self.proc else None

    @property
    def delivery_closed(self) -> bool:
        return self._stdout_done.is_set() and self._callback_done.is_set()

    @property
    def events_complete(self) -> bool:
        return self.delivery_closed and self._stderr_done.is_set() and not self.event_errors

    @property
    def event_errors(self) -> list[str]:
        with self._lock:
            return list(self._errors)

    def wait(self, timeout: float | None = None) -> int:
        if not self.proc:
            return -1
        deadline = time.monotonic() + timeout if timeout is not None else None
        code = self.proc.wait(timeout=timeout)
        for thread in (self._stdout_thread, self._stderr_thread, self._callback_thread):
            if thread:
                remaining = max(0.0, deadline - time.monotonic()) if deadline else 5.0
                thread.join(timeout=remaining)
        if not self.events_complete:
            raise EventStreamIncomplete("事件流收尾未完成: " + ";".join(self.event_errors))
        return code

    def request_stop(self) -> None:
        """登记停止意图；只有控制通道就绪才发送，stop() 负责有界等待。"""
        self._stop_requested = True
        if not self.proc or self.proc.poll() is not None or not self._control_ready.is_set():
            return
        with self._lock:
            if self._stop_sent:
                return
            self._stop_sent = True
        try:
            self.proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
        except OSError:
            if self.proc.poll() is None:
                raise

    def stop(self, timeout_sec: float = 30.0) -> int:
        if not self.proc:
            return 0
        deadline = time.monotonic() + max(0.1, timeout_sec)
        try:
            self._stop_requested = True
            # 避免 Popen 刚返回时 SIGBREAK 被 Python 的默认处理器直接终止。
            while self.is_alive() and not self._control_ready.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(self.command or "worker_start", timeout_sec)
                self._control_ready.wait(timeout=min(.05, remaining))
            self.request_stop()
            return self.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            self.forced_stop = True
            # 只处理这个句柄创建的进程树，禁止按进程名全局终止。
            children = []
            try:
                owner = psutil.Process(self.proc.pid)
                if self.created_at is not None and abs(owner.create_time() - self.created_at) < 0.01:
                    children = owner.children(recursive=True)
            except psutil.Error:
                pass
            for child in reversed(children):
                try:
                    name = child.name().lower()
                    command = child.cmdline()
                    # ADB server/MuMu 主程序可能是共享服务，不能因为是后代进程就终止。
                    # 本句柄已核对根 PID/出生时间，连续采集子进程以唯一命名管道为归属特征。
                    owned_pipe = name.startswith("scrcpy") and any(
                        arg.startswith("--record=" + "\\\\.\\pipe\\mumu_capture_") for arg in command)
                    owned_media = name.startswith(("scrcpy", "ffprobe", "ffmpeg")) and (
                        any(str(self.spec.output_dir.resolve()).lower() in arg.lower() for arg in command)
                        or owned_pipe)
                    if owned_media:
                        child.kill()
                except psutil.Error:
                    pass
            if self.proc.poll() is None:
                self.proc.kill()
            # 强杀后的回收预算独立且有界，向调用方暴露 forced_stop。
            return self.wait(timeout=5.0)

    @property
    def finished_segments(self) -> list[str]:
        with self._lock:
            return list(self._finished_segments)

    @property
    def latest_heartbeat(self) -> dict | None:
        with self._lock:
            return dict(self._latest_heartbeat) if self._latest_heartbeat else None

    @property
    def terminal_event(self) -> dict | None:
        with self._lock:
            return dict(self._terminal_event) if self._terminal_event else None

    @property
    def stderr_tail(self) -> list[str]:
        with self._lock:
            return list(self._stderr_lines)

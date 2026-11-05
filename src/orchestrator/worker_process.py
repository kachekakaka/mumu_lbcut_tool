"""Worker 独立子进程控制句柄 (WorkerProcessHandle)。

职责：
1. 以标准 CLI 参数拉起独立 Worker 执行单元；
2. 异步流式消费标准输出中的 NDJSON 事件流，避免管道缓冲导致死锁；
3. 将事件实时分发至回调函数，自动维护切片产物列表与心跳；
4. 提供确定性的优雅停止（Graceful Stop）与硬超时兜底机制。
"""
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

from src.orchestrator.models import WorkerLaunchSpec

logger = logging.getLogger(__name__)


class WorkerProcessHandle:
    """单个 Worker 子进程的生命周期与事件流控制句柄。"""

    def __init__(
        self,
        spec: WorkerLaunchSpec,
        on_event: Optional[Callable[[dict], None]] = None,
        python_exe: Optional[str] = None,
    ) -> None:
        self.spec = spec
        self.on_event = on_event
        self.python_exe = python_exe or sys.executable

        self.proc: Optional[subprocess.Popen] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None

        self._lock = threading.Lock()
        self._finished_segments: list[str] = []
        self._latest_heartbeat: Optional[dict] = None
        self._started_event: Optional[dict] = None
        self._terminal_event: Optional[dict] = None
        self._stderr_lines: list[str] = []
        self._is_stopped = False

    def start(self) -> None:
        """启动 Worker 子进程并建立无死锁异步事件流监听。"""
        cmd = [
            self.python_exe,
            "-u",  # 禁用标准输入输出缓冲，确保 NDJSON 实时吐出
            "-m", "src.worker.cli",
            "--vmindex", str(self.spec.vmindex),
            "--target", str(self.spec.target),
            "--output-dir", str(self.spec.output_dir),
            "--segment-duration", str(self.spec.segment_duration_sec),
            "--overlap-duration", str(self.spec.overlap_duration_sec),
            "--stall-timeout", str(self.spec.stall_timeout_sec),
            "--heartbeat-interval", str(self.spec.heartbeat_interval_sec),
            "--window-mode", str(self.spec.window_mode),
        ]
        if self.spec.total_duration_sec > 0:
            cmd.extend(["--total-duration", str(self.spec.total_duration_sec)])
        if self.spec.allow_unconfirmed_identity:
            cmd.append("--allow-unconfirmed-identity")
        if self.spec.window_pos_x is not None:
            cmd.extend(["--window-pos-x", str(self.spec.window_pos_x)])
        if self.spec.window_pos_y is not None:
            cmd.extend(["--window-pos-y", str(self.spec.window_pos_y)])

        env = dict(os.environ, PYTHONPATH=".")
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

        logger.info(f"拉起 Worker #{self.spec.vmindex} (目标: {self.spec.target}): {' '.join(cmd)}")
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=creationflags,
        )

        # 启动异步读取线程
        self._stdout_thread = threading.Thread(target=self._drain_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _drain_stdout(self) -> None:
        """消费 Worker stdout NDJSON 事件流。"""
        if not self.proc or not self.proc.stdout:
            return

        for line in self.proc.stdout:
            raw = line.strip()
            if not raw or not (raw.startswith("{") and raw.endswith("}")):
                continue
            try:
                event = json.loads(raw)
            except Exception:
                continue

            event_type = event.get("event")
            with self._lock:
                if event_type == "worker_started":
                    self._started_event = event
                elif event_type == "heartbeat":
                    self._latest_heartbeat = event
                elif event_type == "segment_finished":
                    f_path = event.get("file_path")
                    if f_path:
                        self._finished_segments.append(f_path)
                elif event_type in ("stream_ended", "login_blocked", "fatal_error"):
                    self._terminal_event = event

            if self.on_event:
                try:
                    self.on_event(event)
                except Exception as e:
                    logger.warning(f"Worker 事件回调处理异常: {e}")

    def _drain_stderr(self) -> None:
        """消费 Worker stderr 调试日志。"""
        if not self.proc or not self.proc.stderr:
            return

        for line in self.proc.stderr:
            cleaned = line.strip()
            if cleaned:
                with self._lock:
                    if len(self._stderr_lines) < 200:
                        self._stderr_lines.append(cleaned)
                    else:
                        self._stderr_lines.pop(0)
                        self._stderr_lines.append(cleaned)

    def is_alive(self) -> bool:
        """检查 Worker 进程是否依然存活。"""
        if not self.proc:
            return False
        return self.proc.poll() is None

    def poll_exit_code(self) -> Optional[int]:
        """获取退出码，若未退出则返回 None。"""
        if not self.proc:
            return None
        return self.proc.poll()

    def wait(self, timeout: Optional[float] = None) -> int:
        """阻塞等待 Worker 退出。"""
        if not self.proc:
            return -1
        exit_code = self.proc.wait(timeout=timeout)
        if self._stdout_thread:
            self._stdout_thread.join(timeout=2.0)
        if self._stderr_thread:
            self._stderr_thread.join(timeout=2.0)
        return exit_code

    def stop(self, timeout_sec: float = 15.0) -> int:
        """优雅停止 Worker（允许当前切片安全收尾封口），超时后强制 kill。"""
        if not self.proc or self.proc.poll() is not None:
            return self.proc.poll() if self.proc else 0

        logger.info(f"正在向 Worker #{self.spec.vmindex} 发送优雅退出信号...")
        try:
            if os.name == "nt":
                # Windows 下通过进程组发送 CTRL_BREAK_EVENT
                self.proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self.proc.send_signal(signal.SIGINT)
        except Exception as e:
            logger.warning(f"发送中断信号失败，直接回退至 terminate: {e}")
            self.proc.terminate()

        try:
            exit_code = self.proc.wait(timeout=timeout_sec)
            logger.info(f"Worker #{self.spec.vmindex} 已优雅退出，退出码: {exit_code}")
            return exit_code
        except subprocess.TimeoutExpired:
            logger.warning(f"Worker #{self.spec.vmindex} 优雅退出超时 ({timeout_sec}s)，强制 kill...")
            self.proc.kill()
            exit_code = self.proc.wait(timeout=5.0)
            return exit_code

    @property
    def finished_segments(self) -> list[str]:
        """获取该 Worker 已归档完成的所有切片文件路径。"""
        with self._lock:
            return list(self._finished_segments)

    @property
    def latest_heartbeat(self) -> Optional[dict]:
        """获取最新心跳数据包。"""
        with self._lock:
            return dict(self._latest_heartbeat) if self._latest_heartbeat else None

    @property
    def terminal_event(self) -> Optional[dict]:
        """获取终态通知事件（如 stream_ended / login_blocked）。"""
        with self._lock:
            return dict(self._terminal_event) if self._terminal_event else None

    @property
    def stderr_tail(self) -> list[str]:
        """获取最后的 stderr 输出。"""
        with self._lock:
            return list(self._stderr_lines)

"""统一外部命令超时执行器与单调时钟流程截止时间管理。

提供防挂死硬超时强杀能力与非阻塞单调时钟预算分配机制。
"""
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import subprocess
import sys
import time

logger = logging.getLogger("WorkerExecutor")


@dataclass
class CommandResult:
    """命令执行结果封装。"""
    exit_code: int
    stdout: str
    stderr: str
    duration: float


class CommandTimeoutError(TimeoutError):
    """命令执行硬超时异常。"""
    def __init__(self, cmd: list[str], timeout: float, stdout: str = "", stderr: str = ""):
        self.cmd = cmd
        self.timeout = timeout
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(f"命令执行超时 (时限 {timeout}s): {' '.join(cmd)}")


class DeadlineExceededError(TimeoutError):
    """流程截止时间整体预算耗尽异常。"""
    pass


class MonotonicDeadline:
    """基于单调时钟 (monotonic) 的流程截止时间预算管理器。

    用于多步骤业务流程，杜绝单个子步骤占用过多时间或整体流程无限延期。
    """
    def __init__(self, budget_seconds: float):
        if budget_seconds < 0:
            budget_seconds = 0.0
        self.budget_seconds = budget_seconds
        self._start_time = time.monotonic()
        self._deadline = self._start_time + budget_seconds

    def remaining_budget(self) -> float:
        """获取当前流程剩余的可用单调时间预算 (秒)。"""
        return max(0.0, self._deadline - time.monotonic())

    def is_expired(self) -> bool:
        """检查整体预算是否已耗尽。"""
        return time.monotonic() >= self._deadline

    def check_not_expired(self) -> None:
        """若预算已耗尽，则抛出 DeadlineExceededError。"""
        if self.is_expired():
            raise DeadlineExceededError(
                f"流程总时间预算已耗尽 (预设预算: {self.budget_seconds:.2f}s, 已耗时: {time.monotonic() - self._start_time:.2f}s)"
            )

    def allocate_timeout(self, max_allowed: float | None = None) -> float:
        """为下一个子操作动态分配单次超时配额。

        若剩余预算小于 max_allowed，则以剩余预算为准（动态裁剪）。
        """
        remaining = self.remaining_budget()
        if max_allowed is None:
            return remaining
        return min(max_allowed, remaining)


def _kill_process_tree(pid: int) -> None:
    """Windows 下通过 taskkill 强杀目标进程及其所有子孙进程。"""
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3.0,
            check=False
        )
    except Exception:
        pass


def run_command(
    cmd: list[str],
    timeout: float | None = None,
    check: bool = False,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
) -> CommandResult:
    """带硬超时保护的外部命令执行函数。

    参数:
        cmd: 命令行参数列表
        timeout: 硬超时秒数（若为 None 则无超时）
        check: 退出码非 0 时是否抛出 subprocess.CalledProcessError
        cwd: 执行工作目录
        env: 自定义环境变量字典

    异常:
        CommandTimeoutError: 当执行时长超过 timeout 时强杀子进程树并抛出
        subprocess.CalledProcessError: 当 check=True 且 exit_code != 0 时抛出
    """
    start_mono = time.monotonic()

    # 统一环境变量
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(cwd) if cwd else None,
            env=merged_env,
        )
    except Exception as e:
        logger.error(f"启动子进程失败: {' '.join(cmd)}, 错误: {e}")
        raise

    try:
        stdout_data, stderr_data = proc.communicate(timeout=timeout)
        duration = time.monotonic() - start_mono
        exit_code = proc.returncode

        if check and exit_code != 0:
            raise subprocess.CalledProcessError(
                returncode=exit_code,
                cmd=cmd,
                output=stdout_data,
                stderr=stderr_data
            )

        return CommandResult(
            exit_code=exit_code,
            stdout=stdout_data,
            stderr=stderr_data,
            duration=duration,
        )

    except subprocess.TimeoutExpired:
        logger.warning(f"外部命令超时 (时限 {timeout}s)，执行子进程树强杀: {' '.join(cmd)} (PID: {proc.pid})")
        try:
            proc.kill()
        except Exception:
            pass
        _kill_process_tree(proc.pid)
        stdout_data, stderr_data = proc.communicate()
        raise CommandTimeoutError(
            cmd=cmd,
            timeout=timeout or 0.0,
            stdout=stdout_data,
            stderr=stderr_data
        )

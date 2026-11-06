"""严格目标设备 ADB 客户端与状态轮询器 (Ticket 04)。"""
import logging
from pathlib import Path
import time
from typing import Optional

from src.mvp.config import get_adb_exe
from src.worker.executor import (
    run_command,
    CommandResult,
    MonotonicDeadline,
    DeadlineExceededError,
)

logger = logging.getLogger("AdbDeviceClient")


class DeviceUnavailableError(RuntimeError):
    """目标 ADB 设备离线或未就绪异常。"""
    pass


class AdbDeviceClient:
    """严格绑定单一目标设备的 ADB 客户端。

    严禁缺省 -s 或模糊匹配系统中的其他设备。
    """

    def __init__(self, serial: str, adb_exe: Path | None = None):
        if not serial or not serial.strip():
            raise ValueError("ADB serial 不能为空")
        self.serial = serial.strip()
        self.adb_exe = adb_exe or get_adb_exe()
        if not self.adb_exe or not self.adb_exe.exists():
            raise FileNotFoundError(f"未找到 adb.exe: {self.adb_exe}")

    @property
    def current_serial(self) -> str:
        return self.serial

    def run_adb(self, *args: str, timeout: float = 5.0, check: bool = False) -> CommandResult:
        """执行 ADB 命令，强制携带 -s <exact_serial>。"""
        cmd = [str(self.adb_exe), "-s", self.serial, *args]
        return run_command(cmd, timeout=timeout, check=check)

    def is_device_ready(self, timeout: float = 3.0) -> bool:
        """检查目标设备状态是否为 'device'。"""
        try:
            res = self.run_adb("get-state", timeout=timeout)
            return res.exit_code == 0 and res.stdout.strip() == "device"
        except Exception:
            return False

    def ensure_connected(self, timeout: float = 5.0) -> bool:
        """尝试显式连接目标端口并核验。"""
        cmd = [str(self.adb_exe), "connect", self.serial]
        deadline = time.monotonic() + max(0.0, timeout)
        if timeout < 1.0:
            return False
        try:
            res = run_command(cmd, timeout=timeout)
            logger.debug(f"ADB connect {self.serial}: {res.stdout.strip()}")
        except Exception as e:
            logger.warning(f"ADB connect 异常: {e}")
        remaining = deadline-time.monotonic()
        return remaining >= 1.0 and self.is_device_ready(timeout=min(2.0, remaining))

    def wait_for_device(self, timeout_sec: float = 15.0, poll_interval: float = 0.5) -> None:
        """基于单调时钟有界轮询目标设备是否就绪。

        若超时后设备仍未就绪，抛出 DeviceUnavailableError，严禁串绑其他设备。
        """
        deadline = MonotonicDeadline(budget_seconds=timeout_sec)
        
        while not deadline.is_expired():
            # 先尝试检查已连接状态
            if self.is_device_ready(timeout=deadline.allocate_timeout(max_allowed=2.0)):
                return

            # 若未就绪，尝试显式连接
            self.ensure_connected(timeout=deadline.allocate_timeout(max_allowed=3.0))
            if self.is_device_ready(timeout=deadline.allocate_timeout(max_allowed=2.0)):
                return

            sleep_dur = min(poll_interval, deadline.remaining_budget())
            if sleep_dur > 0:
                time.sleep(sleep_dur)

        raise DeviceUnavailableError(
            f"目标 ADB 设备 {self.serial} 在 {timeout_sec:.1f} 秒内未就绪 (当前状态离线或不可用)"
        )

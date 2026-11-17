"""严格目标设备 ADB 客户端与状态轮询器 (Ticket 04)。"""
import logging
from pathlib import Path
import time
import math
import threading
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
        adb = adb_exe or get_adb_exe()
        if not adb or not Path(adb).exists():
            raise FileNotFoundError(f"未找到 adb.exe: {adb}")
        self.adb_exe = Path(adb).resolve()
        self._metrics_lock = threading.Lock()
        self._command_metrics = {}
        self._slow_commands = []

    @property
    def current_serial(self) -> str:
        return self.serial

    @staticmethod
    def _command_kind(args: tuple[str, ...]) -> str:
        joined = " ".join(args).lower()
        if "uiautomator dump" in joined:
            return "ui_dump"
        if args[:2] == ("shell", "cat"):
            return "ui_xml_read"
        if "screencap" in joined:
            return "screenshot"
        if args[:3] == ("shell", "dumpsys", "activity"):
            return "activity_probe"
        if args[:3] == ("shell", "dumpsys", "window"):
            return "window_probe"
        if args[:3] == ("shell", "input", "tap"):
            return "tap"
        if args[:3] == ("shell", "input", "keyevent"):
            return "keyevent"
        if args[:3] == ("shell", "am", "force-stop"):
            return "force_stop"
        if args[:2] == ("shell", "am") and "start" in args:
            return "intent"
        return "other"

    def reset_metrics(self) -> None:
        with self._metrics_lock:
            self._command_metrics.clear()
            self._slow_commands.clear()

    def metrics_snapshot(self) -> dict:
        with self._metrics_lock:
            result = {}
            for kind, row in self._command_metrics.items():
                samples = sorted(row["samples_ms"])
                def percentile(p):
                    if not samples:
                        return 0.0
                    index = min(len(samples)-1, math.ceil((len(samples)-1)*p))
                    return round(samples[index], 1)
                result[kind] = {
                    "count": row["count"],
                    "total_ms": round(row["total_ms"], 1),
                    "max_ms": round(row["max_ms"], 1),
                    "p50_ms": percentile(.50),
                    "p95_ms": percentile(.95),
                    "failures": row["failures"],
                    "timeouts": row["timeouts"],
                }
            return {"commands": result, "slow_commands": list(self._slow_commands)}

    def _record_command(self, kind: str, elapsed_ms: float, status: str) -> None:
        with self._metrics_lock:
            row = self._command_metrics.setdefault(kind, {
                "count": 0, "total_ms": 0.0, "max_ms": 0.0,
                "failures": 0, "timeouts": 0, "samples_ms": [],
            })
            row["count"] += 1
            row["total_ms"] += elapsed_ms
            row["max_ms"] = max(row["max_ms"], elapsed_ms)
            row["failures"] += int(status != "ok")
            row["timeouts"] += int(status == "timeout")
            row["samples_ms"].append(elapsed_ms)
            if len(row["samples_ms"]) > 256:
                del row["samples_ms"][:-256]
            if elapsed_ms >= 500:
                self._slow_commands.append({
                    "kind": kind, "elapsed_ms": round(elapsed_ms, 1), "status": status,
                })
                if len(self._slow_commands) > 32:
                    del self._slow_commands[:-32]

    def run_adb(self, *args: str, timeout: float = 5.0, check: bool = False,
                kill_tree_on_timeout: bool = True) -> CommandResult:
        """执行 ADB 命令，强制携带 -s <exact_serial>，并记录分类耗时。"""
        cmd = [str(self.adb_exe), "-s", self.serial, *args]
        kind = self._command_kind(args)
        started = time.monotonic()
        status = "ok"
        try:
            options = {} if kill_tree_on_timeout else {'kill_tree_on_timeout': False}
            result = run_command(cmd, timeout=timeout, check=check, cwd=self.adb_exe.parent, **options)
            if result.exit_code != 0:
                status = "error"
            return result
        except Exception as exc:
            status = "timeout" if "timeout" in type(exc).__name__.lower() else "error"
            raise
        finally:
            self._record_command(kind, (time.monotonic()-started)*1000.0, status)

    def force_stop_package(self, package: str, timeout: float = 4.0) -> None:
        """只关闭本设备上的指定应用；失败交给调用方记录，不重开首页。"""
        result = self.run_adb("shell", "am", "force-stop", package, timeout=timeout)
        if result.exit_code != 0 or "error" in (result.stdout + result.stderr).lower():
            raise RuntimeError(f"force-stop 失败，exit_code={result.exit_code}; stdout={result.stdout}; stderr={result.stderr}")

    def is_device_ready(self, timeout: float = 3.0) -> bool:
        """检查目标设备状态是否为 'device'。"""
        try:
            res = self.run_adb("get-state", timeout=max(.001,timeout-.2), kill_tree_on_timeout=False)
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
            res = run_command(cmd, timeout=max(.001,timeout-.2), cwd=self.adb_exe.parent,
                              kill_tree_on_timeout=False)
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

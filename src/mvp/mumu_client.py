"""MuMu 模拟器管理与命令行客户端。"""
from pathlib import Path
import json
import logging
import subprocess
import time
import re

from src.mvp.config import get_mumu_manager_exe, get_adb_exe

logger = logging.getLogger(__name__)


class MuMuClient:
    """封装 MuMuManager.exe 的生命周期与控制接口。"""

    def __init__(self, manager_exe: Path | None = None, adb_exe: Path | None = None):
        self.manager_exe = manager_exe or get_mumu_manager_exe()
        self.adb_exe = adb_exe or get_adb_exe()
        if not self.manager_exe or not self.manager_exe.exists():
            raise FileNotFoundError(f"未找到 MuMuManager.exe: {self.manager_exe}")
        if not self.adb_exe or not self.adb_exe.exists():
            raise FileNotFoundError(f"未找到 adb.exe: {self.adb_exe}")

    def _run_manager(self, *args: str) -> str:
        """执行 MuMuManager 命令并返回 stdout。"""
        cmd = [str(self.manager_exe), *args]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if res.returncode != 0:
            logger.warning(f"MuMuManager 执行返回码 {res.returncode}: {res.stderr.strip() or res.stdout.strip()}")
        return res.stdout.strip()

    def get_all_players(self) -> dict[str, dict]:
        """获取所有实例信息字典。"""
        out = self._run_manager("info", "-v", "all")
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            logger.error(f"解析 MuMuManager info 失败: {out}")
            return {}

    def get_player_info(self, vmindex: int) -> dict:
        """获取单个实例信息字典。"""
        out = self._run_manager("info", "-v", str(vmindex))
        try:
            data = json.loads(out)
            # 有时返回以 str(vmindex) 为 key，有时直接是对象
            if str(vmindex) in data:
                return data[str(vmindex)]
            return data
        except json.JSONDecodeError:
            logger.error(f"解析实例 {vmindex} 信息失败: {out}")
            return {}

    def is_android_started(self, vmindex: int) -> bool:
        """检查 Android 系统是否已完全启动。"""
        info = self.get_player_info(vmindex)
        return bool(info.get("is_android_started", False))

    def ensure_main_process_running(self) -> bool:
        """确保 MuMu 主程序 (MuMuNxMain.exe) 处于运行状态。"""
        # 检查进程
        main_exe = self.manager_exe.parent / "MuMuNxMain.exe"
        if not main_exe.exists():
            return True

        res = subprocess.run(
            ["tasklist", "/fi", "IMAGENAME eq MuMuNxMain.exe"],
            capture_output=True,
            text=True
        )
        if "MuMuNxMain.exe" not in res.stdout:
            logger.info("MuMu 主程序未启动，正在拉起主程序...")
            subprocess.Popen(
                [str(main_exe), "--from-shortcut"],
                cwd=str(self.manager_exe.parent)
            )
            time.sleep(5)
        return True

    def launch_player(self, vmindex: int, timeout_sec: int = 60) -> bool:
        """启动指定实例并等待其 Android 系统就绪。"""
        self.ensure_main_process_running()

        if self.is_android_started(vmindex):
            logger.info(f"实例 {vmindex} 已经处于运行状态。")
            return True

        logger.info(f"正在启动 MuMu 实例 {vmindex}...")
        self._run_manager("control", "-v", str(vmindex), "launch")

        start_time = time.time()
        while time.time() - start_time < timeout_sec:
            time.sleep(3)
            if self.is_android_started(vmindex):
                logger.info(f"实例 {vmindex} Android 系统启动成功 (耗时 {int(time.time() - start_time)}s)。")
                return True

        logger.error(f"实例 {vmindex} 启动超时 ({timeout_sec}s)。")
        return False

    def shutdown_player(self, vmindex: int) -> bool:
        """关闭指定实例。"""
        logger.info(f"正在关闭 MuMu 实例 {vmindex}...")
        self._run_manager("control", "-v", str(vmindex), "shutdown")
        return True

    def get_adb_serial(self, vmindex: int) -> str:
        """获取实例对应的 ADB 地址与端口（如 127.0.0.1:16448）。"""
        info = self.get_player_info(vmindex)
        host = info.get("adb_host_ip") or "127.0.0.1"
        port = info.get("adb_port")
        if port:
            return f"{host}:{port}"

        # 尝试从 vm_config.json 读取
        config_path = self.manager_exe.parent.parent / "vms" / f"MuMuPlayer-15.0-{vmindex}" / "configs" / "vm_config.json"
        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    cfg_port = cfg.get("vm", {}).get("nat", {}).get("port_forward", {}).get("adb", {}).get("host_port")
                    if cfg_port:
                        return f"{host}:{cfg_port}"
            except Exception:
                pass

        # 官方规则默认计算
        calc_port = 16384 + 32 * int(vmindex)
        return f"{host}:{calc_port}"

    def connect_adb(self, vmindex: int) -> str | None:
        """建立 ADB 连接并返回设备 serial。"""
        serial = self.get_adb_serial(vmindex)
        logger.info(f"正在连接实例 {vmindex} 的 ADB: {serial}")

        # 先通过 manager 触发连接
        self._run_manager("adb", "-v", str(vmindex), "-c", "connect")

        # 显式执行 adb connect
        subprocess.run([str(self.adb_exe), "connect", serial], capture_output=True, text=True)

        # 检查 devices
        res = subprocess.run([str(self.adb_exe), "devices"], capture_output=True, text=True)
        lines = [line.strip() for line in res.stdout.strip().splitlines() if line.strip()]

        for line in lines[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[0] == serial and parts[1] == "device":
                return serial

        # 严禁模糊匹配或串绑其他在线设备
        logger.warning(f"实例 {vmindex} 的目标设备 {serial} 未处于 device 状态")
        return None

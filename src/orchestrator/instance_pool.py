"""模拟器实例池：查询实例在线状态，登录强阻断后冷启动抖音。

实例的分配与占用只记录在调度数据库；本模块不持有占用状态，也不开关模拟器。
"""
import logging
import os
import subprocess

from src.mvp.mumu_client import MuMuClient
from src.orchestrator.models import InstanceSlot, InstanceStatus

logger = logging.getLogger(__name__)

DOUYIN_PACKAGE = "com.ss.android.ugc.aweme"


class InstancePool:
    """MuMu 实例查询与抖音复位。"""

    def __init__(self, mumu_client: MuMuClient | None = None) -> None:
        self._mumu = mumu_client

    @property
    def mumu(self) -> MuMuClient:
        if self._mumu is None:
            self._mumu = MuMuClient()
        return self._mumu

    def refresh(self) -> list[InstanceSlot]:
        """扫描全部实例；Android 未启动的实例报告为离线。查询失败时抛出，由调用方保留原状态。"""
        slots = []
        for inst in self.mumu.list_available_instances():
            started = bool(inst.get("android_started"))
            slots.append(InstanceSlot(vmindex=inst["vmindex"], name=inst.get("name", f"player_{inst['vmindex']}"),
                                      status=InstanceStatus.IDLE if started else InstanceStatus.OFFLINE,
                                      raw_info=inst.get("raw_info", {})))
        return slots

    def reset_app(self, vmindex: int) -> bool:
        """关闭并冷启动抖音首页；不清数据、不登录。ADB 不通或命令失败返回 False。"""
        serial = self.mumu.connect_adb(vmindex, retries=2)
        if not serial:
            logger.warning("实例 %s 复位抖音失败：ADB 不通", vmindex)
            return False
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        adb = str(self.mumu.adb_exe)
        try:
            for args in (["am", "force-stop", DOUYIN_PACKAGE],
                         ["am", "start", "-n", f"{DOUYIN_PACKAGE}/.main.MainActivity"]):
                result = subprocess.run([adb, "-s", serial, "shell", *args], capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", timeout=20, creationflags=flags)
                if result.returncode != 0 or "Error" in result.stdout + result.stderr:
                    logger.warning("实例 %s 复位抖音命令失败: %s", vmindex, (result.stderr or result.stdout).strip())
                    return False
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("实例 %s 复位抖音异常: %s", vmindex, exc)
            return False
        logger.info("实例 %s 已冷启动抖音", vmindex)
        return True

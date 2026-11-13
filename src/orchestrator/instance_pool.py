"""模拟器实例池：查询实例在线状态、按需拉起实例开机。

实例的分配与占用只记录在调度数据库；本模块不持有占用状态，也不自动关闭模拟器。
"""
import logging

from src.mvp.mumu_client import MuMuClient
from src.orchestrator.models import InstanceSlot, InstanceStatus

logger = logging.getLogger(__name__)

class InstancePool:
    """MuMu 实例查询与按需开机；应用收尾归 Worker 所有。"""

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

    def boot_instance(self, vmindex: int, stop_event=None) -> bool:
        """按需启动指定 MuMu 实例并等待 Android 就绪；不自动关机。"""
        try:
            ok = bool(self.mumu.launch_player(vmindex, stop_event=stop_event))
        except Exception as exc:
            logger.warning("实例 %s 自动开机异常: %s", vmindex, exc)
            return False
        if not ok:
            logger.warning("实例 %s 自动开机失败", vmindex)
        return ok

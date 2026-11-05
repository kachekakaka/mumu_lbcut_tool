"""模拟器实例池管理器 (InstancePool)。

职责：
1. 发现并维护本地所有 MuMu 模拟器实例槽位；
2. 线程安全地申请 (acquire) 与释放 (release) 实例；
3. 自动隔离因登录/风控强阻断 (退出码 15) 的异常实例，避免后续任务被分配至坏实例；
4. 提供池状态快照供运维或调度控制台监控。
"""
import logging
import threading
import time
from typing import Optional

from src.mvp.mumu_client import MuMuClient
from src.orchestrator.models import InstanceSlot, InstanceStatus

logger = logging.getLogger(__name__)

EXIT_LOGIN_BLOCKED = 15  # 确诊登录阻断标准退出码


class InstancePool:
    """MuMu 模拟器实例调度池。"""

    def __init__(self, mumu_client: MuMuClient | None = None) -> None:
        self.mumu = mumu_client or MuMuClient()
        self._lock = threading.Lock()
        self._slots: dict[int, InstanceSlot] = {}

    def refresh(self) -> list[InstanceSlot]:
        """从模拟器底层扫描并同步更新所有可用实例。"""
        with self._lock:
            try:
                instances = self.mumu.list_available_instances()
            except Exception as e:
                logger.error(f"扫描 MuMu 实例池失败: {e}")
                instances = []

            seen_indices = set()
            for inst in instances:
                idx = inst["vmindex"]
                seen_indices.add(idx)
                if idx not in self._slots:
                    self._slots[idx] = InstanceSlot(
                        vmindex=idx,
                        name=inst.get("name", f"player_{idx}"),
                        status=InstanceStatus.IDLE,
                        raw_info=inst.get("raw_info", {}),
                    )
                else:
                    slot = self._slots[idx]
                    slot.name = inst.get("name", slot.name)
                    slot.raw_info = inst.get("raw_info", slot.raw_info)

            # 标记已移除的实例为 OFFLINE
            for idx, slot in self._slots.items():
                if idx not in seen_indices and slot.status != InstanceStatus.BUSY:
                    slot.status = InstanceStatus.OFFLINE

            return list(self._slots.values())

    def acquire(self, target: str, job_id: str | None = None) -> InstanceSlot | None:
        """从池中原子申请一个就绪闲置的实例。"""
        with self._lock:
            for slot in self._slots.values():
                if slot.status == InstanceStatus.IDLE:
                    slot.status = InstanceStatus.BUSY
                    slot.current_target = target
                    slot.current_job_id = job_id
                    slot.last_acquired_at = time.time()
                    logger.info(f"成功为目标 {target} 分配实例 #{slot.vmindex} ({slot.name})")
                    return slot
            return None

    def release(self, vmindex: int, error_code: int | None = None) -> None:
        """归还实例并根据退出状态更新槽位。"""
        with self._lock:
            slot = self._slots.get(vmindex)
            if not slot:
                return

            slot.current_target = None
            slot.current_job_id = None
            slot.last_released_at = time.time()

            if error_code == EXIT_LOGIN_BLOCKED:
                slot.status = InstanceStatus.BLOCKED
                slot.blocked_reason = "检测到登录/强弹窗阻断，已被调度池安全隔离"
                logger.warning(f"实例 #{vmindex} 触发强登录阻断 (退出码 15)，已被标记隔离！")
            else:
                slot.status = InstanceStatus.IDLE
                slot.blocked_reason = None
                logger.info(f"实例 #{vmindex} 已归还并恢复为闲置 (IDLE)")

    def unblock(self, vmindex: int) -> bool:
        """人工或外部干预完成后解除实例隔离。"""
        with self._lock:
            slot = self._slots.get(vmindex)
            if slot and slot.status == InstanceStatus.BLOCKED:
                slot.status = InstanceStatus.IDLE
                slot.blocked_reason = None
                logger.info(f"实例 #{vmindex} 已解除隔离，重新恢复可用状态")
                return True
            return False

    def get_slot(self, vmindex: int) -> InstanceSlot | None:
        """获取单个实例槽位。"""
        with self._lock:
            return self._slots.get(vmindex)

    def get_summary(self) -> dict:
        """获取实例池快照统计。"""
        with self._lock:
            total = len(self._slots)
            idle = sum(1 for s in self._slots.values() if s.status == InstanceStatus.IDLE)
            busy = sum(1 for s in self._slots.values() if s.status == InstanceStatus.BUSY)
            blocked = sum(1 for s in self._slots.values() if s.status == InstanceStatus.BLOCKED)
            offline = sum(1 for s in self._slots.values() if s.status == InstanceStatus.OFFLINE)
            return {
                "total": total,
                "idle": idle,
                "busy": busy,
                "blocked": blocked,
                "offline": offline,
                "slots": [
                    {
                        "vmindex": s.vmindex,
                        "name": s.name,
                        "status": s.status.value,
                        "current_target": s.current_target,
                        "current_job_id": s.current_job_id,
                        "blocked_reason": s.blocked_reason,
                    }
                    for s in self._slots.values()
                ],
            }

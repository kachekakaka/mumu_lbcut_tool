"""录播调度平台核心 SDK 与调用接口包。

导出组件：
- InstancePool: MuMu 实例在线状态查询与抖音复位
- InstanceSlot, InstanceStatus: 实例槽位与状态
- WorkerProcessHandle: Worker 独立子进程控制句柄与异步 NDJSON 事件监听
- WorkerLaunchSpec: Worker 启动规格参数
- ObservationDispatcher: 周期扫描、手动查询与启动复核的统一派发
"""
from src.orchestrator.models import (
    InstanceSlot,
    InstanceStatus,
    JobStatus,
    CommandStatus,
    WorkerLaunchSpec,
)
from src.orchestrator.instance_pool import InstancePool
from src.orchestrator.worker_process import WorkerProcessHandle
from src.orchestrator.room_scanner import ObservationDispatcher

__all__ = [
    "InstancePool",
    "InstanceSlot",
    "InstanceStatus",
    "JobStatus",
    "CommandStatus",
    "WorkerLaunchSpec",
    "WorkerProcessHandle",
    "ObservationDispatcher",
]

from src.orchestrator.storage import Store
from src.orchestrator.scheduler import Scheduler
__all__ += ["Store", "Scheduler"]

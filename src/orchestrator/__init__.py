"""录播调度平台核心 SDK 与调用接口包。

导出组件：
- InstancePool: MuMu 模拟器实例池调度与状态隔离
- InstanceSlot, InstanceStatus: 实例槽位与状态
- WorkerProcessHandle: Worker 独立子进程控制句柄与异步 NDJSON 事件监听
- WorkerLaunchSpec: Worker 启动规格参数
- RoomScanner, ScanResult: 高并发多直播间开播状态监测器
"""
from src.orchestrator.models import (
    InstanceSlot,
    InstanceStatus,
    JobStatus,
    WorkerLaunchSpec,
)
from src.orchestrator.instance_pool import InstancePool
from src.orchestrator.worker_process import WorkerProcessHandle
from src.orchestrator.room_scanner import RoomScanner, ScanResult

__all__ = [
    "InstancePool",
    "InstanceSlot",
    "InstanceStatus",
    "JobStatus",
    "WorkerLaunchSpec",
    "WorkerProcessHandle",
    "RoomScanner",
    "ScanResult",
]

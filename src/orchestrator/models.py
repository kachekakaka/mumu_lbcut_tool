"""调度平台核心数据模型与状态枚举。"""
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import time


class InstanceStatus(Enum):
    """模拟器实例在调度池中的状态。"""
    IDLE = "idle"            # Android 已启动且无任务，可承接任务
    BUSY = "busy"            # 正在录制任务中
    RESETTING = "resetting"  # 登录强阻断后正在冷启动抖音，不分配任务
    OFFLINE = "offline"      # Android 未启动、未发现或 ADB 不通；重新在线后自动回到空闲
    RECONCILE = "reconcile"  # 旧任务进程归属未确认，实例保留到人工对账


@dataclass
class InstanceSlot:
    """一次实例扫描的结果：MuMu 报告 Android 已启动才视为在线。"""
    vmindex: int
    name: str = ""
    status: InstanceStatus = InstanceStatus.IDLE
    raw_info: dict = field(default_factory=dict)


class JobStatus(Enum):
    """录播任务生命周期状态。"""
    PENDING = "pending"        # 等待分配可用实例
    RUNNING = "running"        # Worker 已拉起，正在录制
    STOPPING = "stopping"      # 正在请求优雅封口退出
    FINISHED = "finished"      # 正常结束（下播或达到总时长限制）
    FAILED = "failed"          # 异常失败终止
    OFFLINE = "offline"        # 目标确诊未开播


@dataclass
class WorkerLaunchSpec:
    """Worker 启动参数规格。"""
    vmindex: int
    target: str
    output_dir: Path
    window_mode: str = "bottom"
    window_pos_x: int | None = None
    window_pos_y: int | None = None
    segment_duration_sec: int = 1800
    total_duration_sec: int = 0
    stall_timeout_sec: float = 10.0
    heartbeat_interval_sec: float = 3.0
    navigation_timeout_sec: float = 300.0
    allow_unconfirmed_identity: bool = False
    target_kind: str = "auto"          # room_id / web_rid / auto（手工调试）
    web_rid: str | None = None
    expected_anchor_uid: str | None = None
    job_id: str | None = None
    event_log_path: Path | None = None
    log_path: Path | None = None

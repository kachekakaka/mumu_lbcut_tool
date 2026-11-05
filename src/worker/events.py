"""Worker NDJSON 标准事件流协议与发射器。"""
from dataclasses import asdict, dataclass
import json
import sys
import time
from typing import Any, TextIO
import uuid


@dataclass
class BaseWorkerEvent:
    event: str
    timestamp: float = 0.0
    mono_time: float = 0.0
    event_id: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()
        if not self.mono_time:
            self.mono_time = time.monotonic()
        if not self.event_id:
            self.event_id = uuid.uuid4().hex


@dataclass
class WorkerStartedEvent(BaseWorkerEvent):
    event: str = "worker_started"
    vmindex: int = 0
    target: str = ""
    pid: int = 0


@dataclass
class SegmentStartedEvent(BaseWorkerEvent):
    event: str = "segment_started"
    segment_index: int = 0
    file_path: str = ""
    target_duration_sec: int = 1800


@dataclass
class HeartbeatEvent(BaseWorkerEvent):
    event: str = "heartbeat"
    elapsed_sec: int = 0
    segment_elapsed_sec: int = 0
    file_size_mb: float = 0.0
    fps: float | None = None
    status: str = "recording"


@dataclass
class StallDetectedEvent(BaseWorkerEvent):
    event: str = "stall_detected"
    stall_duration_sec: float = 0.0
    action: str = "intent_refresh"


@dataclass
class StallHealedEvent(BaseWorkerEvent):
    event: str = "stall_healed"
    action: str = "intent_refresh"
    latency_sec: float = 0.0


@dataclass
class SegmentFinishedEvent(BaseWorkerEvent):
    event: str = "segment_finished"
    segment_index: int = 0
    file_path: str = ""
    duration_sec: int = 0
    file_size_mb: float = 0.0
    is_final: bool = False


@dataclass
class SegmentAbortedEvent(BaseWorkerEvent):
    event: str = "segment_aborted"
    segment_index: int = 0
    file_path: str = ""
    reason: str = "unknown"


@dataclass
class StreamEndedEvent(BaseWorkerEvent):
    event: str = "stream_ended"
    reason: str = "anchor_offline"
    exit_code: int = 2


@dataclass
class LoginBlockedEvent(BaseWorkerEvent):
    event: str = "login_blocked"
    reason: str = "auth_required"
    vmindex: int = 0


class WorkerEventEmitter:
    """负责将 Worker 内部生命周期事件以单行 JSON 格式流式输出至指定流（默认 stdout）。"""

    def __init__(self, stream: TextIO | None = None):
        self.stream = stream or sys.stdout

    def emit(self, event_obj: BaseWorkerEvent):
        """发射单行 JSON。"""
        payload = asdict(event_obj)
        line = json.dumps(payload, ensure_ascii=False)
        self.stream.write(line + "\n")
        self.stream.flush()

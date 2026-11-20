"""Worker NDJSON 标准事件流协议与发射器。"""
from collections import deque
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sys
import threading
import time
from typing import Callable, TextIO
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
    room_identity_status: str = "unconfirmed"
    video_motion: float | None = None


@dataclass
class StallDetectedEvent(BaseWorkerEvent):
    event: str = "stall_detected"
    stall_duration_sec: float = 0.0
    action: str = "intent_refresh"
    reason: str = ""


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


@dataclass
class WorkerStoppedEvent(BaseWorkerEvent):
    event: str = "worker_stopped"
    exit_code: int = 0
    reason: str = "completed"
    had_segment_errors: bool = False


@dataclass
class WorkerFailedEvent(BaseWorkerEvent):
    event: str = "worker_failed"
    exit_code: int = 30
    reason: str = "unknown"


@dataclass
class SegmentQualityWarningEvent(BaseWorkerEvent):
    event: str = "segment_quality_warning"
    segment_index: int = 0
    flag: str = "identity_unconfirmed"
    suspected_start_time: float = 0.0


@dataclass
class RoomIdentityCheckEvent(BaseWorkerEvent):
    event: str = 'room_identity_check'
    action: str = ''
    reason: str = ''
    generation_id: int = 0
    probe_id: int = 0
    observed_room_id: str | None = None
    observed_anchor_uid: str | None = None
    sample_started_at: float = 0.0
    sample_finished_at: float | None = None
    duration_sec: float = 0.0
    accepted: bool = True
    anomaly_detected_at: float | None = None
    stop_decided_at: float | None = None
    capture_stopped_at: float | None = None
    recovered_at: float | None = None


@dataclass
class RoomNavigationEvent(BaseWorkerEvent):
    event: str = "room_navigation"
    attempt: int = 0
    action: str = "observe"
    route: str = ""
    reason: str = ""
    observed_room_id: str | None = None
    observed_anchor_uid: str | None = None
    evidence_source: str = "unavailable"
    evidence_observed_at: float = 0.0
    phase: str = ""
    elapsed_sec: float = 0.0
    remaining_sec: float = 0.0
    stage_budget_sec: float = 0.0


class WorkerEventEmitter:
    """负责将 Worker 内部生命周期事件以单行 JSON 格式流式输出至指定流（默认 stdout）。

    读端消失（调度主控已退出）时写入会失败：此后事件改为追加到 fallback_path，
    并通知 on_stream_lost 让 Worker 平滑封口。管道缓冲中可能有已写出但主控来不及读取的
    事件，所以切换时先补写最近的事件；重放按 event_id 去重。
    """
    RECENT_LIMIT = 200

    def __init__(self, stream: TextIO | None = None, fallback_path: Path | str | None = None,
                 on_stream_lost: Callable[[], None] | None = None):
        self.stream = stream or sys.stdout
        self.context: dict = {}
        self.fallback_path = Path(fallback_path) if fallback_path else None
        self.on_stream_lost = on_stream_lost
        self.stream_lost = False
        self._fallback = None
        self._recent: deque[str] = deque(maxlen=self.RECENT_LIMIT)
        self._lock = threading.Lock()
        self._event_seq = 0

    def set_context(self, **values):
        with self._lock:
            if "run_id" in values and values["run_id"] != self.context.get("run_id"):
                self._event_seq = 0
                self._recent.clear()
            self.context.update(values)

    def emit(self, event_obj: BaseWorkerEvent, context=None):
        """发射单行 JSON。"""
        lost_now = False
        with self._lock:
            self._event_seq += 1
            payload = {**self.context, **(context or {}), **asdict(event_obj), "event_seq": self._event_seq}
            line = json.dumps(payload, ensure_ascii=False) + "\n"
            if not self.stream_lost:
                try:
                    self.stream.write(line)
                    self.stream.flush()
                    self._recent.append(line)
                    return
                except (OSError, ValueError):
                    self.stream_lost = lost_now = True
                    self._open_fallback()
            self._write_fallback(line)
        if lost_now and self.on_stream_lost:
            self.on_stream_lost()

    def _open_fallback(self):
        if not self.fallback_path:
            return
        try:
            with open(self.fallback_path, "ab") as raw:
                # 主控被强杀时最后一行可能不完整；补换行，不能把下一条事件接在残行后面。
                if raw.tell():
                    with open(self.fallback_path, "rb") as check:
                        check.seek(-1, 2)
                        if check.read(1) != b"\n":
                            raw.write(b"\n")
            self._fallback = open(self.fallback_path, "a", encoding="utf-8", buffering=1)
            for line in self._recent:
                self._fallback.write(line)
            self._fallback.flush()
        except OSError:
            if self._fallback is not None:
                try:
                    self._fallback.close()
                except OSError:
                    pass
                self._fallback = None

    def _write_fallback(self, line: str):
        if self._fallback is None:
            return
        try:
            self._fallback.write(line)
            self._fallback.flush()
        except OSError:
            try:
                self._fallback.close()
            except OSError:
                pass
            self._fallback = None

    def close(self):
        with self._lock:
            if self._fallback is not None:
                try:
                    self._fallback.close()
                except OSError:
                    pass
                self._fallback = None

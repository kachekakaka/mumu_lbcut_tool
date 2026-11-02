"""Worker NDJSON 事件系统单元测试。"""
import io
import json
import unittest
from src.worker.events import (
    WorkerEventEmitter,
    WorkerStartedEvent,
    SegmentStartedEvent,
    HeartbeatEvent,
    StallDetectedEvent,
    StallHealedEvent,
    SegmentFinishedEvent,
    StreamEndedEvent,
    LoginBlockedEvent,
)


class TestWorkerEvents(unittest.TestCase):
    def test_emit_ndjson_line(self):
        stream = io.StringIO()
        emitter = WorkerEventEmitter(stream=stream)

        emitter.emit(WorkerStartedEvent(vmindex=2, target="737141", pid=12345))
        emitter.emit(HeartbeatEvent(elapsed_sec=10, segment_elapsed_sec=10, file_size_mb=1.2, fps=29.5, status="recording"))
        emitter.emit(LoginBlockedEvent(reason="preflight_gate_failed", vmindex=2))

        lines = stream.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 3)

        data1 = json.loads(lines[0])
        self.assertEqual(data1["event"], "worker_started")
        self.assertEqual(data1["vmindex"], 2)

        data2 = json.loads(lines[1])
        self.assertEqual(data2["event"], "heartbeat")
        self.assertEqual(data2["fps"], 29.5)

        data3 = json.loads(lines[2])
        self.assertEqual(data3["event"], "login_blocked")
        self.assertEqual(data3["reason"], "preflight_gate_failed")
        self.assertEqual(data3["vmindex"], 2)


if __name__ == "__main__":
    unittest.main()

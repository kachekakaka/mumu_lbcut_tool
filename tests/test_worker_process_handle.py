"""测试 Worker 独立子进程控制句柄 (WorkerProcessHandle)。"""
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from src.orchestrator.models import WorkerLaunchSpec
from src.orchestrator.worker_process import WorkerProcessHandle


class TestWorkerProcessHandle(unittest.TestCase):
    @patch("subprocess.Popen")
    def test_worker_handle_start_and_drain_events(self, mock_popen):
        # 模拟 stdout 输出流
        ndjson_output = (
            '{"event": "worker_started", "timestamp": 100.0, "vmindex": 2}\n'
            '{"event": "heartbeat", "timestamp": 103.0, "current_segment_index": 1}\n'
            '{"event": "segment_finished", "timestamp": 125.0, "segment_index": 1, "file_path": "seg_001.mp4"}\n'
            '{"event": "stream_ended", "timestamp": 130.0}\n'
        )
        mock_proc = MagicMock()
        mock_proc.stdout = ndjson_output.splitlines(keepends=True)
        mock_proc.stderr = ["some debug log\n"]
        mock_proc.poll.return_value = 0
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        events_received = []
        def _cb(ev):
            events_received.append(ev)

        spec = WorkerLaunchSpec(
            vmindex=2,
            target="room_test",
            output_dir=Path("./out"),
            window_mode="bottom",
            window_pos_x=50,
            window_pos_y=50,
        )
        handle = WorkerProcessHandle(spec=spec, on_event=_cb)
        handle.start()
        handle.wait(timeout=2.0)

        self.assertEqual(len(events_received), 4)
        self.assertEqual(handle.finished_segments, ["seg_001.mp4"])
        self.assertIsNotNone(handle.latest_heartbeat)
        self.assertEqual(handle.latest_heartbeat["current_segment_index"], 1)
        self.assertIsNotNone(handle.terminal_event)
        self.assertEqual(handle.terminal_event["event"], "stream_ended")


if __name__ == "__main__":
    unittest.main()

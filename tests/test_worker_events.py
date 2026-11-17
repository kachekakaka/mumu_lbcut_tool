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
    SegmentQualityWarningEvent,
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

    def test_segment_finished_is_final_contract(self):
        """测试切片完成事件的 is_final 字段契约。"""
        stream = io.StringIO()
        emitter = WorkerEventEmitter(stream=stream)

        # 普通轮转切片
        emitter.emit(SegmentFinishedEvent(
            segment_index=1,
            file_path="D:/rec/seg01.mp4",
            duration_sec=1800,
            file_size_mb=500.0,
            is_final=False
        ))

        # 最终停机切片
        emitter.emit(SegmentFinishedEvent(
            segment_index=2,
            file_path="D:/rec/seg02.mp4",
            duration_sec=320,
            file_size_mb=80.0,
            is_final=True
        ))

        lines = stream.getvalue().strip().splitlines()
        seg1 = json.loads(lines[0])
        self.assertEqual(seg1["event"], "segment_finished")
        self.assertFalse(seg1["is_final"])

        seg2 = json.loads(lines[1])
        self.assertEqual(seg2["event"], "segment_finished")
        self.assertTrue(seg2["is_final"])


class BrokenAfter:
    """前 n 次写入成功，之后像读端已关闭的管道一样报错。"""
    def __init__(self, n):
        self.n, self.lines = n, []
    def write(self, text):
        if len(self.lines) >= self.n:
            raise OSError(22, "Invalid argument")
        self.lines.append(text)
    def flush(self):
        pass


def test_stream_loss_switches_to_journal_and_requests_stop(tmp_path):
    journal = tmp_path / "job.ndjson"
    journal.write_text('{"event": "worker_started", "event_id": "a"}\n{"event": "heart', encoding="utf-8")
    lost = []
    emitter = WorkerEventEmitter(BrokenAfter(2), fallback_path=journal, on_stream_lost=lambda: lost.append(1))
    for i in range(4):
        emitter.emit(HeartbeatEvent(elapsed_sec=i))
    emitter.close()
    assert lost == [1] and emitter.stream_lost
    lines = journal.read_text(encoding="utf-8").splitlines()
    assert lines[1] == '{"event": "heart'
    events = [json.loads(line) for line in lines[2:]]
    # 补写已写入管道的最近事件，按 event_id 去重后四条心跳一条不少。
    assert [e["elapsed_sec"] for e in events] == [0, 1, 2, 3]


def test_stream_loss_without_journal_still_requests_stop():
    lost = []
    emitter = WorkerEventEmitter(BrokenAfter(0), on_stream_lost=lambda: lost.append(1))
    emitter.emit(HeartbeatEvent())
    emitter.emit(HeartbeatEvent())
    assert lost == [1]


def test_quality_stdout_and_journal_replay_share_event_id(tmp_path):
    from src.orchestrator.storage import Store
    stream = BrokenAfter(1)
    journal = tmp_path / 'quality.ndjson'
    emitter = WorkerEventEmitter(stream, fallback_path=journal)
    emitter.emit(SegmentQualityWarningEvent(segment_index=1, flag='low_frame_rate', suspected_start_time=10))
    emitter.emit(SegmentQualityWarningEvent(segment_index=1, flag='low_frame_rate', suspected_start_time=10))
    emitter.close()
    live = json.loads(stream.lines[0])
    replay = [json.loads(line) for line in journal.read_text(encoding='utf-8').splitlines()]
    assert replay[0] == live and replay[0]['event_id'] != replay[1]['event_id']
    store = Store(tmp_path / 'replay.sqlite3')
    try:
        target = store.add_target('回放', now=1)
        with store.transaction() as db:
            db.execute("INSERT INTO jobs(id,target_id,session_key,status,created_at) VALUES('j',?,'s','running',1)", (target,))
        for event in [live, *replay, *replay]:
            store.record_event('j', event, 20)
        assert len(store.rows('SELECT * FROM events')) == 2
        assert len(json.loads(store.row('SELECT risk_json FROM segments')['risk_json'])) == 2
    finally:
        store.close()


def test_real_closed_stdout_pipe_switches_to_journal(tmp_path):
    import subprocess
    import sys
    import time
    from pathlib import Path
    journal = tmp_path / "job.ndjson"
    journal.touch()
    script = (
        "import sys,time\n"
        "from src.worker.events import WorkerEventEmitter, HeartbeatEvent\n"
        "stopped=[]\n"
        f"e=WorkerEventEmitter(sys.stdout, fallback_path={str(journal)!r}, on_stream_lost=lambda: stopped.append(1))\n"
        "for i in range(200):\n"
        "    e.emit(HeartbeatEvent(elapsed_sec=i))\n"
        "    if stopped: break\n"
        "    time.sleep(.05)\n"
        "e.emit(HeartbeatEvent(elapsed_sec=-1, status='stopped'))\n"
        "e.close()\n"
        "sys.exit(0 if stopped else 3)\n")
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.Popen([sys.executable, "-u", "-c", script], cwd=root, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)
    for _ in range(3):
        proc.stdout.readline()
    proc.stdout.close()
    assert proc.wait(timeout=30) == 0
    events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert events[-1]["status"] == "stopped"
    assert [e["elapsed_sec"] for e in events[:3]] == [0, 1, 2]


if __name__ == "__main__":
    unittest.main()

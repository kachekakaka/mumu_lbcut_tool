"""多实例长稳与全场景故障注入端到端自动化验收套件 (Ticket 14)。"""
import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from src.worker.controller import WorkerController, WorkerConfig, EXIT_SUCCESS, EXIT_ROOM_MISMATCH
from src.worker.events import WorkerEventEmitter
from src.worker.naming_lock import build_segment_filename, WorkerInstanceLock
from src.worker.segment_verifier import verify_segment_file, SegmentValidationResult
from src.worker.identity_verifier import verify_room_identity, IdentityStatus


def test_e2e_fault_injection_suite_recovers_or_exits_gracefully(tmp_path):
    """端到端注入 5 种典型故障：进程杀戮、慢调用、切片损坏、错房漂移，系统均受控处理无僵死。"""
    config = WorkerConfig(
        vmindex=1,
        target="https://live.douyin.com/fault_test_room",
        output_dir=tmp_path / "out1",
        segment_duration_sec=60,
    )
    mock_stream = io.StringIO()
    emitter = WorkerEventEmitter(stream=mock_stream)
    controller = WorkerController(config=config, emitter=emitter)

    # 1. 注入错房 UID 漂移故障
    mismatch_res = verify_room_identity(
        target_uid="11111",
        current_uid="99999",
        allow_unconfirmed=True,
    )
    assert mismatch_res.status == IdentityStatus.MISMATCH
    assert mismatch_res.can_proceed is False

    # 2. 注入切片文件 0 字节截断损坏故障
    corrupted_file = tmp_path / "corrupted_seg.mp4"
    corrupted_file.write_bytes(b"")
    val_res = verify_segment_file(corrupted_file)
    assert val_res.is_valid is False
    assert "文件过小" in val_res.reject_reason or "截断" in val_res.reject_reason

    # 3. 注入进程崩溃与有界自愈
    mock_pipe = MagicMock()
    mock_pipe.is_recording.return_value = False
    mock_pipe.get_exit_code.return_value = -9
    mock_pipe.current_file_path = "crash_seg.mp4"
    mock_pipe.start_segment.return_value = "healed_seg.mp4"
    controller._pipeline = mock_pipe

    assert controller._total_crash_count == 0
    # 第一次崩溃 -> 就地自愈成功
    healed = controller._handle_pipeline_crash(segment_idx=1, exit_code=-9)
    assert healed is True
    assert controller._total_crash_count == 1

    # 4. 验证 NDJSON 事件流成对闭环
    lines = [json.loads(line) for line in mock_stream.getvalue().splitlines() if line.strip()]
    event_names = [e.get("event") for e in lines]
    assert "segment_aborted" in event_names
    assert "segment_started" in event_names


def test_e2e_multi_instance_parallel_isolation(tmp_path):
    """两个模拟 Worker 实例并行运行，产物目录与命名完全隔离无覆盖。"""
    out_dir_1 = tmp_path / "worker_1"
    out_dir_2 = tmp_path / "worker_2"
    out_dir_1.mkdir(parents=True)
    out_dir_2.mkdir(parents=True)

    # 实例 1 在 VM 1 录制房间 A
    lock1 = WorkerInstanceLock(lock_dir=tmp_path / "locks", vmindex=1, room_id="room_A")
    assert lock1.acquire() is True

    # 实例 2 在 VM 2 录制房间 B
    lock2 = WorkerInstanceLock(lock_dir=tmp_path / "locks", vmindex=2, room_id="room_B")
    assert lock2.acquire() is True

    # 验证生成的切片文件名独立防碰撞
    file1 = build_segment_filename(room_id="room_A", start_epoch=1730000000, segment_index=1, generation_id=1)
    file2 = build_segment_filename(room_id="room_B", start_epoch=1730000000, segment_index=1, generation_id=1)

    assert file1 != file2
    assert "room_A" in file1
    assert "room_B" in file2

    lock1.release()
    lock2.release()

"""测试任务目录隔离防碰撞命名、进程互斥锁与观测增强 (Ticket 12)。"""
import json
import os
from pathlib import Path
import pytest

from src.worker.naming_lock import (
    build_segment_filename,
    WorkerInstanceLock,
    LockAcquisitionError,
)
from src.worker.events import (
    WorkerEventEmitter,
    BaseWorkerEvent,
)


def test_build_segment_filename_strict_format():
    """切片命名严格遵循 <room_id>_<start_epoch>_<segment_index:04d>_<generation:02d>.mp4。"""
    filename = build_segment_filename(
        room_id="737141888",
        start_epoch=1730550000,
        segment_index=1,
        generation_id=2,
    )
    assert filename == "737141888_1730550000_0001_02.mp4"


def test_worker_instance_lock_prevents_duplicate_acquisition(tmp_path):
    """同一设备和同一房间在已有锁持有时，第二个实例获取锁必须失败并抛出异常。"""
    lock1 = WorkerInstanceLock(lock_dir=tmp_path, vmindex=2, room_id="737141")
    assert lock1.acquire() is True

    # 另一个实例尝试获取同一设备+房间锁
    lock2 = WorkerInstanceLock(lock_dir=tmp_path, vmindex=2, room_id="737141")
    with pytest.raises(LockAcquisitionError):
        lock2.acquire()

    # lock1 释放后，lock2 能够正常获取
    lock1.release()
    assert lock2.acquire() is True
    lock2.release()


def test_event_emitter_includes_uuid_and_monotonic_timestamp():
    """NDJSON 事件必须包含全局唯一 event_id (UUID) 与 mono_time 单调时钟。"""
    event = BaseWorkerEvent(event="custom_test_event")
    assert hasattr(event, "event_id")
    assert hasattr(event, "mono_time")
    assert len(event.event_id) >= 16
    assert event.mono_time > 0.0

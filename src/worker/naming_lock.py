"""实例独占使用操作系统锁；任务目录隔离承载产物防碰撞。"""
import logging
import os
from pathlib import Path

from src.mvp.config import DEV_DIR

logger = logging.getLogger(__name__)


class LockAcquisitionError(RuntimeError):
    pass


def get_shared_lock_dir() -> Path:
    """稳定共享目录，不随某个 Worker 的 TEMP 或输出目录变化。"""
    override = os.environ.get("MUMU_LBCUT_INSTANCE_LOCK_DIR")
    path = Path(override) if override else DEV_DIR / "runtime" / "instance-locks"
    if not path.is_absolute():
        raise ValueError("实例锁目录需要绝对路径")
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_segment_filename(room_id: str, start_epoch: int, segment_index: int,
                           generation_id: int) -> str:
    clean_room = "".join(c for c in room_id if c.isalnum() or c in ("_", "-"))
    return f"{clean_room}_{start_epoch}_{segment_index:04d}_{generation_id:02d}.mp4"


class WorkerInstanceLock:
    """同一安装根目录下按 VM 独占；崩溃时由 OS 释放，不删除他人的锁文件。"""
    def __init__(self, lock_dir: Path | None = None, vmindex: int = 0, room_id: str = ""):
        self.lock_dir = Path(lock_dir) if lock_dir is not None else get_shared_lock_dir()
        self.vmindex, self.room_id = vmindex, room_id
        self.lock_file = self.lock_dir / f"worker_vm{vmindex}.lock"
        self._file = None

    def acquire(self) -> bool:
        if self._file is not None:
            raise LockAcquisitionError("此句柄已经持有实例锁")
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        stream = self.lock_file.open("a+b")
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise LockAcquisitionError(f"模拟器 VM {self.vmindex} 已被其他 Worker 独占") from exc
        self._file = stream
        logger.info("获取实例 OS 锁: VM %s，PID %s", self.vmindex, os.getpid())
        return True

    def release(self) -> None:
        stream, self._file = self._file, None
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
        # 不删除锁文件：删除后重建可让两个进程锁定不同文件对象。

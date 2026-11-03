"""防碰撞文件命名体系与多实例文件互斥锁 (Ticket 12)。"""
import logging
import os
from pathlib import Path
import time
from typing import Optional

logger = logging.getLogger("NamingLock")


class LockAcquisitionError(RuntimeError):
    """文件锁被占用异常。"""
    pass


def build_segment_filename(
    room_id: str,
    start_epoch: int,
    segment_index: int,
    generation_id: int,
) -> str:
    """构建全局唯一的防碰撞切片文件名。

    格式: <room_id>_<start_epoch>_<segment_index:04d>_<generation:02d>.mp4
    """
    clean_room = "".join(c for c in room_id if c.isalnum() or c in ("_", "-"))
    return f"{clean_room}_{start_epoch}_{segment_index:04d}_{generation_id:02d}.mp4"


class WorkerInstanceLock:
    """基于文件锁的多实例防并发抢占锁。

    阻止对同一模拟器设备、同一直播间启动第二个录制进程。
    """

    def __init__(self, lock_dir: Path, vmindex: int, room_id: str):
        self.lock_dir = lock_dir
        self.vmindex = vmindex
        self.room_id = room_id
        clean_room = "".join(c for c in room_id if c.isalnum() or c in ("_", "-"))
        self.lock_file = lock_dir / f"worker_vm{vmindex}_{clean_room}.lock"
        self._lock_fd: Optional[int] = None

    def acquire(self) -> bool:
        """尝试获取锁，若已被占用则抛出 LockAcquisitionError。"""
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        try:
            # 使用 O_CREAT | O_EXCL 原子创建锁文件
            flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
            self._lock_fd = os.open(str(self.lock_file), flags)
            # 写入当前 PID
            pid_str = f"{os.getpid()}\n"
            os.write(self._lock_fd, pid_str.encode("utf-8"))
            logger.info(f"成功获取 Worker 进程锁: {self.lock_file.name} (PID: {os.getpid()})")
            return True
        except FileExistsError:
            # 锁文件已存在，检查持有者进程是否存活
            try:
                with open(self.lock_file, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                held_pid = int(content) if content.isdigit() else None
            except Exception:
                held_pid = None

            raise LockAcquisitionError(
                f"实例锁被占用: 设备 VM {self.vmindex} / 房间 {self.room_id} 已在运行中 (持有 PID: {held_pid})，拒绝重复启动！"
            )

    def release(self) -> None:
        """释放锁文件。"""
        if self._lock_fd is not None:
            try:
                os.close(self._lock_fd)
            except Exception:
                pass
            self._lock_fd = None

        if self.lock_file.exists():
            try:
                self.lock_file.unlink()
                logger.info(f"已释放 Worker 进程锁: {self.lock_file.name}")
            except Exception as e:
                logger.warning(f"删除锁文件失败: {e}")

"""防碰撞文件命名体系与多实例文件互斥锁 (Ticket 12)。"""
import logging
import os
from pathlib import Path
import tempfile
import time
from typing import Optional

logger = logging.getLogger("NamingLock")


def is_pid_alive(pid: int) -> bool:
    """跨平台检查进程 PID 是否仍处于存活状态。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle == 0:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


def get_shared_lock_dir() -> Path:
    """获取所有 Worker 共享的实例互斥锁目录。"""
    shared_dir = Path(tempfile.gettempdir()) / "mumu_worker_locks"
    shared_dir.mkdir(parents=True, exist_ok=True)
    return shared_dir


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

    阻止对同一模拟器设备（vmindex）启动第二个录制进程。
    锁仅由模拟器/设备标识建立，不因房间或输出目录不同而产生独立锁。
    """

    def __init__(self, lock_dir: Path | None = None, vmindex: int = 0, room_id: str = ""):
        self.lock_dir = lock_dir if lock_dir is not None else get_shared_lock_dir()
        self.vmindex = vmindex
        self.room_id = room_id
        # 严格按 VM 实例标识命名，同一 VM 只能存在一把实例锁
        self.lock_file = self.lock_dir / f"worker_vm{vmindex}.lock"
        self._lock_fd: Optional[int] = None
        self._acquired_pid: Optional[int] = None

    def acquire(self) -> bool:
        """尝试获取锁，若已被活跃任务占用则抛出 LockAcquisitionError。"""
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        my_pid = os.getpid()

        for attempt in range(2):
            try:
                # 使用 O_CREAT | O_EXCL 原子创建锁文件
                flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
                self._lock_fd = os.open(str(self.lock_file), flags)
                # 写入当前 PID 与时间戳
                pid_str = f"{my_pid}\n"
                os.write(self._lock_fd, pid_str.encode("utf-8"))
                self._acquired_pid = my_pid
                logger.info(f"成功获取 Worker 进程锁: {self.lock_file.name} (PID: {my_pid})")
                return True
            except FileExistsError:
                # 锁文件已存在，检查持有者进程是否存活
                held_pid = None
                try:
                    with open(self.lock_file, "r", encoding="utf-8") as f:
                        content = f.read().strip()
                    held_pid = int(content) if content.isdigit() else None
                except Exception:
                    pass

                # 若持有者进程仍然存活（或者是自己重复 acquire）
                if held_pid and is_pid_alive(held_pid):
                    raise LockAcquisitionError(
                        f"实例锁被占用: 模拟器设备 VM {self.vmindex} 已有活跃 Worker 运行 (持有 PID: {held_pid})，拒绝重复启动！"
                    )

                # 持有者为死进程（陈旧锁），执行所有权回收并重试
                logger.warning(
                    f"检测到模拟器 VM {self.vmindex} 残留陈旧锁 (PID {held_pid} 已消亡)，正在安全接管回收..."
                )
                try:
                    self.lock_file.unlink()
                except Exception:
                    pass

        raise LockAcquisitionError(
            f"获取模拟器设备 VM {self.vmindex} 实例排他锁失败"
        )

    def release(self) -> None:
        """释放锁文件。仅当本进程持有锁时才安全清理，避免误删他人锁。"""
        if self._lock_fd is not None:
            try:
                os.close(self._lock_fd)
            except Exception:
                pass
            self._lock_fd = None

        if self.lock_file.exists():
            try:
                # 校验锁归属，避免误删
                with open(self.lock_file, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if not content or content == str(self._acquired_pid) or content == str(os.getpid()):
                    self.lock_file.unlink()
                    logger.info(f"已释放 Worker 进程锁: {self.lock_file.name}")
            except Exception as e:
                logger.warning(f"删除锁文件失败: {e}")

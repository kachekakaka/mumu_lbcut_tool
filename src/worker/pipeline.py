"""基于 scrcpy 数字流的 MP4 流式切片录制管道。"""
from pathlib import Path
import logging
import os
import signal
import subprocess
import time

from src.mvp.config import SCRCPY_DIR, get_adb_exe
from src.worker.scrcpy_probe import PipelineLogDrainer, parse_scrcpy_version_output, validate_scrcpy_policy

logger = logging.getLogger(__name__)


class WorkerPipeline:
    """管理单路直播的流式切片录制与时间戳物理归零重置。"""

    def __init__(
        self,
        serial: str,
        output_dir: Path,
        scrcpy_exe: Path | None = None,
        adb_exe: Path | None = None,
        max_fps: int = 30,
        bit_rate: str = "4M",
        max_size: int = 1280,
    ):
        self.serial = serial
        self.output_dir = output_dir
        self.scrcpy_exe = scrcpy_exe or (SCRCPY_DIR / "scrcpy.exe")
        self.adb_exe = adb_exe or get_adb_exe()
        self.max_fps = max_fps
        self.bit_rate = bit_rate
        self.max_size = max_size

        self.current_segment_index: int = 0
        self.current_file_path: Path | None = None
        self._proc: subprocess.Popen | None = None
        self._log_drainer: PipelineLogDrainer | None = None
        self._segment_start_time: float = 0.0
        self._last_logs: list[str] = []

    def is_recording(self) -> bool:
        """检查当前切片录制进程是否仍在运行。"""
        if self._proc is None:
            return False
        return self._proc.poll() is None

    def get_exit_code(self) -> int | None:
        """获取录制子进程的退出码（若尚未退出返回 None）。"""
        if self._proc is None:
            return None
        return self._proc.poll()

    def start_segment(self, segment_index: int) -> Path:
        """启动一个指定索引的独立切片 MP4 录制。"""
        if self.is_recording():
            self.stop_segment()
        else:
            if self._log_drainer:
                self._log_drainer.join(timeout=1.0)
                self._log_drainer = None
            time.sleep(1.0)

        self.current_segment_index = segment_index
        self._last_logs.clear()
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"record_{timestamp}_seg_{segment_index:03d}.mp4"
        self.current_file_path = self.output_dir / filename
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # 启动 scrcpy:
        # --audio-source=playback: 内部数字音频流
        # --audio-codec=aac
        # --video-codec=h264
        # 强制依赖系统硬件 PTS 单调对齐
        cmd = [
            str(self.scrcpy_exe),
            "-s", self.serial,
            f"--record={self.current_file_path}",
            "--no-playback",
            "--audio-source=playback",
            "--audio-codec=aac",
            "--video-codec=h264",
            "--require-audio",
            "--display-id=0",
            f"--max-fps={self.max_fps}",
            f"--video-bit-rate={self.bit_rate}",
            f"--max-size={self.max_size}",
        ]

        env = os.environ.copy()
        if self.adb_exe and self.adb_exe.exists():
            env["ADB"] = str(self.adb_exe)
            env["PATH"] = str(self.adb_exe.parent) + os.path.pathsep + env.get("PATH", "")

        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self._proc = subprocess.Popen(
            cmd,
            env=env,
            creationflags=creationflags,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        # 挂接防背压排空器
        def _handle_scrcpy_log(line: str):
            self._last_logs.append(line)
            if len(self._last_logs) > 30:
                self._last_logs.pop(0)
            logger.info(f"[scrcpy] {line}")

        self._log_drainer = PipelineLogDrainer(
            self._proc,
            on_log_line=_handle_scrcpy_log
        )
        self._log_drainer.start()
        self._segment_start_time = time.time()
        time.sleep(1.5)  # 建立连接缓冲

        if not self.is_recording():
            detail = "\n".join(self._last_logs[-5:]) if self._last_logs else "无日志输出"
            raise RuntimeError(f"切片 {segment_index} 启动失败 (code {self._proc.returncode}):\n{detail}")

        logger.info(f"已开启新切片 #{segment_index}: {self.current_file_path.name}")
        return self.current_file_path

    def stop_segment(self, timeout_sec: int = 8) -> bool:
        """优雅停止当前切片，完成 MP4 尾部索引封装。"""
        if not self.is_recording():
            return True

        logger.info(f"正在封口切片 #{self.current_segment_index}...")
        try:
            if os.name == "nt":
                self._proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self._proc.send_signal(signal.SIGINT)
            self._proc.wait(timeout=timeout_sec)
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning(f"等待切片封装超时 ({e})，强制终止...")
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()

        if self._log_drainer:
            self._log_drainer.join(timeout=1.0)
            self._log_drainer = None

        # 等待 1 秒确保 Android 侧 scrcpy-server 彻底释放显存句柄
        time.sleep(1.0)

        return self.current_file_path is not None and self.current_file_path.exists()

    def get_current_file_size_mb(self) -> float:
        """获取当前切片文件大小。"""
        if self.current_file_path and self.current_file_path.exists():
            return self.current_file_path.stat().st_size / (1024 * 1024)
        return 0.0

    def get_current_segment_elapsed(self) -> float:
        """当前切片已录制秒数。"""
        if self._segment_start_time > 0:
            return time.time() - self._segment_start_time
        return 0.0

    def teardown(self):
        """紧急销毁进程。"""
        if self.is_recording():
            self.stop_segment(timeout_sec=3)

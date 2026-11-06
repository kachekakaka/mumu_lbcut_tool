"""基于 scrcpy 数字流的 MP4 流式切片录制管道。"""
from pathlib import Path
import logging
import os
import signal
import subprocess
import time
from collections import deque

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
        port: str | None = None,
        max_fps: int | None = None,
        bit_rate: str = "4M",
        max_size: int = 1280,
    ):
        self.serial = serial
        self.output_dir = output_dir
        self.scrcpy_exe = scrcpy_exe or (SCRCPY_DIR / "scrcpy.exe")
        self.adb_exe = adb_exe or get_adb_exe()
        self.port = port
        self.max_fps = max_fps
        self.bit_rate = bit_rate
        self.max_size = max_size

        self.current_segment_index: int = 0
        self.current_file_path: Path | None = None
        self._proc: subprocess.Popen | None = None
        self._log_drainer: PipelineLogDrainer | None = None
        self._segment_start_time: float = 0.0
        self._last_logs = deque(maxlen=30)
        self.wait_hook = None
        self.startup_timeout_sec = 10.0

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

        self.current_segment_index = segment_index
        self._last_logs.clear()
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"record_{timestamp}_seg_{segment_index:03d}.mp4"
        self.current_file_path = self.output_dir.resolve() / filename
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if self.current_file_path.exists():
            raise FileExistsError(f"拒绝覆盖录制文件: {self.current_file_path}")

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
            f"--video-bit-rate={self.bit_rate}",
            f"--max-size={self.max_size}",
        ]
        if self.max_fps and self.max_fps > 0:
            cmd.append(f"--max-fps={self.max_fps}")
        if self.port:
            cmd.append(f"--port={self.port}")

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
        # 每个进程独立日志缓冲，旧线程不能写入新一代诊断信息。
        logs = deque(maxlen=30)
        self._last_logs = logs
        def _handle_scrcpy_log(line: str):
            logs.append(line)
            logger.info("[scrcpy] %s", line)
        self._log_drainer = PipelineLogDrainer(self._proc, on_log_line=_handle_scrcpy_log)
        self._log_drainer.start()
        self._segment_start_time = time.monotonic()
        deadline = self._segment_start_time + self.startup_timeout_sec
        previous_size = 0
        try:
            while time.monotonic() < deadline:
                if self.wait_hook and self.wait_hook() is False:
                    raise InterruptedError("录制启动收到停止请求")
                if not self.is_recording():
                    detail = "\n".join(list(logs)[-5:])
                    raise RuntimeError(f"切片 {segment_index} 启动退出 (code {self._proc.returncode}): {detail}")
                size = self.current_file_path.stat().st_size if self.current_file_path.exists() else 0
                if previous_size > 0 and size > max(previous_size, 1024):
                    break
                previous_size = size
                time.sleep(0.1)
            else:
                raise TimeoutError("录制进程存活但未确认媒体文件持续增长")
        except Exception:
            self.stop_segment(timeout_sec=3)
            raise

        logger.info(f"已开启新切片 #{segment_index}: {self.current_file_path.name}")
        return self.current_file_path

    def stop_segment(self, timeout_sec: int = 8) -> bool:
        """有界停止：短轮询保持停止响应和状态可见，回收确认属于本管线的进程。"""
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
            except OSError:
                proc.terminate()
            deadline = time.monotonic() + timeout_sec
            while proc.poll() is None and time.monotonic() < deadline:
                if self.wait_hook:
                    self.wait_hook()
                time.sleep(0.1)
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
        if self._log_drainer:
            self._log_drainer.join(timeout=1.0)
            self._log_drainer = None
        return self.current_file_path is not None and self.current_file_path.exists()

    def get_current_file_size_mb(self) -> float:
        """获取当前切片文件大小。"""
        if self.current_file_path and self.current_file_path.exists():
            return self.current_file_path.stat().st_size / (1024 * 1024)
        return 0.0

    def get_current_segment_elapsed(self) -> float:
        """当前切片已录制秒数。"""
        if self._segment_start_time > 0:
            return time.monotonic() - self._segment_start_time
        return 0.0

    def teardown(self):
        """紧急销毁进程。"""
        self.stop_segment(timeout_sec=3)


class DualPipelineManager:
    """管理 Primary 与 Secondary 两个交替录制管线，实现无缝秒级交叠轮转 (ADR 0009)。"""

    def __init__(
        self,
        serial: str,
        output_dir: Path,
        scrcpy_exe: Path | None = None,
        adb_exe: Path | None = None,
        max_fps: int | None = None,
        bit_rate: str = "4M",
        max_size: int = 1280,
        port_a: str = "27183:27190",
        port_b: str = "27191:27200",
    ):
        self.serial = serial
        self.output_dir = output_dir
        self.scrcpy_exe = scrcpy_exe
        self.adb_exe = adb_exe
        self.max_fps = max_fps
        self.bit_rate = bit_rate
        self.max_size = max_size
        self.ports = [port_a, port_b]
        self._active_slot = 0  # 0 为 pipe_a, 1 为 pipe_b

        self.pipe_a = WorkerPipeline(
            serial=serial,
            output_dir=output_dir,
            scrcpy_exe=scrcpy_exe,
            adb_exe=adb_exe,
            port=port_a,
            max_fps=max_fps,
            bit_rate=bit_rate,
            max_size=max_size,
        )
        self.pipe_b = WorkerPipeline(
            serial=serial,
            output_dir=output_dir,
            scrcpy_exe=scrcpy_exe,
            adb_exe=adb_exe,
            port=port_b,
            max_fps=max_fps,
            bit_rate=bit_rate,
            max_size=max_size,
        )

    def get_active_pipeline(self) -> WorkerPipeline:
        """获取当前主录制管线。"""
        return self.pipe_a if self._active_slot == 0 else self.pipe_b

    def get_secondary_pipeline(self) -> WorkerPipeline:
        """获取当前备用/交叠管线。"""
        return self.pipe_b if self._active_slot == 0 else self.pipe_a

    def start_initial_segment(self, segment_index: int) -> Path:
        """启动第一段主切片录制。"""
        active = self.get_active_pipeline()
        return active.start_segment(segment_index)

    def start_segment(self, segment_index: int) -> Path:
        """兼容单管线接口，启动主切片。"""
        return self.start_initial_segment(segment_index)

    def start_overlapping_segment(self, segment_index: int) -> Path:
        """在交叠窗口期启动次管线，与主管线并行捕获。"""
        secondary = self.get_secondary_pipeline()
        return secondary.start_segment(segment_index)

    def promote_secondary(self) -> WorkerPipeline:
        """提升次管线为主管线，并返回已录满的旧主管线（以便调用方执行封口与校验）。"""
        old_primary = self.get_active_pipeline()
        self._active_slot = 1 - self._active_slot
        return old_primary

    def is_recording(self) -> bool:
        """当前主管线是否在录制。"""
        return self.get_active_pipeline().is_recording()

    def stop_segment(self, timeout_sec: int = 8) -> bool:
        """优雅停止当前主管线切片。"""
        return self.get_active_pipeline().stop_segment(timeout_sec=timeout_sec)

    def get_exit_code(self) -> int | None:
        return self.get_active_pipeline().get_exit_code()

    @property
    def current_segment_index(self) -> int:
        return self.get_active_pipeline().current_segment_index

    @property
    def current_file_path(self) -> Path | None:
        return self.get_active_pipeline().current_file_path

    def get_current_file_size_mb(self) -> float:
        return self.get_active_pipeline().get_current_file_size_mb()

    def get_current_segment_elapsed(self) -> float:
        return self.get_active_pipeline().get_current_segment_elapsed()

    def teardown(self):
        """紧急销毁所有管线进程。"""
        self.pipe_a.teardown()
        self.pipe_b.teardown()

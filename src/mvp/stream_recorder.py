"""基于 scrcpy 数字流管线的音视频切片录制器。"""
from pathlib import Path
import logging
import os
import signal
import subprocess
import time
import shutil

from src.mvp.config import SCRCPY_DIR, get_adb_exe

logger = logging.getLogger(__name__)


class StreamRecorder:
    """管理针对单个 Android 实例的后台无窗口独立音视频录制。"""

    def __init__(
        self,
        serial: str,
        output_path: Path,
        scrcpy_exe: Path | None = None,
        adb_exe: Path | None = None,
        max_fps: int = 30,
        bit_rate: str = "4M",
        max_size: int = 1280,
    ):
        self.serial = serial
        self.output_path = output_path
        self.scrcpy_exe = scrcpy_exe or (SCRCPY_DIR / "scrcpy.exe")
        self.adb_exe = adb_exe or get_adb_exe()
        self.max_fps = max_fps
        self.bit_rate = bit_rate
        self.max_size = max_size
        self._proc: subprocess.Popen | None = None
        self._start_time: float | None = None

    def is_recording(self) -> bool:
        """检查当前录制子进程是否仍在活跃运行。"""
        if self._proc is None:
            return False
        return self._proc.poll() is None

    def start(self) -> bool:
        """启动 scrcpy 后台录制。"""
        if self.is_recording():
            logger.warning(f"设备 {self.serial} 的录制任务已在运行中。")
            return True

        if not self.scrcpy_exe.exists():
            raise FileNotFoundError(f"未找到 scrcpy.exe: {self.scrcpy_exe}")

        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        if self.output_path.exists():
            try:
                self.output_path.unlink()
            except OSError:
                pass

        # 组装 scrcpy 参数：
        # --record: 录制到文件
        # --no-playback: 不弹出 GUI 窗口
        # --audio-source=playback: 录制系统内部音频 (Android 12+)
        # --audio-codec=aac: MP4 标准音频编码
        # --video-codec=h264: MP4 兼容性最佳
        cmd = [
            str(self.scrcpy_exe),
            "-s", self.serial,
            f"--record={self.output_path}",
            "--no-playback",
            "--audio-source=playback",
            "--audio-codec=aac",
            "--video-codec=h264",
            f"--max-fps={self.max_fps}",
            f"--video-bit-rate={self.bit_rate}",
            f"--max-size={self.max_size}",
        ]

        env = os.environ.copy()
        if self.adb_exe and self.adb_exe.exists():
            env["ADB"] = str(self.adb_exe)
            # 把 adb 所在目录加到 PATH 最前
            env["PATH"] = str(self.adb_exe.parent) + os.path.pathsep + env.get("PATH", "")

        logger.info(f"启动录制管线: {' '.join(cmd)}")
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
        self._start_time = time.time()
        time.sleep(2)  # 给进程 2 秒初始化时间

        if not self.is_recording():
            stdout, stderr = self._proc.communicate()
            logger.error(f"scrcpy 启动失败！退出码: {self._proc.returncode}\nSTDOUT: {stdout}\nSTDERR: {stderr}")
            return False

        logger.info(f"录制管线启动成功，正在写入: {self.output_path}")
        return True

    def stop(self, timeout_sec: int = 10) -> bool:
        """平稳停止录制，等待 MP4 封口完成。"""
        if not self.is_recording():
            logger.info("录制管线已处于停止状态。")
            return True

        logger.info(f"正在停止录制: {self.output_path.name}")
        try:
            if os.name == "nt":
                # 发送 CTRL_BREAK_EVENT 或 CTRL_C_EVENT
                self._proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self._proc.send_signal(signal.SIGINT)

            self._proc.wait(timeout=timeout_sec)
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning(f"等待录制平稳退出超时或异常 ({e})，强制终止进程...")
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()

        duration = int(time.time() - (self._start_time or time.time()))
        logger.info(f"录制结束，持续时间: {duration}s，文件大小: {self.get_file_size_mb():.2f}MB")
        return self.output_path.exists() and self.output_path.stat().st_size > 0

    def get_file_size_mb(self) -> float:
        """获取当前录制文件大小（MB）。"""
        if self.output_path.exists():
            return self.output_path.stat().st_size / (1024 * 1024)
        return 0.0

    def teardown(self):
        """生命周期异常保护，确保进程被释放。"""
        if self.is_recording():
            logger.warning(f"执行 Teardown 紧急清理录制进程 (PID {self._proc.pid})")
            self.stop(timeout_sec=3)

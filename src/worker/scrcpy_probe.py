"""scrcpy 版本与能力探针，及防背压日志排空器 (Ticket 03)。"""
from dataclasses import dataclass
import logging
from pathlib import Path
import re
import subprocess
import threading
from typing import Callable

logger = logging.getLogger("ScrcpyProbe")


@dataclass
class ScrcpyCapabilities:
    """scrcpy 环境能力快照。"""
    version: str
    major: int
    minor: int
    patch: int = 0
    supports_require_audio: bool = False


def parse_scrcpy_version_output(output: str) -> ScrcpyCapabilities:
    """从 `scrcpy --version` 的输出文本中解析出版本号与特性支持。"""
    # 典型格式: scrcpy 3.1 <https://github.com/Genymobile/scrcpy>
    match = re.search(r"scrcpy\s+(\d+)\.(\d+)(?:\.(\d+))?", output)
    if not match:
        raise ValueError(f"无法从 scrcpy 输出中解析版本: {output[:100]}")

    major = int(match.group(1))
    minor = int(match.group(2))
    patch = int(match.group(3)) if match.group(3) else 0
    version_str = f"{major}.{minor}" if patch == 0 else f"{major}.{minor}.{patch}"

    # scrcpy 从 v2.1 开始引入 --require-audio 参数
    supports_require_audio = (major > 2) or (major == 2 and minor >= 1)

    return ScrcpyCapabilities(
        version=version_str,
        major=major,
        minor=minor,
        patch=patch,
        supports_require_audio=supports_require_audio,
    )


def validate_scrcpy_policy(
    caps: ScrcpyCapabilities,
    target_version: str | None = None,
) -> tuple[bool, str]:
    """根据本任务安全策略验证 scrcpy 能力。

    返回 (is_valid, reject_reason)。
    """
    if caps.major < 2:
        return False, f"scrcpy 版本过低 (当前为 {caps.version}，要求 major >= 2)"

    if not caps.supports_require_audio:
        return False, f"scrcpy 缺少必需音频录制能力 (--require-audio，要求版本 >= 2.1，当前为 {caps.version})"

    if target_version and caps.version != target_version:
        return False, f"scrcpy 版本不符合策略要求 (当前为 {caps.version}，指定版本为 {target_version})"

    return True, ""


class PipelineLogDrainer:
    """针对子进程 stdout/stderr 管道的非阻塞日志排空器。

    持续从子进程标准输出与错误流中读取数据，杜绝因管道缓冲区满（默认 64KB 背压）
    导致子进程录像及音视频编码管线发生不可自愈的死锁卡死。
    """

    def __init__(
        self,
        process: subprocess.Popen,
        on_log_line: Callable[[str], None] | None = None,
    ):
        self.process = process
        self.on_log_line = on_log_line
        self._threads: list[threading.Thread] = []
        self._stop_event = threading.Event()

    def start(self) -> None:
        """启动排空线程。"""
        if self.process.stdout:
            t_out = threading.Thread(
                target=self._drain_stream,
                args=(self.process.stdout, "stdout"),
                daemon=True,
                name="scrcpy-stdout-drainer"
            )
            t_out.start()
            self._threads.append(t_out)

        if self.process.stderr:
            t_err = threading.Thread(
                target=self._drain_stream,
                args=(self.process.stderr, "stderr"),
                daemon=True,
                name="scrcpy-stderr-drainer"
            )
            t_err.start()
            self._threads.append(t_err)

    def _drain_stream(self, stream, stream_name: str) -> None:
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                stripped = line.rstrip("\r\n")
                if self.on_log_line:
                    try:
                        self.on_log_line(stripped)
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def join(self, timeout: float | None = 2.0) -> None:
        """等待排空线程结束。"""
        for t in self._threads:
            t.join(timeout=timeout)

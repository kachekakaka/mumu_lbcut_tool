"""测试 scrcpy 版本策略探针与日志管道防背压 (Ticket 03)。"""
import io
import subprocess
import sys
import threading
import time
from pathlib import Path
import pytest

from src.worker.scrcpy_probe import (
    ScrcpyCapabilities,
    parse_scrcpy_version_output,
    validate_scrcpy_policy,
    PipelineLogDrainer,
)


def test_parse_scrcpy_version_output():
    """解析真实 scrcpy --version 输出格式。"""
    sample_output = """scrcpy 3.1 <https://github.com/Genymobile/scrcpy>
dependencies:
 - SDL 2.30.9
 - libavcodec 61.19.100
 - libavformat 61.7.100
 - libavutil 59.39.100
"""
    caps = parse_scrcpy_version_output(sample_output)
    assert caps.version == "3.1"
    assert caps.major == 3
    assert caps.minor == 1
    assert caps.supports_require_audio is True  # scrcpy >= 2.1 支持 --require-audio


def test_validate_scrcpy_policy_rejects_version_below_2():
    """主版本小于 2 时必须明确拒绝启动。"""
    v1_caps = ScrcpyCapabilities(
        version="1.25",
        major=1,
        minor=25,
        patch=0,
        supports_require_audio=False,
    )
    is_valid, reason = validate_scrcpy_policy(v1_caps)
    assert not is_valid
    assert "版本过低" in reason or "major >= 2" in reason or "< 2" in reason


def test_validate_scrcpy_policy_rejects_missing_audio():
    """缺少音频捕获必需能力时必须明确拒绝。"""
    v20_caps = ScrcpyCapabilities(
        version="2.0",
        major=2,
        minor=0,
        patch=0,
        supports_require_audio=False,  # v2.0 尚无 --require-audio
    )
    is_valid, reason = validate_scrcpy_policy(v20_caps)
    assert not is_valid
    assert "require-audio" in reason or "音频" in reason


def test_pipeline_log_drainer_prevents_pipe_buffer_deadlock():
    """日志排空器能够快速排空子进程的高频大量标准输出，杜绝管道 64KB 缓冲区死锁。"""
    # 构造一个向 stdout/stderr 连续写入 200KB 数据的子进程
    # 若无 drainer，子进程在写入超过 64KB 时会立即死锁挂起
    code = """
import sys
for _ in range(5000):
    sys.stdout.write("scrcpy log line sample data padding 1234567890\\n")
    sys.stderr.write("scrcpy err line sample data padding 1234567890\\n")
sys.stdout.flush()
sys.stderr.flush()
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )

    collected_logs = []
    drainer = PipelineLogDrainer(proc, on_log_line=lambda line: collected_logs.append(line))
    drainer.start()

    # 子进程必须在 3 秒内顺畅完成退出，不能死锁
    proc.wait(timeout=3.0)
    drainer.join(timeout=2.0)

    assert proc.returncode == 0
    assert len(collected_logs) >= 5000

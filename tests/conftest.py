"""可重复的最小媒体夹具；产物由 pytest 的外部临时目录承载。"""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.fixture(scope="session")
def valid_media(tmp_path_factory):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        pytest.skip("本机缺少 ffmpeg/ffprobe，无法执行真实媒体校验测试")
    path = tmp_path_factory.mktemp("synthetic_media") / "short_dual_track.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-n", "-f", "lavfi", "-i", "testsrc=size=96x64:rate=8",
                    "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=24000", "-t", "1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
                   check=True, capture_output=True, timeout=20)
    return path

"""可重复的最小媒体夹具；产物由 pytest 的外部临时目录承载。"""
import subprocess

import pytest
from src.mvp.dep_manager import DependencyManager


@pytest.fixture(scope="session")
def media_tools():
    return DependencyManager().get_media_tools()


@pytest.fixture(scope="session")
def valid_media(tmp_path_factory, media_tools):
    ffmpeg = str(media_tools.ffmpeg)
    path = tmp_path_factory.mktemp("synthetic_media") / "short_dual_track.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-n", "-f", "lavfi", "-i", "testsrc=size=96x64:rate=8",
                    "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=24000", "-t", "1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
                   check=True, capture_output=True, timeout=20)
    return path

"""外部工具管理：媒体工具固定构建和目录，不使用系统 PATH 回退。"""
from contextlib import contextmanager
from dataclasses import dataclass
import errno
from pathlib import Path
import os
import shutil
import urllib.request
import zipfile
import json
import logging
import subprocess
import tempfile
import time
from uuid import uuid4
import psutil

from src.mvp.config import SCRCPY_DIR, FFMPEG_DIR, FFMPEG_BUILD, get_adb_exe
from src.mvp.command import run_client

logger = logging.getLogger(__name__)

SCRCPY_RELEASE_API = "https://api.github.com/repos/Genymobile/scrcpy/releases/latest"
SCRCPY_DEFAULT_WIN64_URL = "https://github.com/Genymobile/scrcpy/releases/download/v3.1/scrcpy-win64-v3.1.zip"


@contextmanager
def installation_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            deadline = time.monotonic() + 120
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN) or time.monotonic() >= deadline:
                        raise
                    time.sleep(.1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


@dataclass(frozen=True)
class MediaTools:
    ffmpeg: Path
    ffprobe: Path


class DependencyManager:
    """管理 scrcpy 及 ADB 等外部工具的检查与获取。"""

    def __init__(self, scrcpy_dir: Path = SCRCPY_DIR, ffmpeg_dir: Path = FFMPEG_DIR):
        self.scrcpy_dir = Path(scrcpy_dir).resolve()
        self.scrcpy_exe_path = self.scrcpy_dir / "scrcpy.exe"
        self._install_lock_path = self.scrcpy_dir.with_name(self.scrcpy_dir.name + ".install.lock")
        self.ffmpeg_dir = Path(ffmpeg_dir).resolve()

    def get_media_tools(self) -> MediaTools:
        """开机前校验成套固定工具；部署包负责携带文件，运行期不下载或升级。"""
        paths = [self.ffmpeg_dir / "bin" / (name + ".exe") for name in ("ffmpeg", "ffprobe")]
        configurations = []
        compilers = []
        for name, path in zip(("ffmpeg", "ffprobe"), paths):
            if not path.is_file():
                raise FileNotFoundError(f"缺少固定媒体工具：{path}；请部署 Gyan {FFMPEG_BUILD} 的 ffmpeg/ffprobe 配套文件")
            try:
                result = run_client([str(path), "-version"], timeout=8)
            except (OSError, subprocess.SubprocessError) as exc:
                raise RuntimeError(f"固定媒体工具无法执行：{path}：{exc}") from exc
            lines = result.stdout.splitlines()
            if result.returncode or not lines or not lines[0].startswith(f"{name} version {FFMPEG_BUILD} "):
                actual = lines[0] if lines else result.stderr.strip()[:200]
                raise RuntimeError(f"媒体工具版本不符：{path}；要求 {FFMPEG_BUILD}，实际 {actual}")
            compiler = next((line for line in lines if line.startswith("built with ")), "")
            configuration = next((line for line in lines if line.startswith("configuration: ")), "")
            if not compiler or not configuration:
                raise RuntimeError(f"媒体工具缺少构建信息：{path}")
            compilers.append(compiler)
            configurations.append(configuration)
            logger.info("固定媒体工具：%s | %s | %s", path, lines[0], compiler)
        if compilers[0] != compilers[1] or configurations[0] != configurations[1]:
            raise RuntimeError("ffmpeg 与 ffprobe 的构建配置不一致，必须部署同一 Gyan 发行包中的配套文件")
        logger.info("固定媒体工具构建配置：%s", configurations[0])
        return MediaTools(*paths)

    def get_adb_path(self) -> Path:
        """获取并校验 ADB 执行路径。"""
        adb = get_adb_exe()
        if not adb or not adb.exists():
            raise FileNotFoundError("未检测到有效的 adb.exe，请确认 MuMu 模拟器安装路径。")
        return adb

    def is_scrcpy_ready(self) -> bool:
        """检查 scrcpy.exe 及 server 是否已就绪。"""
        return not self._missing_scrcpy(self.scrcpy_dir)

    @staticmethod
    def _missing_scrcpy(directory):
        return [directory / name for name in ("scrcpy.exe", "scrcpy-server")
                if not (directory / name).is_file()]

    def ensure_scrcpy(self, force_download: bool = False) -> Path:
        """确保 scrcpy 可用；若不存在则自动下载解压。"""
        with installation_lock(self._install_lock_path):
            if self.scrcpy_dir.exists() and not force_download:
                return self.require_scrcpy()
            with tempfile.TemporaryDirectory(prefix=".scrcpy-install-", dir=self.scrcpy_dir.parent) as temp:
                staging = Path(temp)
                zip_path = staging / "release.zip"
                extract_dir = staging / "extracted"
                url = self._get_latest_win64_url()
                logger.info("正在下载 scrcpy 发布包：%s", url)
                request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(request, timeout=30) as response, zip_path.open("xb") as archive:
                    shutil.copyfileobj(response, archive)
                with zipfile.ZipFile(zip_path) as release:
                    release.extractall(extract_dir)
                items = list(extract_dir.iterdir())
                bundle = items[0] if len(items) == 1 and items[0].is_dir() else extract_dir
                if missing := self._missing_scrcpy(bundle):
                    raise FileNotFoundError("scrcpy 发布包缺件：" + "、".join(p.name for p in missing))
                self._install_scrcpy_bundle(bundle)
        logger.info("scrcpy 已就绪：%s", self.scrcpy_exe_path)
        return self.scrcpy_exe_path

    def _install_scrcpy_bundle(self, bundle):
        backup = None
        if self.scrcpy_dir.exists():
            for process in psutil.process_iter(["exe"]):
                if process.info.get("exe") and Path(process.info["exe"]) == self.scrcpy_exe_path:
                    raise PermissionError(f"scrcpy 正在使用，无法更新：{self.scrcpy_exe_path}（PID {process.pid}）")
            backup = self.scrcpy_dir.with_name(f".{self.scrcpy_dir.name}-previous-{uuid4().hex}")
            # Windows 拒绝移动含正在使用文件的目录时，原目录与内容仍在原位。
            self.scrcpy_dir.rename(backup)
        try:
            bundle.rename(self.scrcpy_dir)
        except OSError:
            if backup is not None:
                backup.rename(self.scrcpy_dir)
            raise
        if backup is not None:
            logger.info("原 scrcpy 目录已保留：%s", backup)

    def require_scrcpy(self) -> Path:
        """仅检查本地依赖，不下载或改写任何工具文件。"""
        if missing := self._missing_scrcpy(self.scrcpy_dir):
            raise FileNotFoundError("缺少本地采集工具：" + "、".join(map(str, missing)))
        return self.scrcpy_exe_path

    def _get_latest_win64_url(self) -> str:
        """从 GitHub API 获取最新的 win64 下载直链，若失败则回退至默认版本。"""
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            req = urllib.request.Request(SCRCPY_RELEASE_API, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                for asset in data.get("assets", []):
                    name = asset.get("name", "")
                    if "win64" in name and name.endswith(".zip"):
                        return asset["browser_download_url"]
        except Exception as e:
            logger.warning(f"获取最新 scrcpy 发布信息失败 ({e})，使用默认稳定下载地址。")
        return SCRCPY_DEFAULT_WIN64_URL

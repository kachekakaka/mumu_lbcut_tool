"""外部工具管理：媒体工具固定构建和目录，不使用系统 PATH 回退。"""
from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import urllib.request
import zipfile
import json
import logging
import subprocess

from src.mvp.config import SCRCPY_DIR, FFMPEG_DIR, FFMPEG_BUILD, get_adb_exe
from src.mvp.command import run_client

logger = logging.getLogger(__name__)

SCRCPY_RELEASE_API = "https://api.github.com/repos/Genymobile/scrcpy/releases/latest"
SCRCPY_DEFAULT_WIN64_URL = "https://github.com/Genymobile/scrcpy/releases/download/v3.1/scrcpy-win64-v3.1.zip"


@dataclass(frozen=True)
class MediaTools:
    ffmpeg: Path
    ffprobe: Path


class DependencyManager:
    """管理 scrcpy 及 ADB 等外部工具的检查与获取。"""

    def __init__(self, scrcpy_dir: Path = SCRCPY_DIR, ffmpeg_dir: Path = FFMPEG_DIR):
        self.scrcpy_dir = scrcpy_dir
        self.scrcpy_exe_path = self.scrcpy_dir / "scrcpy.exe"
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
        return self.scrcpy_exe_path.exists()

    def ensure_scrcpy(self, force_download: bool = False) -> Path:
        """确保 scrcpy 可用；若不存在则自动下载解压。"""
        if self.is_scrcpy_ready() and not force_download:
            return self.scrcpy_exe_path

        self.scrcpy_dir.mkdir(parents=True, exist_ok=True)
        download_url = self._get_latest_win64_url()
        zip_path = self.scrcpy_dir.parent / "scrcpy_temp.zip"

        logger.info(f"正在下载 scrcpy 发布包: {download_url}")
        try:
            # 下载 zip
            headers = {"User-Agent": "Mozilla/5.0"}
            req = urllib.request.Request(download_url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp, open(zip_path, "wb") as f:
                shutil.copyfileobj(resp, f)

            logger.info("下载完成，正在解压...")
            # 解压
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                # 寻找内部根目录
                namelist = zip_ref.namelist()
                top_dirs = {name.split("/")[0] for name in namelist if "/" in name}
                extract_temp = self.scrcpy_dir.parent / "_scrcpy_extract_tmp"
                if extract_temp.exists():
                    shutil.rmtree(extract_temp)
                zip_ref.extractall(extract_temp)

                # 搬运文件到目标目录
                src_folder = extract_temp
                if len(top_dirs) == 1:
                    single_top = extract_temp / list(top_dirs)[0]
                    if single_top.is_dir():
                        src_folder = single_top

                for item in src_folder.iterdir():
                    dest = self.scrcpy_dir / item.name
                    if dest.exists():
                        if dest.is_dir():
                            shutil.rmtree(dest)
                        else:
                            dest.unlink()
                    shutil.move(str(item), str(dest))

                if extract_temp.exists():
                    shutil.rmtree(extract_temp)

        finally:
            if zip_path.exists():
                zip_path.unlink()

        if not self.is_scrcpy_ready():
            raise RuntimeError(f"解压后未找到 {self.scrcpy_exe_path}，请检查发布包内容。")

        logger.info(f"scrcpy 已就绪: {self.scrcpy_exe_path}")
        return self.scrcpy_exe_path

    def require_scrcpy(self) -> Path:
        """仅检查本地依赖，不下载或改写任何工具文件。"""
        for path in (self.scrcpy_exe_path, self.scrcpy_dir / 'scrcpy-server'):
            if not path.is_file():
                raise FileNotFoundError(f'缺少本地采集工具：{path}')
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

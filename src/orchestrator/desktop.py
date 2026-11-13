"""宿主机桌面文件与目录定位辅助函数。"""
import logging
from pathlib import Path
import platform
import subprocess

logger = logging.getLogger(__name__)


def run_file_manager(cmd: list[str]) -> None:
    """拉起宿主机文件管理器进程。"""
    subprocess.Popen(cmd)


def reveal_in_file_manager(target_path: Path, is_file: bool = True) -> None:
    """在宿主机资源管理器中高亮定位文件或打开目录。"""
    system = platform.system()
    if system == "Windows":
        cmd = ["explorer.exe", f"/select,{target_path}"] if is_file else ["explorer.exe", str(target_path)]
    else:
        # 非 Windows 平台测试/运行降级
        folder = target_path.parent if is_file else target_path
        cmd = ["open" if system == "Darwin" else "xdg-open", str(folder)]

    try:
        run_file_manager(cmd)
    except Exception as exc:
        logger.warning("拉起宿主机文件管理器失败: %s", exc)

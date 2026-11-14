"""宿主机桌面文件与目录定位辅助函数。"""
from pathlib import Path
import platform
import subprocess


def run_file_manager(cmd: list[str]) -> None:
    """拉起宿主机文件管理器进程。"""
    subprocess.Popen(cmd)


def reveal_in_file_manager(target_path: Path, is_file: bool = True) -> None:
    """发送桌面定位请求；启动失败向上传递，返回不代表桌面已完成定位。"""
    system = platform.system()
    if system == "Windows":
        cmd = ["explorer.exe", f"/select,{target_path}"] if is_file else ["explorer.exe", str(target_path)]
    else:
        # 非 Windows 平台测试/运行降级
        folder = target_path.parent if is_file else target_path
        cmd = ["open" if system == "Darwin" else "xdg-open", str(folder)]

    run_file_manager(cmd)

"""宿主机桌面文件与目录定位辅助函数。"""
import os
from pathlib import Path
import platform
import subprocess


def get_windows_system_dir() -> Path:
    r"""获取并验证 Windows 系统目录（通常为 C:\Windows）。"""
    sys_root = os.environ.get("SystemRoot") or os.environ.get("windir")
    if not sys_root:
        raise RuntimeError("无法获取 Windows 系统目录 (SystemRoot/windir 缺失)")
    path = Path(sys_root).resolve()
    if not path.is_dir():
        raise RuntimeError(f"Windows 系统目录不存在: {path}")
    return path


def run_file_manager(cmd: list[str]) -> None:
    """拉起宿主机文件管理器进程。"""
    cwd = None
    if platform.system() == "Windows":
        cwd = str(get_windows_system_dir())
    subprocess.Popen(cmd, cwd=cwd)


def reveal_in_file_manager(target_path: Path, is_file: bool = True) -> None:
    """发送桌面定位请求；启动失败向上传递，返回不代表桌面已完成定位。"""
    target = Path(target_path).resolve()
    system = platform.system()
    if system == "Windows":
        cmd = ["explorer.exe", f"/select,{target}"] if is_file else ["explorer.exe", str(target)]
    else:
        # 非 Windows 平台测试/运行降级
        folder = target.parent if is_file else target
        cmd = ["open" if system == "Darwin" else "xdg-open", str(folder)]

    run_file_manager(cmd)

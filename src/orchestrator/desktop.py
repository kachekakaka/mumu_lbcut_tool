"""宿主机桌面文件与目录定位辅助函数。"""
import ctypes
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


def select_windows_file(target: Path) -> None:
    """让 Windows 外壳打开文件所在目录并选中文件。"""
    ole = ctypes.OleDLL("ole32")
    shell = ctypes.OleDLL("shell32")
    ole.CoInitialize.argtypes = [ctypes.c_void_p]
    ole.CoUninitialize.restype = None
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    shell.SHParseDisplayName.argtypes = [
        ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_ulong, ctypes.c_void_p,
    ]
    shell.SHOpenFolderAndSelectItems.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_ulong,
    ]
    pidl = ctypes.c_void_p()
    ole.CoInitialize(None)
    try:
        shell.SHParseDisplayName(str(target), None, ctypes.byref(pidl), 0, None)
        shell.SHOpenFolderAndSelectItems(pidl, 0, None, 0)
    finally:
        ole.CoTaskMemFree(pidl)
        ole.CoUninitialize()


def reveal_in_file_manager(target_path: Path, is_file: bool = True) -> None:
    """发送桌面定位请求；启动失败向上传递，返回不代表桌面已完成定位。"""
    target = Path(target_path).resolve()
    system = platform.system()
    if system == "Windows":
        if is_file:
            select_windows_file(target)
            return
        cmd = ["explorer.exe", str(target)]
    else:
        # 非 Windows 平台测试/运行降级
        folder = target.parent if is_file else target
        cmd = ["open" if system == "Darwin" else "xdg-open", str(folder)]

    run_file_manager(cmd)

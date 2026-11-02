"""MVP 核心配置与路径解析模块。"""
from pathlib import Path
import os
import winreg

# 仓库根目录
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# 仓外工作目录（遵循 README.md 约定）
DEV_DIR = REPO_ROOT.parent / "mumu_lbcut_tool_dev"
TMP_DIR = DEV_DIR / "mumu_lbcut_tool_tmp"
TOOLS_DIR = DEV_DIR / "tools"
SCRCPY_DIR = TOOLS_DIR / "scrcpy"

# 测试产物目录
MVP_OUTPUT_DIR = TMP_DIR / "20261020-mvp"


def get_mumu_install_path() -> Path | None:
    """自动获取或探测 MuMu 模拟器安装路径。"""
    # 1. 优先读取环境变量
    env_path = os.environ.get("MUMU_HOME")
    if env_path and Path(env_path).exists():
        return Path(env_path)

    # 2. 查询 Windows 注册表
    reg_keys = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    ]
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub_key in reg_keys:
            try:
                with winreg.OpenKey(root, sub_key) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            sub_name = winreg.EnumKey(key, i)
                            with winreg.OpenKey(key, sub_name) as app_key:
                                display_name, _ = winreg.QueryValueEx(app_key, "DisplayName")
                                if "mumu" in str(display_name).lower():
                                    install_loc, _ = winreg.QueryValueEx(app_key, "InstallLocation")
                                    if install_loc and Path(install_loc).exists():
                                        return Path(install_loc)
                        except OSError:
                            continue
            except OSError:
                continue

    # 3. 常见默认路径兜底
    common_paths = [
        Path(r"D:\soft\MuMuPlayer"),
        Path(r"C:\Program Files\Netease\MuMuPlayer-12.0"),
        Path(r"C:\Program Files\MuMuPlayer"),
    ]
    for path in common_paths:
        if path.exists():
            return path

    return None


def get_mumu_manager_exe() -> Path | None:
    """获取 MuMuManager.exe 路径。"""
    mumu_dir = get_mumu_install_path()
    if not mumu_dir:
        return None
    candidate = mumu_dir / "nx_main" / "MuMuManager.exe"
    if candidate.exists():
        return candidate
    return None


def get_adb_exe() -> Path | None:
    """优先获取 MuMu 自带的 adb.exe，避免版本冲突。"""
    mumu_dir = get_mumu_install_path()
    if mumu_dir:
        candidate = mumu_dir / "nx_main" / "adb.exe"
        if candidate.exists():
            return candidate
    return None

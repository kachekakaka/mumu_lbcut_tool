"""Windows 便携录播控制台的启动和自检入口。"""
import argparse
from contextlib import closing
import ctypes
from ctypes import wintypes
from importlib import import_module
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import uuid


BUNDLE = Path(__file__).resolve().parent
WEB = BUNDLE / "app" / "web" / "dist"
DB_NAME = "orchestrator.sqlite3"


def choose_recordings():
    """用 Windows 自带的文件夹选择器，返回用户选定的已有目录。"""
    class BrowseInfo(ctypes.Structure):
        _fields_ = [("hwndOwner", wintypes.HWND), ("pidlRoot", ctypes.c_void_p),
                    ("pszDisplayName", wintypes.LPWSTR), ("lpszTitle", wintypes.LPCWSTR),
                    ("ulFlags", wintypes.UINT), ("lpfn", ctypes.c_void_p),
                    ("lParam", wintypes.LPARAM), ("iImage", ctypes.c_int)]

    ole = ctypes.OleDLL("ole32")
    shell = ctypes.WinDLL("shell32")
    shell.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BrowseInfo)]
    shell.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell.SHGetPathFromIDListEx.argtypes = [ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    free = ctypes.WinDLL("ole32").CoTaskMemFree
    free.argtypes = [ctypes.c_void_p]
    ole.OleInitialize(None)
    try:
        display = ctypes.create_unicode_buffer(260)
        info = BrowseInfo(pszDisplayName=ctypes.cast(display, wintypes.LPWSTR),
                          lpszTitle="选择存放录像的文件夹（请选择便携包以外的位置）",
                          ulFlags=0x01 | 0x40 | 0x10)
        item = shell.SHBrowseForFolderW(ctypes.byref(info))
        if not item:
            return None
        try:
            path = ctypes.create_unicode_buffer(32768)
            if not shell.SHGetPathFromIDListEx(item, path, len(path), 0):
                raise ValueError("请选择磁盘上的文件夹")
            return Path(path.value)
        finally:
            free(item)
    finally:
        ctypes.WinDLL("ole32").OleUninitialize()


def check_tools():
    from src.mvp.config import get_mumu_install_path, get_adb_exe, get_mumu_manager_exe
    from src.mvp.dep_manager import DependencyManager
    install = get_mumu_install_path()
    if not install:
        raise FileNotFoundError("找不到 MuMu 安装目录。请安装 MuMu，或设置 MUMU_HOME 指向安装目录。")
    for lookup, name in ((get_mumu_manager_exe, "MuMuManager.exe"), (get_adb_exe, "adb.exe")):
        if not lookup():
            raise FileNotFoundError(f"缺少 MuMu 文件 {install / 'nx_main' / name}")
    dep = DependencyManager()
    scrcpy = dep.require_scrcpy()
    version = subprocess.run([str(scrcpy), "--version"], cwd=scrcpy.parent, timeout=10,
                             capture_output=True, encoding="utf-8", errors="replace")
    if version.returncode or not re.match(r"scrcpy 5\.0\.1(?:\s|$)", version.stdout):
        raise ValueError(f"请重新解压完整的 scrcpy 5.0.1 及配套 DLL：{scrcpy}")
    dep.get_media_tools()
    index = WEB / "index.html"
    if not index.is_file():
        raise FileNotFoundError(f"缺少控制台网页 {index}")
    assets = re.findall(r'(?:src|href)="(/?assets/[^"]+)"', index.read_text(encoding="utf-8"))
    if not assets:
        raise ValueError(f"网页入口缺少构建资源：{index}")
    for asset in assets:
        file = WEB / asset.lstrip("/")
        if not file.is_file():
            raise FileNotFoundError(f"缺少网页资源 {file}")


def writable_recordings(path, home):
    if not path.is_absolute():
        raise ValueError(f"录像目录需要绝对路径：{path}")
    path = path.resolve()
    if path.is_relative_to(BUNDLE) or path.is_relative_to(home):
        raise ValueError(f"请将录像保存在程序和运行数据目录之外：{path}")
    if not path.is_dir():
        raise FileNotFoundError(f"原录像目录不可用，请连接原磁盘：{path}")
    probe = path / (".mumu-write-" + uuid.uuid4().hex)
    try:
        # Windows 上 tempfile 可能将目录拒写反复当成重名，一次独占创建即可验证。
        handle = probe.open("xb")
        try:
            with handle:
                handle.write(b"\0")
        finally:
            probe.unlink()
    except OSError as exc:
        raise OSError(f"无法写入录像目录：{path}") from exc
    return path


def recordings_for(home):
    from src.orchestrator.storage import SCHEMA_VERSION, Store
    data = home / "data"
    settings = data / "settings.json"
    db = data / DB_NAME
    if data.exists():
        try:
            saved = json.loads(settings.read_text(encoding="utf-8"))["recordings"]
            if not isinstance(saved, str) or not saved:
                raise ValueError("缺少录像路径")
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise ValueError(f"请恢复原设置文件 {settings}：{exc}") from exc
        path = writable_recordings(Path(saved), home)
        if not db.is_file():
            raise FileNotFoundError(f"原调度数据库丢失，请恢复 {db}")
        try:
            with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if version != SCHEMA_VERSION or not {"jobs", "targets", "segments"}.issubset(tables):
                raise ValueError(f"要求 v{SCHEMA_VERSION} 的调度数据库")
        except (ValueError, sqlite3.Error) as exc:
            raise ValueError(f"原调度数据库不可用 {db}：{exc}") from exc
        return path
    selected = choose_recordings()
    if selected is None:
        return None
    path = writable_recordings(selected, home)
    # 设置与已关闭的首个数据库一同就位，中途失败可重新选择。
    with tempfile.TemporaryDirectory(dir=home) as tmp:
        stage = Path(tmp) / "data"
        store = Store(stage / DB_NAME)
        store.close()
        (stage / "settings.json").write_text(
            json.dumps({"recordings": str(path)}, ensure_ascii=False, indent=2), encoding="utf-8")
        stage.rename(data)
    return path


def main():
    parser = argparse.ArgumentParser(description="MuMu 便携录播")
    parser.add_argument("action", choices=("check", "start"))
    parser.add_argument("--segment-duration", type=int, default=1800, help="每片秒数，默认 1800")
    args = parser.parse_args()
    if args.segment_duration < 1:
        parser.error("每片秒数必须为正数")
    try:
        check_tools()
        from src.orchestrator.cli import main as run
        from src.orchestrator.scheduler import HostLease
        from src.orchestrator import server
        for module in ("src.worker.cli", "src.orchestrator.room_query_process", "httpx",
                       "yaml", "numpy", "PIL.Image"):
            import_module(module)
        if args.action == "check":
            print(f"环境自检通过。控制台 {server.SERVICE_VERSION}，采集工具和网页就绪。")
            return 0
        home = Path(os.environ["LOCALAPPDATA"]).resolve() / "MuMuLBCut"
        if home.is_relative_to(BUNDLE):
            raise ValueError(f"运行数据目录位于解压目录内，请另选解压位置：{home}")
        with closing(HostLease(home / "portable.lock")):
            output = recordings_for(home)
            if output is None:
                print("已取消选择录像目录。")
                return 0
            os.environ["MUMU_LBCUT_INSTANCE_LOCK_DIR"] = str(home / "instance-locks")
            print(f"录像目录 {output}\n运行数据 {home / 'data'}\n"
                  "控制台 http://127.0.0.1:8000/\n按 Ctrl+C 平滑停止，等待尾片保存。", flush=True)
            return run(["run", "--data-dir", str(home / "data"), "--output-dir", str(output),
                        "--static-root", str(WEB), "--open-browser", "--require-local-tools",
                        "--segment-duration", str(args.segment_duration)])
    except (OSError, ValueError, RuntimeError, KeyError, ImportError, subprocess.SubprocessError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

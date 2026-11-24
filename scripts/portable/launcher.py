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
import time
from urllib.request import ProxyHandler, build_opener
import uuid


BUNDLE = Path(__file__).resolve().parent
WEB = BUNDLE / "app" / "web" / "dist"
DB_NAME = "orchestrator.sqlite3"


def choose_directory(title):
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
                          lpszTitle=title,
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


def atomic_settings(path, value):
    temporary = path.with_name("." + path.name + "-" + uuid.uuid4().hex)
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise OSError(f"无法保存设置 {path}：{exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def absolute_directory(path):
    value = Path(path)
    if not value.is_absolute() or value.drive.startswith("\\\\") or value.resolve() != value:
        raise ValueError(f"请选择直接指向本地目录的绝对路径：{path}")
    return value


def overlaps(path, *others):
    return any(path.is_relative_to(other) or other.is_relative_to(path) for other in others)


def probe_directory(path, *, hardlink=False):
    source = path / (".mumu-write-" + uuid.uuid4().hex)
    target = source.with_suffix(".link")
    created = linked = False
    try:
        with source.open("xb") as stream:
            created = True
            stream.write(b"\0")
        if hardlink:
            os.link(source, target)
            linked = True
            try:
                os.link(source, target)
            except FileExistsError:
                pass
            else:
                raise OSError("此卷无法保证无覆盖归档")
    except OSError as exc:
        raise OSError(f"无法在此目录写入或无覆盖归档 {path}：{exc}") from exc
    finally:
        if linked:
            target.unlink()
        if created:
            source.unlink()


def read_settings(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("需要 JSON 对象")
        return value
    except (ValueError, OSError) as exc:
        raise ValueError(f"请恢复原设置文件 {path}：{exc}") from exc


def choose_workspace(default):
    answer = ctypes.WinDLL("user32").MessageBoxW(
        None, f"使用默认工作目录？\n{default}\n\n是：使用默认目录\n否：选择其他目录",
        "MuMu 工作目录", 0x23)
    if answer == 6:
        return default
    if answer == 7:
        return choose_directory("选择 MuMu 工作目录")
    return None


def workspace_for(bundle, home, recordings_override=None):
    from src.orchestrator.storage import SCHEMA_VERSION, Store
    pointer = bundle / "workspace.json"
    if pointer.is_symlink():
        raise ValueError(f"工作目录指针必须在启动器旁：{pointer}")
    saved_pointer = pointer.exists()
    default = bundle.parent / "mumu_lbcut_tool_dev"
    if saved_pointer:
        saved = read_settings(pointer)
        if set(saved) != {"dataDirectory"} or not isinstance(saved["dataDirectory"], str):
            raise ValueError(f"工作目录指针损坏，请恢复 {pointer}")
        try:
            workspace = absolute_directory(saved["dataDirectory"])
        except ValueError as exc:
            raise ValueError(f"工作目录指针损坏 {pointer}：{exc}") from exc
        if not workspace.is_dir():
            raise FileNotFoundError(f"原工作目录不可用，请连接原磁盘：{workspace}")
    else:
        workspace = choose_workspace(default)
        if workspace is None:
            return None
        workspace = absolute_directory(workspace)
        probe_directory(bundle)
    if overlaps(workspace, bundle, home):
        raise ValueError(f"工作目录和程序或本机启动锁目录重叠：{workspace}")
    data = absolute_directory(workspace / "runtime-console-unified")
    upload = absolute_directory(workspace / "upload-platform")
    settings = data / "settings.json"
    db = data / DB_NAME
    existing = saved_pointer or data.exists()
    if existing and (settings.resolve() != settings or db.resolve() != db):
        raise ValueError(f"原调度设置或数据库指向其他位置：{settings}，{db}")
    values = read_settings(settings) if existing else {}
    if existing:
        if not isinstance(values.get("recordings"), str):
            raise ValueError(f"原设置缺少录像路径，请恢复 {settings}")
        if not db.is_file() or not db.stat().st_size:
            raise FileNotFoundError(f"原调度数据库丢失，请恢复 {db}")
        try:
            with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if version != SCHEMA_VERSION or not {"jobs", "targets", "segments"}.issubset(tables):
                raise ValueError(f"要求 v{SCHEMA_VERSION} 的调度数据库")
        except (ValueError, sqlite3.Error) as exc:
            raise ValueError(f"原调度数据库不可用 {db}：{exc}") from exc
    path = absolute_directory(recordings_override or values.get("recordings") or workspace / "recordings")
    if overlaps(path, bundle, data, upload, home):
        raise ValueError(f"录像根与程序、录播或上传数据目录重叠：{path}")
    workspace.mkdir(parents=True, exist_ok=True)
    if path == workspace / "recordings" and not existing:
        path.mkdir(exist_ok=True)
    if not path.is_dir():
        raise FileNotFoundError(f"录像目录不可用，请连接磁盘或检查设置：{path}")
    probe_directory(workspace)
    probe_directory(path, hardlink=True)
    if not existing:
        # 设置与首个数据库一同就位，保留已存在的数据目录。
        with tempfile.TemporaryDirectory(dir=workspace) as tmp:
            stage = Path(tmp) / "data"
            Store(stage / DB_NAME).close()
            atomic_settings(stage / "settings.json", {"recordings": str(path)})
            stage.rename(data)
    else:
        probe_directory(data)
        if str(path) != values["recordings"]:
            atomic_settings(settings, {**values, "recordings": str(path)})
    if not saved_pointer:
        atomic_settings(pointer, {"dataDirectory": str(workspace)})
    return data, upload, path


def start_upload(bundle, data, settings):
    upload_bundle = bundle / "上传"
    runtime = upload_bundle / "runtime/pythonw.exe"
    values = read_settings(settings)
    expected = values.get("upload_deployment")
    if expected is not None and (not isinstance(expected, str) or str(uuid.UUID(expected, version=4)) != expected):
        raise ValueError(f"上传部署编号损坏，请恢复 {settings}")
    args = [str(runtime), "-B", "-X", "utf8", "-m", "upload_platform.portable_window",
            "--bundle", str(upload_bundle), "--data-dir", str(data)]
    if expected:
        args += ["--expected-deployment", expected]
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("PYTHON")}
    process = subprocess.Popen(args, cwd=upload_bundle, env=env)
    home = Path(os.environ["LOCALAPPDATA"]) / "fileUploadPlatform" / "MuMu"
    opener = build_opener(ProxyHandler({}))
    until = time.monotonic() + 45
    while time.monotonic() < until and process.poll() is None:
        try:
            with opener.open("http://127.0.0.1:8765/api/v1/system/health", timeout=1) as response:
                health = json.load(response)
            deployment = health["deployment_id"]
            if (not isinstance(deployment, str) or str(uuid.UUID(deployment, version=4)) != deployment
                    or expected and expected != deployment):
                raise ValueError(f"本机上传平台正在使用其他部署，请核对：{data}")
            server, worker = (json.loads((home / (role + ".json")).read_text(encoding="utf-8"))
                              for role in ("server", "worker"))
            if not all(isinstance(record, dict) for record in (server, worker)):
                raise ValueError(f"上传平台启动记录损坏：{home}")
            for record in (server, worker):
                if record.get("deployment_id") == deployment and Path(record.get("data") or "") != data:
                    raise ValueError(f"上传平台数据目录冲突：{record.get('data')}\n请求目录：{data}")
            if (health.get("api") == health.get("database") == "ready"
                    and all(record.get("deployment_id") == deployment and Path(record.get("data") or "") == data
                            for record in (server, worker))
                    and any(row["active"] and row["id"] == worker["worker_id"] and row["pid"] == worker["pid"]
                            for row in health["workers"])):
                if not expected:
                    atomic_settings(settings, {**values, "upload_deployment": deployment})
                return
        except (OSError, KeyError, json.JSONDecodeError):
            pass
        time.sleep(.25)
    raise RuntimeError(f"上传平台未就绪，请查看上传启动窗口\n上传数据目录：{data}")


def main():
    parser = argparse.ArgumentParser(description="MuMu 便携录播")
    parser.add_argument("action", choices=("check", "start"))
    parser.add_argument("--combined", action="store_true")
    parser.add_argument("--recordings-dir", type=Path, help="专用录像根，需要已存在的绝对路径")
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
        bundle = BUNDLE.parent if args.combined or (
            BUNDLE.name == "录播" and (BUNDLE.parent / "启动MuMu.cmd").is_file()) else BUNDLE
        if overlaps(home, bundle):
            raise ValueError(f"本机启动锁目录与解压目录重叠，请另选解压位置：{home}")
        with closing(HostLease(home / "portable.lock")):
            workspace = workspace_for(bundle, home, args.recordings_dir)
            if workspace is None:
                print("已取消选择工作目录。")
                return 0
            data, upload, output = workspace
            if args.combined:
                try:
                    start_upload(bundle, upload, data / "settings.json")
                except (OSError, RuntimeError, ValueError, TypeError) as exc:
                    print(f"上传平台暂不可用：{exc}", file=sys.stderr)
            os.environ["MUMU_LBCUT_INSTANCE_LOCK_DIR"] = str(home / "instance-locks")
            print(f"录像目录 {output}\n运行数据 {data}\n"
                  "控制台 http://127.0.0.1:8000/\n按 Ctrl+C 平滑停止，等待尾片保存。", flush=True)
            return run(["run", "--data-dir", str(data), "--output-dir", str(output),
                        "--static-root", str(WEB), "--open-browser", "--require-local-tools",
                        "--segment-duration", str(args.segment_duration)])
    except (OSError, ValueError, RuntimeError, KeyError, ImportError, subprocess.SubprocessError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""扫码部署配置和当前 Windows 用户共享的启动记录。"""

import ctypes
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4

import psutil

from .upload_core_client import BridgeError

ROOT = Path(__file__).resolve().parents[2]


def local_path(value):
    p = Path(value)
    if not p.is_absolute() or p.drive.startswith("\\\\") or str(p).startswith("//"):
        raise ValueError
    p = p.resolve()
    if p.drive.startswith("\\\\"):
        raise ValueError
    return p


def snapshot(profile, *, new_directory=False):
    try:
        python, data, cfg = [
            local_path(profile[k]) for k in ("python", "data_dir", "engine_config")
        ]
        port = profile["port"]
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError
        if (
            python.name.lower() != "python.exe"
            or not python.is_file()
            or not (python.parent.parent / "pyvenv.cfg").is_file()
        ):
            raise ValueError
        if (
            (not new_directory and not data.is_dir())
            or not cfg.is_file()
            or cfg.stat().st_size > 65536
        ):
            raise ValueError
        engine = json.loads(cfg.read_text(encoding="utf-8-sig"))
        paths = {
            k: local_path(engine[k])
            for k in ("executable", "capabilities_file", "state_root")
        }
        state = paths["state_root"]
        if (
            state.is_relative_to(data)
            or data.is_relative_to(state)
            or data.is_relative_to(ROOT)
            or state.is_relative_to(ROOT)
        ):
            raise ValueError
        if (
            not paths["executable"].is_file()
            or not paths["capabilities_file"].is_file()
            or not isinstance(engine["build_id"], str)
        ):
            raise ValueError
        return {
            "python": str(python),
            "data_dir": str(data),
            "engine_config": str(cfg),
            "port": port,
            "engine": {k: str(v) for k, v in paths.items()}
            | {"build_id": engine["build_id"]},
        }
    except (ValueError, OSError, KeyError, TypeError):
        raise BridgeError(
            "configuration", "请核对本机虚拟环境、独立数据目录及引擎配置", 422
        ) from None


def registry():
    return (
        Path(os.environ["LOCALAPPDATA"])
        / "mumu_lbcut_tool"
        / "upload-core"
        / "registry.sqlite3"
    )


def boot_id():
    # Windows 本次启动的 BootIdentifier，比按当前时钟反推启动时间稳定。
    info = ctypes.create_string_buffer(32)
    if ctypes.windll.ntdll.NtQuerySystemInformation(90, info, len(info), None) != 0:
        raise BridgeError("boot_identity", "无法核对 Windows 启动身份")
    return info.raw[:16].hex()


@contextmanager
def transaction(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=4)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS deployments(
            id TEXT PRIMARY KEY, directory TEXT UNIQUE, port INTEGER UNIQUE, config TEXT,
            attempt TEXT, stage TEXT, boot REAL, host_pid INTEGER, host_time REAL,
            child_pid INTEGER, child_time REAL, listener INTEGER, listener_time REAL, started REAL)""")
        yield db
        db.commit()


def live(pid, created):
    if not pid:
        return None
    try:
        proc = psutil.Process(pid)
        return proc if proc.create_time() == created else None
    except psutil.NoSuchProcess:
        return None


def owners(port):
    return {
        c.pid
        for c in psutil.net_connections(kind="tcp")
        if c.status == psutil.CONN_LISTEN
        and c.laddr.port == port
        and c.laddr.ip in ("127.0.0.1", "0.0.0.0", "::")
    }


def state(row):
    if row["boot"] != boot_id():
        return "occupied" if owners(row["port"]) else "stopped"
    if row["stage"] in ("stopped", "failed"):
        return "occupied" if owners(row["port"]) else row["stage"]
    host = live(row["host_pid"], row["host_time"])
    child = live(row["child_pid"], row["child_time"])
    listener = live(row["listener"], row["listener_time"])
    pids = owners(row["port"])
    if listener and pids == {listener.pid}:
        return "online"
    if pids and not (
        child
        and pids.issubset({p.pid for p in [child, *child.children(recursive=True)]})
    ):
        return "occupied"
    if host or child or listener:
        return "starting" if time.time() - row["started"] < 30 else "check"
    if row["stage"] in ("creating", "spawned") and not row["listener"] or pids:
        return "check"
    return "stopped"


class CoreRuntime:
    def __init__(self, path):
        self.path = path

    def profile(self):
        if not self.path.exists():
            return None
        try:
            value = json.loads(self.path.read_text(encoding="utf8"))
            return {
                k: value[k]
                for k in ("id", "python", "data_dir", "port", "engine_config")
            } | {"selection": value.get("selection")}
        except (OSError, ValueError, KeyError, TypeError):
            raise BridgeError("configuration", "部署配置文件无法读取", 422) from None

    def configure(self, values, create, pending):
        with transaction(registry()) as db:
            old = self.profile()
            row = (
                db.execute(
                    "SELECT * FROM deployments WHERE id=?", (old["id"],)
                ).fetchone()
                if old
                else None
            )
            if (
                row
                and state(row) not in ("stopped", "failed")
                or old
                and pending(old["id"])
            ):
                raise BridgeError(
                    "deployment_busy", "先完成原部署的操作并由运维停止原服务"
                )
            cfg = snapshot(values, new_directory=create)
            data = Path(cfg["data_dir"])
            if create and (data.exists() or data.is_relative_to(ROOT)):
                raise BridgeError(
                    "directory_exists", "新建部署请选择仓外尚不存在的目录"
                )
            directory = os.path.normcase(cfg["data_dir"])
            existing = db.execute(
                "SELECT * FROM deployments WHERE directory=? OR port=?",
                (directory, cfg["port"]),
            ).fetchall()
            encoded = json.dumps(cfg, sort_keys=True)
            if not create and not existing and not (data / "core.sqlite3").is_file():
                raise BridgeError(
                    "empty_deployment",
                    "该目录没有已有 Core 库，如需空库请显式新建独立部署",
                )
            if existing and len(existing) == 1 and existing[0]["config"] == encoded:
                identifier = existing[0]["id"]
            elif not existing or (
                len(existing) == 1
                and row
                and existing[0]["id"] == row["id"]
                and directory == row["directory"]
            ):
                identifier = (
                    row["id"] if row and directory == row["directory"] else str(uuid4())
                )
                db.execute(
                    """INSERT INTO deployments(id,directory,port,config,stage,boot) VALUES(?,?,?,?,'stopped',?)
                    ON CONFLICT(id) DO UPDATE SET port=excluded.port,config=excluded.config""",
                    (identifier, directory, cfg["port"], encoded, boot_id()),
                )
            else:
                raise BridgeError("deployment_conflict", "目录或端口已绑定到其他部署")
            if create:
                data.mkdir(parents=True)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(str(uuid4()) + ".tmp")
            temp.write_text(
                json.dumps({k: cfg[k] for k in values} | {
                    "id": identifier, "selection": str(uuid4())
                }),
                encoding="utf8",
            )
            temp.replace(self.path)

    def inspect(self, ensure=False, *, posting=False):
        with transaction(registry()) as db:
            p = self.profile()
            if p is None:
                return {"state": "unconfigured"}
            row = db.execute(
                "SELECT * FROM deployments WHERE id=?", (p["id"],)
            ).fetchone()
            if not row or json.loads(row["config"]) != snapshot(p):
                raise BridgeError("binding", "部署与启动登记不一致，请核对配置")
            observed = state(row)
            if ensure and observed in ("stopped", "failed"):
                attempt = str(uuid4())
                proc = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "src.orchestrator.upload_core_host",
                        p["id"],
                        attempt,
                    ],
                    cwd=ROOT,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                db.execute(
                    """UPDATE deployments SET attempt=?,stage='intent',boot=?,started=?,host_pid=?,host_time=?,
                    child_pid=NULL,child_time=NULL,listener=NULL,listener_time=NULL WHERE id=?""",
                    (
                        attempt,
                        boot_id(),
                        time.time(),
                        proc.pid,
                        psutil.Process(proc.pid).create_time(),
                        p["id"],
                    ),
                )
                observed = "starting"
            info = {"state": observed, "deployment": p["id"], "port": p["port"]}
            if posting and observed == "online":
                info.update(
                    selection=p["selection"], listener=(row["listener"], row["listener_time"]),
                    config=json.loads(row["config"]),
                )
            return info

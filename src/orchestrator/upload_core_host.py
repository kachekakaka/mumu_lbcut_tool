"""无窗口宿主，仅启动已登记的 Core。先记录创建阶段，再记录真实监听进程。"""

import json
import os
import subprocess
import sys
import time

import psutil

from .upload_core_runtime import owners, registry, transaction


def main(identifier, attempt):
    with transaction(registry()) as db:
        row = db.execute(
            "SELECT * FROM deployments WHERE id=? AND attempt=? AND stage='intent'",
            (identifier, attempt),
        ).fetchone()
        # Windows venv 的 python.exe 可能是转发器，宿主是它直接启动的解释器。
        host = psutil.Process()
        if not row or not any(
            p.pid == row["host_pid"] and p.create_time() == row["host_time"]
            for p in [host, *host.parents()]
        ):
            return
        cfg = json.loads(row["config"])
        db.execute(
            "UPDATE deployments SET stage='creating',host_pid=?,host_time=? WHERE id=?",
            (host.pid, host.create_time(), identifier),
        )
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.upper().startswith("UPLOAD_PLATFORM_")
    }
    env.update(
        UPLOAD_PLATFORM_API_URL=f"http://127.0.0.1:{cfg['port']}",
        UPLOAD_PLATFORM_DATA_DIR=cfg["data_dir"],
        UPLOAD_PLATFORM_BILIBILI_CONFIG=cfg["engine_config"],
    )
    with subprocess.Popen(
        [cfg["python"], "-I", "-m", "upload_platform", "server"],
        cwd=cfg["data_dir"],
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) as child:
        proc = psutil.Process(child.pid)
        with transaction(registry()) as db:
            db.execute(
                "UPDATE deployments SET stage='spawned',child_pid=?,child_time=? WHERE id=? AND attempt=?",
                (proc.pid, proc.create_time(), identifier, attempt),
            )
        while child.poll() is None:
            pids = owners(cfg["port"])
            if len(pids) == 1:
                listener = next(
                    (
                        p
                        for p in [proc, *proc.children(recursive=True)]
                        if p.pid in pids
                    ),
                    None,
                )
                if listener:
                    with transaction(registry()) as db:
                        db.execute(
                            "UPDATE deployments SET listener=?,listener_time=? WHERE id=? AND attempt=?",
                            (listener.pid, listener.create_time(), identifier, attempt),
                        )
                    break
            time.sleep(0.25)
        returncode = child.wait()
        # 宿主持有原始进程句柄，正常观察到退出可解除启动占用。
        with transaction(registry()) as db:
            db.execute(
                "UPDATE deployments SET stage=? WHERE id=? AND attempt=?",
                ("failed" if returncode else "stopped", identifier, attempt),
            )


if __name__ == "__main__":
    main(*sys.argv[1:])

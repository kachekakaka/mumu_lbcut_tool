"""使用一次性 venv 中的合成 Core，核对真实 Windows 进程归属与启动竞争。"""

import concurrent.futures
import json
import os
import socket
import subprocess
import sys
import time
import venv

import pytest

from src.orchestrator.upload_core_runtime import CoreRuntime, registry, transaction


@pytest.mark.skipif(os.name != "nt", reason="使用 Windows 进程与控制台窗口")
def test_two_consoles_share_windowless_host(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "user"))
    env = tmp_path / "environment"
    venv.EnvBuilder(with_pip=False).create(env)
    module = env / "Lib/site-packages/upload_platform"
    module.mkdir(parents=True)
    (module / "__init__.py").write_text("")
    (module / "__main__.py").write_text(
        "import ctypes, os, socket, time\n"
        "from pathlib import Path\n"
        "from urllib.parse import urlsplit\n"
        'data=Path(os.environ["UPLOAD_PLATFORM_DATA_DIR"])\n'
        'sock=socket.socket();sock.bind(("127.0.0.1",urlsplit(os.environ["UPLOAD_PLATFORM_API_URL"]).port));sock.listen()\n'
        'with (data/"window").open("a") as f:f.write(str(ctypes.windll.kernel32.GetConsoleWindow())+"\\n")\n'
        "until=time.monotonic()+35\n"
        'while time.monotonic()<until and not (data/"test-exit").exists():time.sleep(.1)\n',
        encoding="utf8",
    )
    cfg = tmp_path / "engine.json"
    caps = tmp_path / "capabilities.json"
    caps.write_text("{}")
    cfg.write_text(
        json.dumps(
            {
                "executable": sys.executable,
                "capabilities_file": str(caps),
                "state_root": str(tmp_path / "private"),
                "build_id": "fixture",
            }
        )
    )
    data = tmp_path / "data"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    profiles = [
        CoreRuntime(tmp_path / name / "profile.json")
        for name in ("console1", "console2")
    ]
    values = {
        "python": str(env / "Scripts/python.exe"),
        "data_dir": str(data),
        "port": port,
        "engine_config": str(cfg),
    }
    for i, runtime in enumerate(profiles):
        runtime.configure(values, i == 0, lambda _: False)

    def ensure(runtime):
        code = "from pathlib import Path; from src.orchestrator.upload_core_runtime import CoreRuntime;import sys; CoreRuntime(Path(sys.argv[1])).inspect(True)"
        subprocess.run(
            [sys.executable, "-c", code, str(runtime.path)], check=True, timeout=20
        )

    try:
        with concurrent.futures.ThreadPoolExecutor() as pool:
            list(pool.map(ensure, profiles))
        until = time.monotonic() + 15
        while profiles[0].inspect()["state"] != "online" and time.monotonic() < until:
            time.sleep(0.15)
        assert profiles[0].inspect() == profiles[1].inspect(True)
        assert profiles[0].inspect()["state"] == "online"
        assert (data / "window").read_text().splitlines() == ["0"]
        with transaction(registry()) as db:
            row = db.execute("SELECT * FROM deployments").fetchone()
            assert row["host_pid"] and row["child_pid"] and row["listener"]
    finally:
        (data / "test-exit").touch()
        until = time.monotonic() + 10
        while profiles[0].inspect()["state"] != "stopped" and time.monotonic() < until:
            time.sleep(0.1)

"""模拟器与 ADB 常驻进程工作目录隔离专项测试 (K14) - TDD。"""
import os
from pathlib import Path
import subprocess
from unittest.mock import MagicMock, patch
import pytest

from src.mvp.app_launcher import AppLauncher
from src.mvp.config import get_mumu_manager_exe, get_adb_exe
from src.mvp.mumu_client import MuMuClient
from src.mvp.stream_recorder import StreamRecorder
from src.orchestrator.desktop import (
    get_windows_system_dir,
    reveal_in_file_manager,
    run_file_manager,
)
from src.worker.adb_client import AdbDeviceClient
from src.worker.continuous_pipeline import ContinuousPipeline
from src.worker.continuous_session import ContinuousSession
from src.worker.executor import run_command
from src.worker.navigator import WorkerNavigator


@pytest.fixture
def fake_mumu_env(tmp_path):
    """创建包含模拟器和 ADB 伪造可执行文件的测试沙箱（包含中文和空格）。"""
    sandbox = tmp_path / "测试 目录 with spaces"
    mumu_dir = sandbox / "MuMu" / "nx_main"
    mumu_dir.mkdir(parents=True, exist_ok=True)
    manager_exe = mumu_dir / "MuMuManager.exe"
    manager_exe.write_text("fake manager", encoding="utf-8")
    main_exe = mumu_dir / "MuMuNxMain.exe"
    main_exe.write_text("fake main", encoding="utf-8")
    adb_exe = mumu_dir / "adb.exe"
    adb_exe.write_text("fake adb", encoding="utf-8")
    return {
        "sandbox": sandbox,
        "manager_exe": manager_exe,
        "main_exe": main_exe,
        "adb_exe": adb_exe,
    }


def test_mumu_client_cwd_isolation(fake_mumu_env):
    """验证 MuMuClient 各入口显式传入 cwd 且父进程 CWD 未变。"""
    orig_cwd = os.getcwd()
    manager_exe = fake_mumu_env["manager_exe"]
    adb_exe = fake_mumu_env["adb_exe"]

    client = MuMuClient(manager_exe=manager_exe, adb_exe=adb_exe)
    assert client.manager_exe.is_absolute()
    assert client.adb_exe.is_absolute()

    captured_runs = []

    def mock_run(cmd, *args, **kwargs):
        captured_runs.append((list(cmd), kwargs.get("cwd")))
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "{}"
        mock_res.stderr = ""
        return mock_res

    # 1. 验证 _run_manager 传入 cwd=manager_exe.parent
    with patch("subprocess.run", side_effect=mock_run):
        client._run_manager("info", "-v", "all")
        assert len(captured_runs) == 1
        cmd, cwd = captured_runs[0]
        assert Path(cmd[0]) == manager_exe
        assert Path(cwd) == manager_exe.parent

    # 2. 验证 connect_adb 中 adb connect 与 adb devices 均带 cwd=adb_exe.parent
    captured_runs.clear()
    devices_output = "List of devices attached\n127.0.0.1:16384\tdevice\n"

    def mock_run_connect(cmd, *args, **kwargs):
        captured_runs.append((list(cmd), kwargs.get("cwd")))
        mock_res = MagicMock()
        mock_res.returncode = 0
        cmd_str = " ".join(cmd)
        mock_res.stdout = devices_output if "devices" in cmd_str else "{}"
        mock_res.stderr = ""
        return mock_res

    with patch("subprocess.run", side_effect=mock_run_connect):
        serial = client.connect_adb(0, retries=1)
        assert serial == "127.0.0.1:16384"
        assert len(captured_runs) >= 3
        connect_call = next(c for c in captured_runs if "connect" in c[0] and Path(c[0][0]) == adb_exe)
        assert Path(connect_call[1]) == adb_exe.parent
        devices_call = next(c for c in captured_runs if "devices" in c[0] and Path(c[0][0]) == adb_exe)
        assert Path(devices_call[1]) == adb_exe.parent

    # 3. 验证 ensure_main_process_running 拉起 MuMuNxMain.exe 携带 cwd=manager_exe.parent
    captured_popens = []

    def mock_popen(cmd, *args, **kwargs):
        captured_popens.append((list(cmd), kwargs.get("cwd")))
        return MagicMock()

    with patch("subprocess.run", return_value=MagicMock(stdout="no main process", return_value=0)), \
         patch("subprocess.Popen", side_effect=mock_popen):
        client.ensure_main_process_running(timeout_sec=0.1)
        assert len(captured_popens) == 1
        assert Path(captured_popens[0][1]) == manager_exe.parent
        assert Path(captured_popens[0][0][0]) == fake_mumu_env["main_exe"]

    assert os.getcwd() == orig_cwd


def test_mumu_client_missing_paths():
    """验证缺失 manager_exe 或 adb_exe 时抛出明确 FileNotFoundError。"""
    with pytest.raises(FileNotFoundError, match="未找到 MuMuManager.exe"):
        MuMuClient(manager_exe=Path("D:/nonexistent/manager.exe"), adb_exe=Path("D:/nonexistent/adb.exe"))


def test_app_launcher_cwd_and_path_resolution(fake_mumu_env):
    """验证 AppLauncher 的 ADB 初次调用、重试重连及截图 pull 路径与 cwd。"""
    orig_cwd = os.getcwd()
    adb_exe = fake_mumu_env["adb_exe"]
    launcher = AppLauncher(serial="127.0.0.1:16384", adb_exe=adb_exe)
    assert launcher.adb_exe.is_absolute()

    captured_runs = []

    def mock_run(cmd, *args, **kwargs):
        captured_runs.append((list(cmd), kwargs.get("cwd")))
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "device not found" if len(captured_runs) == 1 else "ok"
        mock_res.stderr = ""
        return mock_res

    # 测试 _adb 重连分支
    with patch("subprocess.run", side_effect=mock_run):
        out = launcher._adb("shell", "echo", "1")
        assert len(captured_runs) == 3  # 初次失败 -> connect -> 重试
        for call_cmd, call_cwd in captured_runs:
            assert call_cwd == adb_exe.parent

    # 测试 capture_screenshot 相对路径先被解析为绝对路径
    captured_runs.clear()
    rel_path = Path("relative/path/to/shot.png")
    expected_abs = rel_path.resolve()

    def mock_screenshot_run(cmd, *args, **kwargs):
        captured_runs.append((list(cmd), kwargs.get("cwd")))
        expected_abs.parent.mkdir(parents=True, exist_ok=True)
        expected_abs.write_bytes(b"fake image")
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = ""
        mock_res.stderr = ""
        return mock_res

    try:
        with patch("subprocess.run", side_effect=mock_screenshot_run):
            ok = launcher.capture_screenshot(rel_path)
            assert ok is True
            pull_call = next(c for c in captured_runs if "pull" in c[0])
            assert pull_call[1] == adb_exe.parent
            assert pull_call[0][-1] == str(expected_abs)
    finally:
        if expected_abs.exists():
            expected_abs.unlink()
        if expected_abs.parent.exists() and not any(expected_abs.parent.iterdir()):
            expected_abs.parent.rmdir()

    assert os.getcwd() == orig_cwd


def test_adb_device_client_cwd(fake_mumu_env):
    """验证 AdbDeviceClient run_adb 与 ensure_connected 显式指定 cwd。"""
    orig_cwd = os.getcwd()
    adb_exe = fake_mumu_env["adb_exe"]
    client = AdbDeviceClient(serial="127.0.0.1:16384", adb_exe=adb_exe)
    assert client.adb_exe.is_absolute()

    captured_cmds = []

    def mock_run_cmd(cmd, timeout=None, check=False, cwd=None, env=None):
        captured_cmds.append((list(cmd), cwd))
        from src.worker.executor import CommandResult
        return CommandResult(exit_code=0, stdout="device\n", stderr="", duration=0.01)

    with patch("src.worker.adb_client.run_command", side_effect=mock_run_cmd):
        res = client.run_adb("get-state")
        assert res.exit_code == 0
        assert captured_cmds[0][1] == adb_exe.parent

        captured_cmds.clear()
        ok = client.ensure_connected(timeout=2.0)
        assert ok is True
        connect_call = next(c for c in captured_cmds if "connect" in c[0])
        assert connect_call[1] == adb_exe.parent

    # 验证通用执行器 run_command 的默认 cwd 仍为 None，未被全局篡改
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (b"ok", b"")
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc
        run_command(["cmd.exe", "/c", "echo", "test"])
        assert mock_popen.call_args[1]["cwd"] is None

    assert os.getcwd() == orig_cwd


def test_continuous_session_device_online_cwd(fake_mumu_env):
    """验证 ContinuousSession._adb_device_online 传入 cwd。"""
    orig_cwd = os.getcwd()
    adb_exe = fake_mumu_env["adb_exe"]
    scrcpy_exe = fake_mumu_env["sandbox"] / "tools" / "scrcpy" / "scrcpy.exe"
    scrcpy_exe.parent.mkdir(parents=True, exist_ok=True)
    scrcpy_exe.write_text("fake scrcpy", encoding="utf-8")

    controller = MagicMock()
    session = ContinuousSession(
        controller=controller,
        serial="127.0.0.1:16384",
        scrcpy_exe=scrcpy_exe,
        adb_exe=adb_exe,
        media_tools=MagicMock(),
    )
    assert session.adb.is_absolute()

    captured_runs = []

    def mock_run(cmd, *args, **kwargs):
        captured_runs.append((list(cmd), kwargs.get("cwd")))
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "device\n"
        mock_res.stderr = ""
        return mock_res

    with patch("subprocess.run", side_effect=mock_run):
        assert session._adb_device_online() is True
        assert len(captured_runs) == 1
        assert captured_runs[0][1] == adb_exe.parent

    assert os.getcwd() == orig_cwd


def test_worker_navigator_cwd_and_screenshot(fake_mumu_env):
    """验证 WorkerNavigator 截屏与内存截帧传入 cwd=adb.parent。"""
    orig_cwd = os.getcwd()
    adb_exe = fake_mumu_env["adb_exe"]
    nav = WorkerNavigator(serial="127.0.0.1:16384", adb_exe=adb_exe)
    assert nav.adb_exe.is_absolute()

    captured_runs = []
    rel_path = Path("nav_shots/test.png")
    expected_abs = rel_path.resolve()

    def mock_run(cmd, *args, **kwargs):
        captured_runs.append((list(cmd), kwargs.get("cwd")))
        expected_abs.parent.mkdir(parents=True, exist_ok=True)
        expected_abs.write_bytes(b"shot")
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "device"
        mock_res.stderr = ""
        return mock_res

    try:
        with patch.object(nav, "_adb"), patch("subprocess.run", side_effect=mock_run):
            ok = nav.capture_screenshot(rel_path)
            assert ok is True
            pull_call = next(c for c in captured_runs if "pull" in c[0])
            assert pull_call[1] == adb_exe.parent
            assert pull_call[0][-1] == str(expected_abs)
    finally:
        if expected_abs.exists():
            expected_abs.unlink()
        if expected_abs.parent.exists() and not any(expected_abs.parent.iterdir()):
            expected_abs.parent.rmdir()

    # 内存截帧
    captured_runs.clear()

    def mock_frame_run(cmd, *args, **kwargs):
        captured_runs.append((list(cmd), kwargs.get("cwd")))
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = b"raw bytes"
        mock_res.stderr = b""
        return mock_res

    with patch("subprocess.run", side_effect=mock_frame_run):
        nav._capture_frame_array()
        assert len(captured_runs) == 1
        assert captured_runs[0][1] == adb_exe.parent

    assert os.getcwd() == orig_cwd


def test_continuous_pipeline_scrcpy_cwd_and_env(fake_mumu_env, tmp_path):
    """验证 ContinuousPipeline 启动 scrcpy 指定 cwd=adb.parent 及 server 环境变量。"""
    orig_cwd = os.getcwd()
    adb_exe = fake_mumu_env["adb_exe"]
    scrcpy_exe = fake_mumu_env["sandbox"] / "tools" / "scrcpy" / "scrcpy.exe"
    scrcpy_server = fake_mumu_env["sandbox"] / "tools" / "scrcpy" / "scrcpy-server"
    scrcpy_exe.parent.mkdir(parents=True, exist_ok=True)
    scrcpy_exe.write_text("fake scrcpy", encoding="utf-8")
    scrcpy_server.write_text("fake server", encoding="utf-8")
    output_dir = tmp_path / "pipeline_out"

    pipe = ContinuousPipeline(
        serial="127.0.0.1:16384",
        output_dir=output_dir,
        scrcpy_exe=scrcpy_exe,
        adb_exe=adb_exe,
        segment_duration_sec=10,
        ffmpeg_exe="ffmpeg",
    )
    assert pipe.adb_exe.is_absolute()
    assert pipe.scrcpy_exe.is_absolute()

    captured_popens = []

    def mock_popen(cmd, *args, **kwargs):
        captured_popens.append({
            "cmd": list(cmd),
            "cwd": kwargs.get("cwd"),
            "env": kwargs.get("env"),
        })
        mock_p = MagicMock()
        mock_p.poll.return_value = None
        mock_p.stdout.readline.return_value = b""
        return mock_p

    frag = MagicMock()
    frag.path.exists.return_value = True
    frag.path.stat.return_value.st_size = 2048

    with patch("subprocess.Popen", side_effect=mock_popen), \
         patch("src.worker.continuous_pipeline.NamedPipeBridge"), \
         patch("src.worker.continuous_pipeline.StreamMonitor"), \
         patch("src.worker.continuous_pipeline.PipelineLogDrainer"), \
         patch.object(pipe, "fragments", return_value=[frag]), \
         patch.object(pipe, "is_recording", return_value=True):
        pipe.start(timeout_sec=0.1)

    scrcpy_call = next(c for c in captured_popens if Path(c["cmd"][0]) == scrcpy_exe)
    assert Path(scrcpy_call["cwd"]) == adb_exe.parent
    assert Path(scrcpy_call["env"]["ADB"]) == adb_exe
    assert Path(scrcpy_call["env"]["SCRCPY_SERVER_PATH"]) == scrcpy_server
    assert scrcpy_call["env"]["PATH"].lower().startswith(str(adb_exe.parent).lower())

    assert os.getcwd() == orig_cwd


def test_stream_recorder_scrcpy_cwd_and_output_path(fake_mumu_env, tmp_path):
    """验证 StreamRecorder 启动 scrcpy 指定 cwd=adb.parent 且输出路径为绝对路径。"""
    orig_cwd = os.getcwd()
    adb_exe = fake_mumu_env["adb_exe"]
    scrcpy_exe = fake_mumu_env["sandbox"] / "tools" / "scrcpy" / "scrcpy.exe"
    scrcpy_server = fake_mumu_env["sandbox"] / "tools" / "scrcpy" / "scrcpy-server"
    scrcpy_exe.parent.mkdir(parents=True, exist_ok=True)
    scrcpy_exe.write_text("fake scrcpy", encoding="utf-8")
    scrcpy_server.write_text("fake server", encoding="utf-8")
    rel_out = Path("recordings/test.mp4")

    rec = StreamRecorder(
        serial="127.0.0.1:16384",
        output_path=rel_out,
        scrcpy_exe=scrcpy_exe,
        adb_exe=adb_exe,
    )
    assert rec.output_path.is_absolute()
    assert rec.scrcpy_exe.is_absolute()
    assert rec.adb_exe.is_absolute()

    captured_popens = []

    def mock_popen(cmd, *args, **kwargs):
        captured_popens.append({
            "cmd": list(cmd),
            "cwd": kwargs.get("cwd"),
            "env": kwargs.get("env"),
        })
        mock_p = MagicMock()
        mock_p.poll.return_value = None
        return mock_p

    with patch("subprocess.Popen", side_effect=mock_popen), \
         patch("time.sleep"):
        ok = rec.start()
        assert ok is True

    scrcpy_call = captured_popens[0]
    assert Path(scrcpy_call["cwd"]) == adb_exe.parent
    assert Path(scrcpy_call["env"]["ADB"]) == adb_exe
    assert Path(scrcpy_call["env"]["SCRCPY_SERVER_PATH"]) == scrcpy_server
    # 验证 record 参数传入的是绝对路径，而非相对路径
    record_arg = next(arg for arg in scrcpy_call["cmd"] if arg.startswith("--record="))
    assert record_arg == f"--record={rec.output_path}"

    assert os.getcwd() == orig_cwd


def test_stream_recorder_adb_missing_or_invalid_on_windows(fake_mumu_env, tmp_path):
    """验证 Windows 下 StreamRecorder 在 ADB 路径缺失或无效时明确报错，不启动 scrcpy 也不继承 CWD。"""
    orig_cwd = os.getcwd()
    scrcpy_exe = fake_mumu_env["sandbox"] / "tools" / "scrcpy" / "scrcpy.exe"
    scrcpy_exe.parent.mkdir(parents=True, exist_ok=True)
    scrcpy_exe.write_text("fake scrcpy", encoding="utf-8")
    rel_out = tmp_path / "out.mp4"

    with patch("os.name", "nt"), patch("subprocess.Popen") as mock_popen:
        # 1. adb_exe 为 None (且 get_adb_exe 返回 None)
        with patch("src.mvp.stream_recorder.get_adb_exe", return_value=None):
            rec_none = StreamRecorder(serial="127.0.0.1:16384", output_path=rel_out, scrcpy_exe=scrcpy_exe, adb_exe=None)
            with pytest.raises(FileNotFoundError, match="未配置 adb.exe 路径"):
                rec_none.start()

        # 2. adb_exe 路径不存在
        nonexistent_adb = tmp_path / "nonexistent" / "adb.exe"
        rec_missing = StreamRecorder(serial="127.0.0.1:16384", output_path=rel_out, scrcpy_exe=scrcpy_exe, adb_exe=nonexistent_adb)
        with pytest.raises(FileNotFoundError, match="未找到 adb.exe"):
            rec_missing.start()

        # 3. adb_exe 为目录 (无效路径)
        dir_adb = tmp_path / "fake_adb_dir"
        dir_adb.mkdir(parents=True, exist_ok=True)
        rec_invalid = StreamRecorder(serial="127.0.0.1:16384", output_path=rel_out, scrcpy_exe=scrcpy_exe, adb_exe=dir_adb)
        with pytest.raises(RuntimeError, match="无效的 adb.exe 路径"):
            rec_invalid.start()

        # 确保上述异常情况下，subprocess.Popen 均未被触发，scrcpy 从未启动
        assert mock_popen.call_count == 0

    assert os.getcwd() == orig_cwd


def test_desktop_file_manager_windows_system_dir():
    """验证 Windows 分支 Explorer 传入系统目录 cwd 且相对路径被规范为绝对路径。"""
    orig_cwd = os.getcwd()
    target = Path("some_dir/file.mp4")
    expected_target = target.resolve()

    captured_popens = []

    def mock_popen(cmd, *args, **kwargs):
        captured_popens.append((list(cmd), kwargs.get("cwd")))
        return MagicMock()

    with patch("platform.system", return_value="Windows"), \
         patch("subprocess.Popen", side_effect=mock_popen):
        reveal_in_file_manager(target, is_file=True)

        assert len(captured_popens) == 1
        cmd, cwd = captured_popens[0]
        assert cmd[0] == "explorer.exe"
        assert f"/select,{expected_target}" in cmd[1]
        assert cwd is not None
        # 验证 cwd 是系统 Windows 目录
        sys_dir = get_windows_system_dir()
        assert Path(cwd) == sys_dir

    # 验证系统目录缺失时明确报错，不吞异常，不可回退到当前目录
    with patch.dict(os.environ, {"SystemRoot": "", "windir": ""}, clear=True), \
         patch("platform.system", return_value="Windows"):
        with pytest.raises(RuntimeError, match="无法获取 Windows 系统目录"):
            run_file_manager(["explorer.exe"])

    assert os.getcwd() == orig_cwd

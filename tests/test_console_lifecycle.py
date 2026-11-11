"""独立 Windows 宿主：端口、主控锁、信号、故障和查询 Job Object 归属。"""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import psutil
import pytest
import requests

from src.orchestrator.storage import Store

HOST = Path(__file__).parent / 'fixtures' / 'console_host.py'
pytestmark = pytest.mark.skipif(os.name != 'nt', reason='本组验证 Windows 原生生命周期')


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def spawn(root, number, mode='normal', new_console=False):
    root.mkdir(parents=True, exist_ok=True)
    log = (root / f'host-{number}.log').open('wb')
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    proc = subprocess.Popen([sys.executable, str(HOST), str(root), str(number), '--mode', mode],
        stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NEW_CONSOLE if new_console else subprocess.CREATE_NEW_PROCESS_GROUP,
        startupinfo=startup, env=dict(os.environ, PYTHONIOENCODING='utf-8'))
    log.close()
    return proc


def ready(proc, number):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and proc.poll() is None:
        try:
            response = requests.get(f'http://127.0.0.1:{number}/api/system', timeout=.3)
            if response.status_code == 200 and response.json()['scheduler_running']:
                return
        except requests.RequestException:
            pass
        time.sleep(.05)
    raise AssertionError(f'测试服务未就绪，退出码 {proc.poll()}')


def stop(proc):
    if proc.poll() is None:
        proc.send_signal(signal.CTRL_BREAK_EVENT)
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)


def test_port_conflict_does_not_create_database(tmp_path):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        sock.listen()
        child = spawn(tmp_path, sock.getsockname()[1])
        assert child.wait(15) != 0
        assert not (tmp_path / 'state' / 'orchestrator.sqlite3').exists()


def test_second_host_does_not_interrupt_commands(tmp_path):
    first_port, second_port = port(), port()
    child = spawn(tmp_path, first_port)
    try:
        ready(child, first_port)
        # 注入已在等待查询的命令，第二个主控必须在更改它之前被锁拒绝。
        store = Store(tmp_path / 'state' / 'orchestrator.sqlite3')
        with store.transaction() as db:
            db.execute("INSERT INTO commands(id,request_id,kind,subject,payload_json,status,created_at) VALUES('held','held','record','none','{}','running',1)")
        other = spawn(tmp_path, second_port)
        assert other.wait(15) != 0
        assert store.row("SELECT status FROM commands WHERE id='held'")['status'] == 'running'
        store.close()
    finally:
        stop(child)
    assert child.returncode == 0


@pytest.mark.parametrize('mode', ['init_failure', 'fatal'])
def test_initialization_and_scheduler_failures_exit_nonzero(tmp_path, mode):
    child = spawn(tmp_path, port(), mode)
    assert child.wait(20) != 0
    # 失败资源释放后，同目录可再次启动。
    number = port()
    next_child = spawn(tmp_path, number)
    try:
        ready(next_child, number)
    finally:
        stop(next_child)
    assert next_child.returncode == 0


@pytest.mark.parametrize('sig', ['break', 'ctrl_c'])
def test_windows_signals_reclaim_all_queries(tmp_path, sig):
    number = port()
    child = spawn(tmp_path, number, 'blocked', new_console=sig == 'ctrl_c')
    try:
        ready(child, number)
        deadline = time.monotonic() + 5
        pids = []
        while time.monotonic() < deadline:
            try:
                pids = json.loads((tmp_path / 'pids.json').read_text(encoding='utf-8'))
            except (FileNotFoundError, ValueError):
                pass
            if len(pids) == 8:
                break
            time.sleep(.05)
        assert len(pids) == 8
        started = time.monotonic()
        if sig == 'ctrl_c':
            subprocess.run([sys.executable, str(HOST.with_name('console_signal.py')), str(child.pid)], check=True, timeout=5)
        else:
            child.send_signal(signal.CTRL_BREAK_EVENT)
        assert child.wait(10) == 0
        assert time.monotonic() - started < 7
        assert not any(psutil.pid_exists(pid) for pid in pids)
    finally:
        stop(child)


def test_forced_host_death_kills_query_job_only(tmp_path):
    number = port()
    child = spawn(tmp_path, number, 'blocked')
    sentinel = None
    try:
        ready(child, number)
        deadline = time.monotonic() + 5
        pids = []
        while len(pids) != 8 and time.monotonic() < deadline:
            try:
                pids = json.loads((tmp_path / 'pids.json').read_text(encoding='utf-8'))
            except (FileNotFoundError, ValueError):
                pass
            time.sleep(.05)
        assert len(pids) == 8
        identity = json.loads((tmp_path / 'worker.json').read_text(encoding='utf-8'))
        sentinel = psutil.Process(identity['pid'])
        assert sentinel.create_time() == identity['born']
        child.kill()
        child.wait(5)
        deadline = time.monotonic() + 5
        while any(psutil.pid_exists(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(.05)
        assert not any(psutil.pid_exists(pid) for pid in pids)
        assert sentinel.is_running(), '模拟录制 Worker 不得属于查询 Job Object'
    finally:
        stop(child)
        if sentinel and sentinel.is_running():
            sentinel.terminate()
            sentinel.wait(5)

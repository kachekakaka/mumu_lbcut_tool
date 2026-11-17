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


def spawn(root, number, mode='normal', new_console=False, host='127.0.0.1', open_browser=False, browser='probe'):
    root.mkdir(parents=True, exist_ok=True)
    log = (root / f'host-{number}.log').open('wb')
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    args = [sys.executable, str(HOST), str(root), str(number), '--mode', mode, '--host', host, '--browser', browser]
    if open_browser:
        args.append('--open-browser')
    proc = subprocess.Popen(args,
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


def test_wildcard_host_serves_loopback_and_local_interface(tmp_path):
    addresses = {item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    addresses = sorted(address for address in addresses if not address.startswith('127.'))
    if not addresses:
        pytest.skip('没有可用于验证的非回环 IPv4 地址')
    number = port()
    child = spawn(tmp_path, number, host='0.0.0.0')
    try:
        ready(child, number)
        session = requests.Session()
        session.trust_env = False
        for address in ('127.0.0.1', addresses[0]):
            response = session.get(f'http://{address}:{number}/api/system', timeout=3)
            assert response.status_code == 200
            assert response.json()['scheduler_running']
    finally:
        stop(child)
    assert child.returncode == 0


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
            if len(pids) == 9:
                break
            time.sleep(.05)
        assert len(pids) == 9
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
        while len(pids) != 9 and time.monotonic() < deadline:
            try:
                pids = json.loads((tmp_path / 'pids.json').read_text(encoding='utf-8'))
            except (FileNotFoundError, ValueError):
                pass
            time.sleep(.05)
        assert len(pids) == 9
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


def browser_page(root):
    (root / 'web').mkdir(parents=True)
    (root / 'web' / 'index.html').write_text('<h1>隔离切片页</h1>', encoding='utf-8')


def wait_file(child, path):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and child.poll() is None:
        if path.exists() and path.stat().st_size:
            return
        time.sleep(.03)
    raise AssertionError(f'等待文件失败：{path}，退出码 {child.poll()}')


@pytest.mark.parametrize('browser', ['probe', 'block', 'false', 'error'])
def test_browser_opens_after_real_http_and_does_not_block_shutdown(tmp_path, browser):
    browser_page(tmp_path)
    number = port()
    child = spawn(tmp_path, number, open_browser=True, browser=browser, host='0.0.0.0')
    try:
        wait_file(child, tmp_path / 'browser.jsonl')
        ready(child, number)  # 包括浏览器调用一直阻塞时的 HTTP 响应。
    finally:
        stop(child)
    assert child.returncode == 0
    calls = [json.loads(line) for line in (tmp_path / 'browser.jsonl').read_text(encoding='utf-8').splitlines()]
    assert len(calls) == 1
    assert calls[0]['url'] == f'http://127.0.0.1:{number}/segments'
    assert calls[0]['system']['scheduler_running'] is True
    assert calls[0]['page_status'] == 200 and '隔离切片页' in calls[0]['page_body']
    if browser in ('false', 'error'):
        assert '请手动访问' in (tmp_path / f'host-{number}.log').read_text(encoding='utf-8')


def test_browser_waits_for_slow_initialization(tmp_path):
    browser_page(tmp_path)
    number = port()
    child = spawn(tmp_path, number, mode='init_wait', open_browser=True)
    try:
        deadline = time.monotonic() + 15
        while not (tmp_path / 'init-entered').exists() and child.poll() is None:
            assert time.monotonic() < deadline
            time.sleep(.03)
        assert child.poll() is None
        time.sleep(2.1)  # 明确跨过旧入口的两秒盲等边界，初始化仍由事件控制。
        assert not (tmp_path / 'browser.jsonl').exists()
        (tmp_path / 'init-release').touch()
        wait_file(child, tmp_path / 'browser.jsonl')
    finally:
        (tmp_path / 'init-release').touch()
        stop(child)
    assert child.returncode == 0


@pytest.mark.parametrize('scenario', ['disabled', 'missing_frontend', 'init_failure'])
def test_no_browser_for_disabled_missing_frontend_or_failed_startup(tmp_path, scenario):
    if scenario != 'missing_frontend':
        browser_page(tmp_path)
    number = port()
    child = spawn(tmp_path, number, mode='init_failure' if scenario == 'init_failure' else 'normal',
                  open_browser=scenario != 'disabled')
    try:
        if scenario == 'init_failure':
            assert child.wait(15) != 0
        else:
            ready(child, number)
            if scenario == 'missing_frontend':
                assert requests.get(f'http://127.0.0.1:{number}/segments', timeout=3).status_code == 503
    finally:
        stop(child)
    assert not (tmp_path / 'browser.jsonl').exists()


@pytest.mark.parametrize('conflict', ['port', 'data'])
def test_existing_healthy_service_does_not_trigger_new_browser(tmp_path, conflict):
    first = tmp_path / '原服务'
    browser_page(first)
    number = port()
    child = spawn(first, number)
    other = None
    try:
        ready(child, number)
        second = first if conflict == 'data' else tmp_path / '新服务'
        if second != first:
            browser_page(second)
        other = spawn(second, port() if conflict == 'data' else number, open_browser=True)
        assert other.wait(15) != 0
        assert not (second / 'browser.jsonl').exists()
        ready(child, number)
        if conflict == 'port':
            assert not (second / 'state' / 'orchestrator.sqlite3').exists()
    finally:
        if other is not None:
            stop(other)
        stop(child)
    assert child.returncode == 0

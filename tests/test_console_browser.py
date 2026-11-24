"""浏览器打开边界：地址、HTTP 启动顺序、一次派发与失败隔离。"""
import asyncio
import threading

import pytest
import uvicorn

from src.orchestrator import cli, server


@pytest.mark.parametrize(('host', 'port', 'expected'), [
    ('127.0.0.1', 8000, 'http://127.0.0.1:8000/segments'),
    ('0.0.0.0', 8123, 'http://127.0.0.1:8123/segments'),
    ('192.168.3.250', 8123, 'http://192.168.3.250:8123/segments'),
    ('::1', 8123, 'http://[::1]:8123/segments'),
])
def test_browser_url(tmp_path, host, port, expected):
    config = server.ServerConfig(tmp_path / "data", tmp_path / "recordings", host=host, port=port)
    config.validate()
    assert server.browser_url(config) == expected


@pytest.fixture
def console(tmp_path, monkeypatch):
    static = tmp_path / 'web'
    static.mkdir()
    (static / 'index.html').write_text('<h1>切片测试</h1>', encoding='utf-8')
    config = server.ServerConfig(tmp_path / 'state', tmp_path / 'out',
                                 static_root=static, open_browser=True)
    runtime = server.Runtime(config)
    runtime.state = 'running'
    runtime.thread = threading.current_thread()
    instance = server.ConsoleServer(uvicorn.Config(server.create_app(config, runtime)), runtime)
    calls = []

    async def startup(self, sockets=None):
        assert calls == []  # 必须先完成父类 HTTP 启动。
        self.started = True

    monkeypatch.setattr(uvicorn.Server, 'startup', startup)
    monkeypatch.setattr(server.webbrowser, 'open', lambda url: calls.append(url) or True)
    return instance, calls


def immediate_threads(monkeypatch, pending=None, fail=False):
    class Thread:
        def __init__(self, *, target, args, name, daemon):
            assert daemon is True
            self.target, self.args = target, args

        def start(self):
            if fail:
                raise RuntimeError('线程资源不足')
            if pending is None:
                self.target(*self.args)
            else:
                pending.append(lambda: self.target(*self.args))
    monkeypatch.setattr(server.threading, 'Thread', Thread)


def test_open_after_startup_only_once(console, monkeypatch):
    instance, calls = console
    immediate_threads(monkeypatch)
    asyncio.run(instance.startup())
    assert calls == ['http://127.0.0.1:8000/segments']

    async def already_started(self, sockets=None):
        pass
    monkeypatch.setattr(uvicorn.Server, 'startup', already_started)
    asyncio.run(instance.startup())
    assert len(calls) == 1


@pytest.mark.parametrize('reason', ['disabled', 'missing_frontend', 'closing', 'failed', 'dead', 'stopped', 'exit', 'not_started'])
def test_no_browser_when_not_eligible(console, monkeypatch, reason, caplog):
    instance, calls = console
    runtime = instance.runtime
    if reason == 'disabled':
        runtime.config.open_browser = False
    elif reason == 'missing_frontend':
        (runtime.config.static_root / 'index.html').unlink()
    elif reason == 'closing':
        runtime.state = 'closing'
    elif reason == 'failed':
        runtime.failure = RuntimeError('失败')
    elif reason == 'dead':
        runtime.thread = None
    elif reason == 'stopped':
        runtime.stop.set()
    elif reason == 'exit':
        instance.should_exit = True
    else:
        async def not_started(self, sockets=None):
            pass
        monkeypatch.setattr(uvicorn.Server, 'startup', not_started)
    immediate_threads(monkeypatch)
    asyncio.run(instance.startup())
    assert not calls and not instance.browser_attempted
    if reason == 'missing_frontend':
        assert '前端尚未构建' in caplog.text


@pytest.mark.parametrize('reason', ['closing', 'failure'])
def test_shutdown_before_thread_dispatch(console, monkeypatch, reason):
    instance, calls = console
    pending = []
    immediate_threads(monkeypatch, pending)
    asyncio.run(instance.startup())
    assert len(pending) == 1
    if reason == 'closing':
        instance.runtime.request_close()
    else:
        instance.runtime.failure = RuntimeError('失败')
    pending[0]()
    assert calls == []


@pytest.mark.parametrize('failure', ['thread', 'false', 'exception'])
def test_browser_failure_does_not_stop_service(console, monkeypatch, caplog, failure):
    instance, calls = console
    immediate_threads(monkeypatch, fail=failure == 'thread')

    def failed(url):
        calls.append(url)
        if failure == 'exception':
            raise OSError('打开失败')
        return False
    monkeypatch.setattr(server.webbrowser, 'open', failed)
    asyncio.run(instance.startup())
    assert instance.is_running() and instance.browser_attempted
    assert '请手动访问 http://127.0.0.1:8000/segments' in caplog.text
    assert len(calls) == (0 if failure == 'thread' else 1)


def test_parent_startup_failure_does_not_open(console, monkeypatch):
    instance, calls = console
    async def failed(self, sockets=None):
        raise OSError('HTTP listener 创建失败')
    monkeypatch.setattr(uvicorn.Server, 'startup', failed)
    with pytest.raises(OSError):
        asyncio.run(instance.startup())
    assert not calls and not instance.browser_attempted


@pytest.mark.parametrize('enabled', [False, True])
def test_cli_browser_defaults_and_forwarding(tmp_path, monkeypatch, enabled):
    monkeypatch.chdir(tmp_path)
    configs = []
    monkeypatch.setattr(server, 'run_server', lambda config: configs.append(config) or 0)
    argv = ['run', '--output-dir', '录像 目录'] + (['--open-browser', '--static-root', '候选网页'] if enabled else [])
    assert cli.main(argv) == 0
    config = configs[0]
    assert config.open_browser is enabled
    assert config.data_dir == cli.DEFAULT_DATA_DIR.resolve()
    assert config.output_dir == (tmp_path / '录像 目录').resolve()
    assert config.static_root == (tmp_path / '候选网页' if enabled else server.REPO_ROOT / 'web' / 'dist')
    assert (config.host, config.port) == ('127.0.0.1', 8000)
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(['run'])
    assert exc.value.code == 2

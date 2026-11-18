"""统一服务：预留监听资源后启动调度线程，lifespan 负责等待平滑收尾。"""
import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
import ipaddress
import logging
from pathlib import Path
import signal
import socket
import threading
import time
import webbrowser

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
import uvicorn

from src.orchestrator.api import install_api
from src.orchestrator.scheduler import Scheduler
from src.orchestrator.storage import ControlError, Store

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = REPO_ROOT.parent / "mumu_lbcut_tool_dev" / "runtime-console"
SERVICE_VERSION = "0.2.0"
logger = logging.getLogger(__name__)


@dataclass
class ServerConfig:
    data_dir: Path
    output_dir: Path
    instances: list[int] | None = None
    host: str = "127.0.0.1"
    port: int = 8000
    scan_interval: float = 30.0
    segment_duration: int = 1800
    navigation_timeout: float = 300.0
    max_runtime: float = 0.0
    static_root: Path = REPO_ROOT / "web" / "dist"
    open_browser: bool = False
    core_profile: Path | None = None

    def validate(self):
        from src.worker.room_locator import validate_navigation_timeout
        import math
        validate_navigation_timeout(self.navigation_timeout)
        if (self.instances is not None and (not self.instances or any(i < 0 for i in self.instances))) or not 1 <= self.port <= 65535:
            raise ValueError("实例编号或端口非法")
        if not all(math.isfinite(v) for v in (self.scan_interval, self.navigation_timeout, self.max_runtime)) or self.scan_interval <= 0 or self.segment_duration <= 0 or self.max_runtime < 0:
            raise ValueError("时间参数非法")
        address = ipaddress.ip_address(self.host)
        private = address.is_private or (address.version == 4 and address in ipaddress.ip_network("100.64.0.0/10"))
        if str(address) != "0.0.0.0" and (address.is_unspecified or address.is_multicast or not private):
            raise ValueError("请绑定本机、组网地址或 0.0.0.0")


class Runtime:
    def __init__(self, config, scheduler_factory=Scheduler):
        self.config, self.scheduler_factory = config, scheduler_factory
        self.store = self.scheduler = None
        self.state, self.last_tick_at, self.failure = "closing", None, None
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.thread = None
        self.on_exit = lambda: None

    def start(self):
        self.config.validate()
        self.thread = threading.Thread(target=self._run, name="scheduler-main")
        self.thread.start()
        self.ready.wait()
        if self.failure:
            self.thread.join()
            raise RuntimeError("调度初始化失败") from self.failure

    def _run(self):
        try:
            self.store = Store(self.config.data_dir / "orchestrator.sqlite3")
            self.scheduler = self.scheduler_factory(self.store, self.config.output_dir, self.config.instances,
                scan_interval_sec=self.config.scan_interval, segment_duration_sec=self.config.segment_duration,
                navigation_timeout_sec=self.config.navigation_timeout)
            with self.lock:
                if not self.stop.is_set():
                    self.state = "running"
            self.ready.set()
            started = time.monotonic()
            while not self.stop.is_set():
                self.scheduler.tick()
                self.last_tick_at = time.time()
                if self.config.max_runtime and time.monotonic() - started >= self.config.max_runtime:
                    break
                self.stop.wait(.1)
        except BaseException as exc:
            self.failure = exc
            logger.exception("调度线程失败，服务将退出")
        finally:
            self.request_close()
            self.ready.set()
            self.on_exit()
            try:
                if self.scheduler:
                    self.scheduler.close()
            except BaseException as exc:
                self.failure = self.failure or exc
                logger.exception("调度收尾失败")
            with self.lock:
                self.state = "failed" if self.failure else "closing"

    def request_close(self):
        with self.lock:
            self.state = "failed" if self.failure else "closing"
            self.stop.set()
            if self.scheduler:
                self.scheduler._shutdown = True

    def close(self):
        self.request_close()
        if self.thread:
            self.thread.join()  # 调度器自带媒体收尾预算，不能提前切断它。
        if self.store:
            self.store.close()
            self.store = None

    def submit(self, kind, subject, payload, request_id):
        with self.lock:
            if self.state != "running" or not self.thread or not self.thread.is_alive():
                raise ControlError("service_unavailable", "服务未就绪或正在关闭", 503)
            return self.store.enqueue_command(kind, subject, payload=payload, request_id=request_id)

    def system(self):
        return dict(scheduler_running=self.state == "running" and bool(self.thread and self.thread.is_alive()),
                    service_state=self.state, last_tick_at=self.last_tick_at, server_time=time.time(),
                    upload_enabled=False, service_version=SERVICE_VERSION, api_version=1,
                    statistics_timezone="Asia/Shanghai", frontend_available=(self.config.static_root / "index.html").is_file())


def create_app(config, runtime=None):
    runtime = runtime or Runtime(config)
    if not hasattr(runtime, "config"):
        runtime.config = config

    @asynccontextmanager
    async def lifespan(app):
        try:
            await asyncio.to_thread(runtime.start)
            yield
        finally:
            await asyncio.to_thread(runtime.close)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = runtime
    install_api(app, runtime)
    from src.orchestrator.bilibili_api import install_accounts
    install_accounts(app, config)

    @app.get("/{path:path}")
    def static(path: str):
        if path == "api" or path.startswith("api/"):
            raise ControlError("not_found", "接口不存在", 404)
        root = config.static_root.resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise ControlError("not_found", "资源不存在", 404)
        index = (root / "index.html").resolve()
        if not index.is_relative_to(root):
            raise ControlError("not_found", "资源不存在", 404)
        if not index.is_file():
            return JSONResponse(dict(code="frontend_unavailable", message="前端尚未构建", details=None), status_code=503)
        if target.is_file():
            return FileResponse(target)
        if Path(path).suffix:
            raise ControlError("not_found", "静态资源不存在", 404)
        return FileResponse(index)

    return app


def browser_url(config):
    host = "127.0.0.1" if config.host == "0.0.0.0" else config.host
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{config.port}/segments"


class ConsoleServer(uvicorn.Server):
    def __init__(self, config, runtime):
        super().__init__(config)
        self.runtime = runtime
        self.browser_attempted = False

    def is_running(self):
        runtime = self.runtime
        with runtime.lock:
            return (self.started and not self.should_exit and not runtime.stop.is_set()
                    and runtime.failure is None and runtime.state == "running"
                    and bool(runtime.thread and runtime.thread.is_alive()))

    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        if not self.is_running():
            return
        config = self.runtime.config
        url = browser_url(config)
        if not (config.static_root / "index.html").is_file():
            logger.warning("服务已就绪，但前端尚未构建；构建后可访问 %s", url)
            return
        logger.info("控制台已就绪：%s", url)
        if config.open_browser and not self.browser_attempted:
            self.browser_attempted = True
            try:
                # 系统打开请求可能阻塞；不能占用 HTTP 循环或拖住服务退出。
                threading.Thread(target=self.open_browser, args=(url,),
                                 name="console-browser", daemon=True).start()
            except Exception:
                logger.warning("无法启动浏览器打开线程，请手动访问 %s", url, exc_info=True)

    def open_browser(self, url):
        if not self.is_running():
            return
        try:
            if not webbrowser.open(url):
                logger.warning("默认浏览器未接受打开请求，请手动访问 %s", url)
        except Exception:
            logger.warning("打开默认浏览器失败，请手动访问 %s", url, exc_info=True)

    @contextmanager
    def capture_signals(self):
        old = {}

        def stop(signum, frame):
            self.runtime.request_close()
            self.should_exit = True

        try:
            for sig in (signal.SIGINT, signal.SIGTERM, *([signal.SIGBREAK] if hasattr(signal, "SIGBREAK") else [])):
                old[sig] = signal.signal(sig, stop)
            yield
        finally:
            for sig, handler in old.items():
                signal.signal(sig, handler)


def run_server(config, scheduler_factory=Scheduler):
    config.validate()
    sock = socket.socket(socket.AF_INET6 if ":" in config.host else socket.AF_INET, socket.SOCK_STREAM)
    runtime = Runtime(config, scheduler_factory)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((config.host, config.port))
        sock.listen(128)
        app = create_app(config, runtime)

        server = ConsoleServer(uvicorn.Config(app, host=config.host, port=config.port, workers=1,
            lifespan="on", timeout_graceful_shutdown=80, access_log=False, proxy_headers=False), runtime)
        runtime.on_exit = lambda: setattr(server, "should_exit", True)
        server.run(sockets=[sock])
        return 1 if runtime.failure or not server.started else 0
    finally:
        sock.close()

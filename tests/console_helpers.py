"""控制台测试的网络边界与 HTTP 宿主；不访问模拟器或外网。"""
from concurrent.futures import Future
from contextlib import contextmanager
import socket
import threading
import time
from types import SimpleNamespace

from src.orchestrator.room_query_process import QueryResult, CAPACITY


class ImmediateQueries:
    fatal_error = None

    def __init__(self, resolver):
        self.resolver = resolver
        self.calls = []

    def available(self, source):
        return True

    def submit(self, source, raw):
        future = Future()
        self.calls.append((source, raw, future))
        future.set_result(QueryResult(self.resolver.resolve(raw)))
        return future

    def close(self):
        pass


class DeferredQueries(ImmediateQueries):
    def __init__(self):
        self.calls = []

    def available(self, source):
        return sum(s == source and not f.done() for s, r, f in self.calls) < CAPACITY[source]

    def submit(self, source, raw):
        future = Future()
        self.calls.append((source, raw, future))
        return future


def observe_room(store, target_id, room, now, ended_interval=1):
    meta = store.begin_observation(target_id, now)
    return store.apply_observation(meta, room, ended_interval)


class TestRuntime:
    __test__ = False
    def __init__(self, store):
        self.store = store
        self.state = "running"

    def submit(self, kind, subject, payload, key):
        from src.orchestrator.storage import ControlError
        if self.state != "running":
            raise ControlError("service_unavailable", "服务正在关闭", 503)
        return self.store.enqueue_command(kind, subject, payload=payload, request_id=key)

    def system(self):
        return dict(scheduler_running=True, service_state=self.state)

    def start(self):
        pass

    def close(self):
        pass


@contextmanager
def live_http(store, root):
    import uvicorn
    from src.orchestrator.server import create_app, ServerConfig
    app = create_app(ServerConfig(store.path.parent, root, [1001]), TestRuntime(store))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]})
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started
        yield "http://127.0.0.1:" + str(sock.getsockname()[1])
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()
        assert not thread.is_alive()

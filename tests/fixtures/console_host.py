"""隔离宿主夹具：真实 HTTP/调度/查询进程，设备边界固定为空闲。"""
import argparse
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.orchestrator.server import ServerConfig, run_server
from src.orchestrator.scheduler import Scheduler
from src.orchestrator.room_query_process import QueryProcesses

parser = argparse.ArgumentParser()
parser.add_argument('root', type=Path)
parser.add_argument('port', type=int)
parser.add_argument('--mode', default='normal')
parser.add_argument('--host', default='127.0.0.1')
parser.add_argument('--open-browser', action='store_true')
parser.add_argument('--browser', choices=('probe', 'block', 'false', 'error', 'real'), default='probe')
parser.add_argument('--static-root', type=Path)
args = parser.parse_args()
args.root.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.INFO)
sentinel = None


def browser_probe(url):
    import requests
    session = requests.Session()
    session.trust_env = False
    system = session.get(url.removesuffix('/segments') + '/api/system', timeout=5)
    page = session.get(url, timeout=5)
    with (args.root / 'browser.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(dict(url=url, system=system.json(), page_status=page.status_code,
                                     page_body=page.text), ensure_ascii=False) + '\n')
    if args.browser == 'block':
        import threading
        threading.Event().wait()  # 模拟系统调用不返回，服务仍应能退出。
    if args.browser == 'error':
        raise OSError('合成浏览器失败')
    return args.browser != 'false'


if args.browser != 'real':
    from src.orchestrator import server
    server.webbrowser.open = browser_probe


class Pool:
    def refresh(self):
        if args.mode == 'init_wait':
            (args.root / 'init-entered').touch()
            deadline = time.monotonic() + 30
            while not (args.root / 'init-release').exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError('测试未释放初始化等待')
                time.sleep(.02)
        if args.mode == 'init_failure':
            raise RuntimeError('合成初始化失败')
        return []


class Host(Scheduler):
    def __init__(self, *a, **kw):
        self.count = 0
        queries = QueryProcesses(child_command=[sys.executable, str(Path(__file__).with_name('console_query_child.py'))])
        super().__init__(*a, **kw, pool=Pool(), query_executor=queries, auto_scan=False)
        if args.mode == 'blocked':
            for source, count in (('scan', 5), ('verify', 1), ('manual', 2), ('login', 1)):
                for _ in range(count):
                    queries.submit(source, 'block_dns')

    def tick(self):
        super().tick()
        self.count += 1
        if args.mode == 'fatal' and self.count > 10:
            raise RuntimeError('合成调度异常')
        pids = [s.proc.pid for s in self.room_queries.slots if s.proc]
        (args.root / 'pids.json').write_text(json.dumps(pids), encoding='utf-8')


if args.mode == 'blocked':
    sentinel = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    import psutil
    (args.root / 'worker.json').write_text(json.dumps(dict(pid=sentinel.pid, born=psutil.Process(sentinel.pid).create_time())), encoding='utf-8')
try:
    raise SystemExit(run_server(ServerConfig(args.root / 'state', args.root / 'out', [1001],
        host=args.host, port=args.port, open_browser=args.open_browser,
        static_root=args.static_root or args.root / 'web'), Host))
finally:
    if sentinel and sentinel.poll() is None:
        sentinel.terminate()
        sentinel.wait(5)

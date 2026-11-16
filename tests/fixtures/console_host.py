"""隔离宿主夹具：真实 HTTP/调度/查询进程，设备边界固定为空闲。"""
import argparse
import json
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
args = parser.parse_args()
args.root.mkdir(parents=True, exist_ok=True)
sentinel = None


class Pool:
    def refresh(self):
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
    raise SystemExit(run_server(ServerConfig(args.root / 'state', args.root / 'out', [1001], host=args.host, port=args.port), Host))
finally:
    if sentinel and sentinel.poll() is None:
        sentinel.terminate()
        sentinel.wait(5)

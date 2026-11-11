"""真实 Windows 子进程/信号/双管道验收；仅录制进程使用测试夹具，不启动 MuMu。"""
from pathlib import Path
import json
import subprocess
import sys
import threading
import time

import pytest
from src.orchestrator.models import WorkerLaunchSpec
from src.orchestrator.worker_process import WorkerProcessHandle, EventStreamIncomplete

CHILD = r'''
import argparse, json, pathlib, shutil, signal, sys, time
p = argparse.ArgumentParser()
p.add_argument('--output-dir', required=True)
p.add_argument('--job-id', default='fixture')
p.add_argument('--media', required=True)
p.add_argument('--stress', action='store_true')
a, _ = p.parse_known_args()
stop = False
seq = 0
def handle(signum, frame):
    global stop
    stop = True
signal.signal(signal.SIGINT, handle)
if hasattr(signal, 'SIGBREAK'):
    signal.signal(signal.SIGBREAK, handle)
root = pathlib.Path(a.output_dir)
root.mkdir(parents=True, exist_ok=True)
output = root / 'fixture.mp4'
def emit(kind, **fields):
    global seq
    seq += 1
    print(json.dumps(dict(event=kind, event_id=str(seq), job_id=a.job_id, **fields)), flush=True)
emit('worker_started', pid=__import__('os').getpid())
emit('segment_started', segment_index=1, file_path=str(output))
if a.stress:
    for n in range(320):
        sys.stderr.write('L' * 65535 + '\n')
        emit('heartbeat', elapsed_sec=n)
    sys.stderr.flush()
limit = time.monotonic() + 12
while not stop and time.monotonic() < limit:
    time.sleep(.02)
shutil.copyfile(a.media, output)
emit('segment_finished', segment_index=1, file_path=str(output), duration_sec=1, file_size_mb=.01, is_final=True)
emit('worker_stopped', exit_code=0, reason='stop_requested')
'''


def install_child_boundary(monkeypatch, tmp_path, media, stress=False):
    script = tmp_path / 'owned_process_fixture.py'
    script.write_text(CHILD, encoding='utf-8')
    real_popen = subprocess.Popen
    def spawn(cmd, **kwargs):
        if len(cmd) > 3 and cmd[2:4] == ['-m', 'src.worker.cli']:
            cmd = [sys.executable, '-u', str(script), '--media', str(media)] + (['--stress'] if stress else []) + cmd[4:]
        return real_popen(cmd, **kwargs)
    monkeypatch.setattr('src.orchestrator.worker_process.subprocess.Popen', spawn)


def test_actual_child_signal_tail_and_high_volume_logs(tmp_path, monkeypatch, valid_media):
    install_child_boundary(monkeypatch, tmp_path, valid_media, stress=True)
    ready, got = threading.Event(), []
    def callback(event):
        if event['event'] == 'heartbeat':
            time.sleep(.001)  # 故意让业务回调慢于 stdout，排空线程不应跟着阻塞。
        got.append(event)
        if event['event'] == 'segment_started':
            ready.set()
    handle = WorkerProcessHandle(WorkerLaunchSpec(1001, '123', tmp_path / 'recording',
        job_id='owned-real-child', event_log_path=tmp_path / 'events.ndjson', log_path=tmp_path / 'events.log'),
        on_event=callback)
    handle.start()
    try:
        assert handle.command[handle.command.index('--log-file') + 1] == str(tmp_path / 'events.log')
        assert ready.wait(5)
        assert handle.stop(timeout_sec=8) == 0
        assert handle.events_complete
        assert not handle.forced_stop
        assert not handle.is_alive()
        assert len(handle.finished_segments) == 1
        assert Path(handle.finished_segments[0]).exists()
        assert got[-1]['event'] == 'worker_stopped'
        assert len([e for e in got if e['event'] == 'heartbeat']) == 320
        logged = [json.loads(line) for line in (tmp_path / 'events.ndjson').read_text().splitlines()]
        assert len(logged) == len(got)
    finally:
        if handle.is_alive():
            handle.stop(timeout_sec=3)


def test_callback_failure_is_reported_not_silently_successful(tmp_path, monkeypatch, valid_media):
    install_child_boundary(monkeypatch, tmp_path, valid_media)
    ready = threading.Event()
    def fail(event):
        ready.set()
        raise ValueError('injected event consumer failure')
    handle = WorkerProcessHandle(WorkerLaunchSpec(1002, '123', tmp_path / 'recording', job_id='bad-callback'), fail)
    handle.start()
    try:
        assert ready.wait(5)
        with pytest.raises(EventStreamIncomplete):
            handle.stop(timeout_sec=5)
        assert not handle.is_alive()
        assert any('event_callback_failed' in e for e in handle.event_errors)
    finally:
        if handle.is_alive():
            handle.proc.kill()
            handle.proc.wait(timeout=3)


def test_two_real_children_fifo_and_cli_stop(tmp_path, monkeypatch, valid_media):
    """真实两个 Windows 子进程 + SQLite + CLI 命令；设备/联网边界为固定观测。"""
    from types import SimpleNamespace
    from src.mvp.room_resolver import RoomStatus, ResolvedRoom
    from src.orchestrator.instance_pool import InstancePool
    from src.orchestrator.room_scanner import RoomScanner
    from src.orchestrator.scheduler import Scheduler
    from src.orchestrator.storage import Store
    install_child_boundary(monkeypatch, tmp_path, valid_media)
    class Resolver:
        def resolve(self, raw):
            return ResolvedRoom(raw, RoomStatus.LIVE, room_id='77000000000000000' + raw)
        def batch_resolve(self, targets, max_workers=8):
            return {raw: self.resolve(raw) for raw in targets}
    state_dir = tmp_path / 'state'
    store = Store(state_dir / 'orchestrator.sqlite3')
    for raw in ('11', '12', '13'):
        store.add_target(raw, allow_unconfirmed=True)
    scanner = RoomScanner(Resolver())
    pool = InstancePool(SimpleNamespace(list_available_instances=lambda: [
        {'vmindex': i, 'name': f'Synthetic VM {i}', 'android_started': True} for i in (1001, 1002)]))
    scheduler = Scheduler(store, tmp_path / 'recordings', [1001, 1002], pool=pool, scanner=scanner, auto_scan=False)
    def pump(predicate):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            scheduler.tick()
            if predicate():
                return
            time.sleep(.02)
        assert predicate(), store.rows('SELECT * FROM jobs')
    try:
        scheduler.apply_snapshot(scanner.scan(['11', '12', '13']))
        pump(lambda: len(store.rows("SELECT * FROM jobs WHERE status='running'")) == 2
             and len(store.rows('SELECT * FROM segments')) == 2)
        jobs = store.rows('SELECT * FROM jobs ORDER BY created_at,rowid')
        assert [job['status'] for job in jobs] == ['running', 'running', 'pending']
        import os
        cli = subprocess.run([sys.executable, '-m', 'src.orchestrator.cli', '--data-dir', str(state_dir),
                              'stop', jobs[0]['id']], cwd=str(Path(__file__).resolve().parents[1]),
                             env=dict(os.environ, PYTHONIOENCODING='utf-8'), capture_output=True,
                             text=True, encoding='utf-8', timeout=5)
        assert cli.returncode == 0, cli.stderr
        assert json.loads(cli.stdout)['status'] == 'queued'
        pump(lambda: store.row('SELECT status FROM jobs WHERE id=?', (jobs[0]['id'],))['status'] == 'finished'
             and store.row('SELECT status FROM jobs WHERE id=?', (jobs[2]['id'],))['status'] == 'running')
        assert store.row('SELECT collection_complete FROM jobs WHERE id=?', (jobs[0]['id'],))['collection_complete'] == 1
    finally:
        scheduler.close()
        segments = store.rows('SELECT * FROM segments')
        assert len(segments) == 3
        assert all(segment['status'] == 'finished' and Path(segment['file_path']).exists() for segment in segments)
        assert not store.rows("SELECT * FROM instances WHERE job_id IS NOT NULL")
        store.close()


@pytest.mark.parametrize('attempt', range(8))
def test_stop_immediately_after_popen_waits_for_signal_registration(tmp_path, monkeypatch, valid_media, attempt):
    install_child_boundary(monkeypatch, tmp_path, valid_media)
    handle = WorkerProcessHandle(WorkerLaunchSpec(1003, '123', tmp_path / 'recording',
                                                  job_id=f'immediate-stop-{attempt}'))
    handle.start()
    try:
        # 不等待首帧/回调，故意在 Python 子解释器刚创建的窗口请求停止。
        assert handle.stop(timeout_sec=5) == 0
        assert not handle.forced_stop
        assert handle.events_complete
        assert not handle.is_alive()
        assert handle.terminal_event['event'] == 'worker_stopped'
    finally:
        if handle.is_alive():
            handle.proc.kill()
            handle.proc.wait(timeout=3)

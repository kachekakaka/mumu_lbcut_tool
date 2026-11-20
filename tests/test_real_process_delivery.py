"""真实 Windows 子进程/信号/双管道验收；仅录制进程使用测试夹具，不启动 MuMu。"""
from pathlib import Path
import json
import os
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
    print(json.dumps(dict(event=kind, event_id=str(seq), job_id=a.job_id,
                          run_id='fixture-run', event_seq=seq, **fields)), flush=True)
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


RESTART_WORKER = r'''
import argparse, os, pathlib, shutil, signal, time
from src.worker.events import (WorkerEventEmitter, WorkerStartedEvent, HeartbeatEvent,
    SegmentStartedEvent, SegmentFinishedEvent, SegmentQualityWarningEvent, WorkerStoppedEvent)
p = argparse.ArgumentParser()
for flag in ('job-id', 'target', 'event-log', 'output-dir'):
    p.add_argument('--' + flag, required=True)
a, _ = p.parse_known_args()
gate = pathlib.Path(os.environ['LEDGER_TEST_GATE'])
root = pathlib.Path(a.output_dir)
root.mkdir(parents=True)
path = root / 'finished.mp4'
stop = False
def stopped(*_):
    global stop
    stop = True
signal.signal(signal.SIGINT, stopped)
if hasattr(signal, 'SIGBREAK'):
    signal.signal(signal.SIGBREAK, stopped)
emitter = WorkerEventEmitter(fallback_path=a.event_log, on_stream_lost=lambda: (root/'pipe-lost').touch())
emitter.set_context(job_id=a.job_id, run_id='run-' + a.job_id)
emitter.emit(WorkerStartedEvent())
emitter.emit(SegmentStartedEvent(segment_index=1, file_path=str(path)))
original = a.target.endswith('11')
while not (stop or original and gate.exists()):
    emitter.emit(HeartbeatEvent(elapsed_sec=1))
    time.sleep(.05)
if original:
    for n in range(1200):
        emitter.emit(HeartbeatEvent(elapsed_sec=n))
    emitter.emit(SegmentQualityWarningEvent(segment_index=1, flag='low_frame_rate'))
shutil.copyfile(os.environ['LEDGER_TEST_MEDIA'], path)
emitter.emit(SegmentFinishedEvent(segment_index=1, file_path=str(path), is_final=True))
emitter.emit(WorkerStoppedEvent(exit_code=0, reason='completed'))
emitter.close()
'''


RESTART_HOST = r'''
from pathlib import Path
from types import SimpleNamespace
import sys, threading, time
sys.path.insert(0, str(Path.cwd() / 'tests'))
from console_helpers import ImmediateQueries, observe_room
from src.mvp.room_resolver import ResolvedRoom, RoomStatus
from src.orchestrator.instance_pool import InstancePool
from src.orchestrator.scheduler import Scheduler
from src.orchestrator.storage import Store
from src.orchestrator import worker_process

shadow, state, output, marker = map(Path, sys.argv[1:])
worker_process.REPO_ROOT = shadow
store = Store(state)
pool = InstancePool(SimpleNamespace(list_available_instances=lambda: [
    dict(vmindex=i, android_started=True) for i in (1001, 1002)]))
resolver = SimpleNamespace(resolve=lambda raw: ResolvedRoom(
    raw, RoomStatus.LIVE, room_id='77000000000000000' + raw, anchor_uid='900001'))
scheduler = Scheduler(store, output, [1001, 1002], pool=pool,
    query_executor=ImmediateQueries(resolver), auto_scan=False)
def pause(job, handle, path, now):
    staging = marker.with_suffix('.tmp')
    staging.write_text(str(handle.proc.pid), encoding='ascii')
    staging.replace(marker)
    threading.Event().wait(60)
    raise RuntimeError('测试主控未按预期退出')
store.mark_launched = pause
target = store.add_target('11')
observe_room(store, target, resolver.resolve('11'), time.time())
while not marker.exists():
    scheduler.tick()
    time.sleep(.02)
'''


@pytest.mark.skipif(os.name != 'nt', reason='覆盖 Windows 真实 Worker 和停录信号')
def test_restart_holds_pidless_worker_through_replay_and_settlement(tmp_path, monkeypatch, valid_media):
    import sqlite3
    from types import SimpleNamespace

    import psutil
    from console_helpers import ImmediateQueries, observe_room
    from src.mvp.room_resolver import ResolvedRoom, RoomStatus
    from src.orchestrator import worker_process
    from src.orchestrator.instance_pool import InstancePool
    from src.orchestrator.scheduler import Scheduler, inspect_job_process
    from src.orchestrator.storage import Store

    shadow = tmp_path / 'shadow'
    worker = shadow / 'src' / 'worker'
    worker.mkdir(parents=True)
    original = Path(worker_process.__file__).resolve().parents[1] / 'worker'
    (worker / '__init__.py').write_text(f'__path__.append({str(original)!r})\n', encoding='utf-8')
    (worker / 'cli.py').write_text(RESTART_WORKER, encoding='utf-8')
    monkeypatch.setattr(worker_process, 'REPO_ROOT', shadow)
    state, output = tmp_path / 'state.sqlite3', tmp_path / 'output'
    marker, gate = tmp_path / 'pid', tmp_path / 'release'
    monkeypatch.setenv('LEDGER_TEST_GATE', str(gate))
    monkeypatch.setenv('LEDGER_TEST_MEDIA', str(valid_media))
    host = subprocess.Popen([sys.executable, '-u', '-c', RESTART_HOST,
                             str(shadow), str(state), str(output), str(marker)])
    owned = None
    scheduler = store = None

    def wait_for(condition, step=lambda: None, timeout=10):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            step()
            if value := condition():
                return value
            time.sleep(.025)
        pytest.fail('未达到录播交接同步点')

    def snapshot(sql, *params):
        with sqlite3.connect(state) as db:
            db.row_factory = sqlite3.Row
            return dict(row) if (row := db.execute(sql, params).fetchone()) else None

    try:
        pid = wait_for(lambda: int(marker.read_text()) if marker.exists() and marker.stat().st_size else 0)
        owned = psutil.Process(pid)
        old = snapshot('SELECT * FROM jobs')
        assert old['pid'] is None and old['event_log_path']
        assert inspect_job_process(old) == 'alive'
        host.kill()
        host.wait(10)
        wait_for(lambda: (output / old['id'] / 'pipe-lost').exists())
        assert owned.is_running()
        store = Store(state)
        slots = [1001, 1002]
        pool = InstancePool(SimpleNamespace(list_available_instances=lambda: [
            dict(vmindex=i, android_started=True) for i in slots]))
        resolver = SimpleNamespace(resolve=lambda raw: ResolvedRoom(
            raw, RoomStatus.LIVE, room_id='77000000000000000' + raw, anchor_uid='900001'))
        probes = []

        def probe(job):
            status = inspect_job_process(job)
            probes.append((job['id'], status))
            return status

        def start():
            return Scheduler(store, output, slots, pool=pool, query_executor=ImmediateQueries(resolver),
                             process_probe=probe, auto_scan=False)

        scheduler = start()
        for raw in ('13', '12'):  # C 先占用另一实例；B 已就绪，只能等待 A。
            target = store.add_target(raw)
            observe_room(store, target, resolver.resolve(raw), time.time())
            if raw == '13':
                other = wait_for(lambda: snapshot("SELECT * FROM jobs WHERE status='running'"), scheduler.tick)
        b = wait_for(lambda: snapshot("SELECT * FROM jobs WHERE status='pending'"), scheduler.tick)
        wait_for(lambda: probes.count((old['id'], 'alive')) >= 3, scheduler.tick)
        assert snapshot("SELECT status,pid FROM jobs WHERE id=?", b['id']) == dict(status='pending', pid=None)
        assert snapshot("SELECT status,job_id FROM instances WHERE vmindex=?", old['vmindex']) == dict(
            status='busy', job_id=old['id'])
        assert snapshot("SELECT heartbeat_json FROM jobs WHERE id=?", other['id'])['heartbeat_json']
        scheduler.close()
        slots.remove(other['vmindex'])
        scheduler = start()
        assert snapshot("SELECT job_id FROM instances WHERE vmindex=?", old['vmindex'])['job_id'] == old['id']
        assert snapshot("SELECT pid FROM jobs WHERE id=?", b['id'])['pid'] is None

        gate.touch()
        owned.wait(10)
        path = Path(old['event_log_path'])
        original_log = path.read_bytes()
        first, rest = original_log.split(b'\n', 1)
        path.write_bytes(first + b'\n{"bad":"\xe4\xb8\n' + rest + first + b'\n')
        wait_for(lambda: scheduler._replays.get(old['id'], 0) > 0, scheduler.tick)
        assert snapshot("SELECT job_id FROM instances WHERE vmindex=?", old['vmindex'])['job_id'] == old['id']
        assert snapshot("SELECT pid FROM jobs WHERE id=?", b['id'])['pid'] is None
        scheduler.close()
        scheduler = start()

        finish = store.finish_worker
        seen = []

        def pause_finish(job, *args, **kwargs):
            if job == old['id']:
                assert snapshot("SELECT job_id FROM instances WHERE vmindex=?", old['vmindex'])['job_id'] == job
                assert snapshot("SELECT pid FROM jobs WHERE id=?", b['id'])['pid'] is None
                assert snapshot("SELECT COUNT(*) AS n FROM events WHERE job_id=? AND kind='worker_stopped'", job)['n'] == 1
                seen.append(job)
            return finish(job, *args, **kwargs)

        store.finish_worker = pause_finish
        wait_for(lambda: snapshot("SELECT * FROM jobs WHERE id=? AND status='running'", b['id']), scheduler.tick)
        result = snapshot('SELECT status,exit_code,error_reason,collection_complete FROM jobs WHERE id=?', old['id'])
        assert result == dict(status='finished_with_warnings', exit_code=0, error_reason='completed', collection_complete=1)
        assert seen == [old['id']]
        clip = snapshot('SELECT * FROM segments WHERE job_id=?', old['id'])
        assert clip['is_final'] == 1 and Path(clip['file_path']).read_bytes() == valid_media.read_bytes()
        assert json.loads(clip['risk_json'])[0]['flag'] == 'low_frame_rate'
        scheduler.reconcile(old['id'])
        assert snapshot("SELECT job_id FROM instances WHERE vmindex=?", old['vmindex'])['job_id'] == b['id']
    finally:
        gate.touch(exist_ok=True)
        if host.poll() is None:
            host.kill()
            host.wait(10)
        if owned is not None and owned.is_running():
            owned.wait(10)
        if scheduler is not None:
            scheduler.close()
        if store is not None:
            store.close()


@pytest.mark.parametrize('failure', ['brief_lock', 'busy', 'commit_io', 'commit_io_missing_stop'])
def test_runtime_owns_shutdown_and_replays_after_write_failure(tmp_path, monkeypatch, valid_media, failure):
    import sqlite3
    from types import SimpleNamespace
    import psutil
    from console_helpers import ImmediateQueries, observe_room
    from src.mvp.room_resolver import ResolvedRoom, RoomStatus
    from src.orchestrator.instance_pool import InstancePool
    from src.orchestrator.scheduler import Scheduler
    from src.orchestrator.server import Runtime, ServerConfig

    install_child_boundary(monkeypatch, tmp_path, valid_media)
    data = tmp_path / 'state'
    path = data / 'orchestrator.sqlite3'
    output = tmp_path / 'recordings'
    output.mkdir()
    config = ServerConfig(data_dir=data, output_dir=output, instances=[1001, 1002])
    resolver = SimpleNamespace(resolve=lambda raw: ResolvedRoom(
        raw, RoomStatus.LIVE, room_id='77000000000000000' + raw, anchor_uid='900001'))
    pool = InstancePool(SimpleNamespace(list_available_instances=lambda: [
        dict(vmindex=i, android_started=True) for i in (1001, 1002)]))
    def factory(store, output_dir, instances, **kwargs):
        return Scheduler(store, output_dir, instances, pool=pool, query_executor=ImmediateQueries(resolver),
                         auto_scan=False, **kwargs)

    armed = threading.Event()
    real_connect = sqlite3.connect
    class FaultyCommit(sqlite3.Connection):
        def commit(self):
            if armed.is_set() and self.execute(
                "SELECT 1 FROM events WHERE kind='segment_finished'").fetchone():
                raise sqlite3.OperationalError('disk I/O error (测试注入)')
            return super().commit()

    if failure.startswith('commit_io'):
        def connect(database, *args, **kwargs):
            if Path(database) == path:
                kwargs['factory'] = FaultyCommit
            return real_connect(database, *args, **kwargs)
        monkeypatch.setattr(sqlite3, 'connect', connect)

    def until(predicate, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.02)
        pytest.fail('服务未到达写入故障同步点')

    runtime = Runtime(config, factory)
    lock = None
    try:
        runtime.start()
        for raw in ('21', '22'):
            target = runtime.store.add_target(raw)
            observe_room(runtime.store, target, resolver.resolve(raw), time.time())
        until(lambda: len(runtime.store.rows("SELECT id FROM jobs WHERE status='running'")) == 2)
        handles = list(runtime.scheduler._handles.values())
        pids = [h.proc.pid for h in handles]
        if failure.startswith('commit_io'):
            armed.set()
        else:
            lock = real_connect(path)
            lock.execute('BEGIN IMMEDIATE')
        handles[0].request_stop()
        until(lambda: any('segment_finished' in p.read_text(encoding='utf-8')
                          for p in (data / 'events').glob('*.ndjson')))
        if failure == 'brief_lock':
            assert runtime.failure is None and handles[1].is_alive()
            lock.rollback()
            handles[1].request_stop()
            until(lambda: len(runtime.store.rows("SELECT id FROM jobs WHERE status='finished'")) == 2)
            assert runtime.failure is None
            assert len(runtime.store.rows("SELECT id FROM events WHERE kind='segment_finished'")) == 2
            return
        until(lambda: runtime.failure is not None)
        assert isinstance(runtime.failure, sqlite3.OperationalError)
        assert 'locked' in str(runtime.failure) if failure == 'busy' else 'disk I/O' in str(runtime.failure)
        with real_connect(path) as reader:
            assert reader.execute("SELECT COUNT(*) FROM instances WHERE job_id IS NOT NULL").fetchone()[0] == 2
            assert reader.execute("SELECT COUNT(*) FROM events WHERE kind='segment_finished'").fetchone()[0] == 0
        if lock:
            lock.rollback()
        runtime.close()
        assert all(not psutil.pid_exists(pid) for pid in pids)
        assert len(list((data / 'events').glob('*.ndjson'))) == 2
    finally:
        if lock:
            lock.close()
        runtime.close()
        armed.clear()
    missing_stop = failure == 'commit_io_missing_stop'
    if missing_stop:
        journal = next((data / 'events').glob('*.ndjson'))
        lines = journal.read_bytes().splitlines(keepends=True)
        assert any(b'worker_stopped' in line for line in lines)
        journal.write_bytes(b''.join(line for line in lines if b'worker_stopped' not in line))
    recovered = Runtime(config, factory)
    try:
        recovered.start()
        until(lambda: len(recovered.store.rows("SELECT id FROM jobs WHERE ended_at IS NOT NULL")) == 2)
        assert len(recovered.store.rows("SELECT id FROM events WHERE kind='segment_finished'")) == 2
        results = recovered.store.rows('SELECT status,collection_complete,error_reason FROM jobs')
        assert sum(row['collection_complete'] for row in results) == (1 if missing_stop else 2)
        if missing_stop:
            invalid, = [row for row in results if not row['collection_complete']]
            assert invalid['status'] == 'failed' and 'worker_stopped' in invalid['error_reason']
    finally:
        recovered.close()


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
    from src.orchestrator.scheduler import Scheduler
    from src.orchestrator.storage import Store
    install_child_boundary(monkeypatch, tmp_path, valid_media)
    class Resolver:
        def resolve(self, raw):
            return ResolvedRoom(raw, RoomStatus.LIVE, room_id='77000000000000000' + raw, anchor_uid='900001')
    state_dir = tmp_path / 'state'
    store = Store(state_dir / 'orchestrator.sqlite3')
    for raw in ('11', '12', '13'):
        store.add_target(raw, allow_unconfirmed=True)
    pool = InstancePool(SimpleNamespace(list_available_instances=lambda: [
        {'vmindex': i, 'name': f'Synthetic VM {i}', 'android_started': True} for i in (1001, 1002)]))
    from console_helpers import ImmediateQueries, observe_room, live_http
    scheduler = Scheduler(store, tmp_path / 'recordings', [1001, 1002], pool=pool, query_executor=ImmediateQueries(Resolver()), auto_scan=False)
    def pump(predicate):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            scheduler.tick()
            if predicate():
                return
            time.sleep(.02)
        assert predicate(), store.rows('SELECT * FROM jobs')
    try:
        for target in store.rows('SELECT * FROM targets'):
            observe_room(store, target['id'], Resolver().resolve(target['raw']), time.time())
        pump(lambda: len(store.rows("SELECT * FROM jobs WHERE status='running'")) == 2
             and len(store.rows('SELECT * FROM segments')) == 2)
        jobs = store.rows('SELECT * FROM jobs ORDER BY created_at,rowid')
        assert [job['status'] for job in jobs] == ['running', 'running', 'pending']
        import os
        with live_http(store, tmp_path) as url:
            cli = subprocess.run([sys.executable, '-m', 'src.orchestrator.cli', 'stop', jobs[0]['id'], '--server', url],
                                 cwd=str(Path(__file__).resolve().parents[1]), env=dict(os.environ, PYTHONIOENCODING='utf-8'),
                                 capture_output=True, text=True, encoding='utf-8', timeout=10)
        assert cli.returncode == 0, cli.stderr
        assert json.loads(cli.stdout)['status'] == 'pending'
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

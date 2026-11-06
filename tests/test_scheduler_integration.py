"""真实 SQLite + Scanner + InstancePool + Scheduler 集成；仅替换网络和 Worker 进程边界。"""
from pathlib import Path
from types import SimpleNamespace
import time

import pytest
from src.mvp.room_resolver import RoomStatus, ResolvedRoom
from src.orchestrator.instance_pool import InstancePool
from src.orchestrator.room_scanner import RoomScanner
from src.orchestrator.scheduler import Scheduler
from src.orchestrator.storage import Store


class Clock:
    def __init__(self):
        self.value = time.time()
    def __call__(self):
        return self.value
    def advance(self, seconds=1):
        self.value += seconds


class Network:
    def __init__(self):
        self.rooms = {}
        self.resolved = []
    def resolve(self, raw):
        self.resolved.append(raw)
        return self.rooms.get(raw, ResolvedRoom(raw, RoomStatus.UNKNOWN))
    def batch_resolve(self, targets, max_workers=8):
        return {t: self.resolve(t) for t in targets}


class Child:
    """外部子进程替身；事件回调仍经过真实 Scheduler 的入队、事务和收尾。"""
    serial = 100000
    def __init__(self, spec, on_event):
        Child.serial += 1
        self.spec, self.on_event = spec, on_event
        self.proc = SimpleNamespace(pid=Child.serial)
        self.created_at = time.time()
        self.command = ['python', '-m', 'src.worker.cli', '--job-id', spec.job_id]
        self.event_errors = []
        self.events_complete = False
        self.forced_stop = False
        self.alive = False
        self.terminal_event = None
        self.seq = 0
    def emit(self, kind, **kw):
        self.seq += 1
        self.on_event(dict(event=kind, event_id=f'{self.spec.job_id}:{self.seq}', job_id=self.spec.job_id, **kw))
    def start(self):
        self.alive = True
        self.file = self.spec.output_dir / 'segment.mp4'
        self.file.parent.mkdir(parents=True)
        self.emit('worker_started', pid=self.proc.pid)
        self.emit('segment_started', segment_index=1, file_path=str(self.file))
    def is_alive(self):
        return self.alive
    def poll_exit_code(self):
        return None if self.alive else 0
    def stop(self, timeout_sec=30):
        self.file.write_bytes(b'synthetic-process-boundary-artifact')
        self.emit('segment_finished', segment_index=1, file_path=str(self.file), duration_sec=1,
                  file_size_mb=.01, is_final=True)
        self.terminal_event = dict(event='worker_stopped', reason='stop_requested')
        self.emit('worker_stopped', reason='stop_requested', exit_code=0)
        self.alive = False
        self.events_complete = True
        return 0
    def wait(self, timeout=None):
        return self.poll_exit_code()


@pytest.fixture
def system(tmp_path):
    clock, network = Clock(), Network()
    store = Store(tmp_path / 'state' / 'orchestrator.sqlite3')
    mumu = SimpleNamespace(list_available_instances=lambda: [dict(vmindex=i, name=f'VM{i}') for i in (1, 2)])
    pool = InstancePool(mumu)
    children = []
    def factory(**kw):
        c = Child(**kw)
        children.append(c)
        return c
    scheduler = Scheduler(store, tmp_path / 'recordings', [1, 2], pool=pool,
        scanner=RoomScanner(network), handle_factory=factory, clock=clock,
        ended_interval_sec=1, auto_scan=False, process_probe=lambda job: 'absent')
    env = SimpleNamespace(clock=clock, network=network, store=store, scheduler=scheduler,
                          children=children, tmp=tmp_path, pool=pool, factory=factory)
    yield env
    scheduler.close()
    store.close()


def observe(env, values):
    env.clock.advance(2)
    for raw, status in values.items():
        env.network.rooms[raw] = ResolvedRoom(raw, status, room_id='room-' + raw)
    snap = env.scheduler.scanner.scan(list(env.network.rooms))
    snap.timestamp = env.clock()
    env.scheduler.apply_snapshot(snap)


def pump(env, predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        env.scheduler.tick()
        if predicate():
            return
        time.sleep(.005)
    assert predicate(), env.store.rows('SELECT * FROM jobs')


def test_two_instances_fifo_and_pending_ended(system):
    e = system
    ids = [e.store.add_target(raw, now=e.clock()) for raw in ('A', 'B', 'C')]
    observe(e, dict.fromkeys(('A', 'B', 'C'), RoomStatus.LIVE))
    pump(e, lambda: len(e.children) == 2)
    jobs = e.store.rows('SELECT * FROM jobs ORDER BY created_at,rowid')
    assert [j['status'] for j in jobs] == ['running', 'running', 'pending']
    assert len({j['vmindex'] for j in jobs[:2]}) == 2
    # 等待目标 C 已下播，不应在稍后槽位释放时误启动。
    observe(e, {'C': RoomStatus.ENDED})
    observe(e, {'C': RoomStatus.ENDED})
    pump(e, lambda: e.store.row('SELECT status FROM jobs WHERE target_id=?', (ids[2],))['status'] == 'cancelled')
    assert len(e.children) == 2
    # D 排队，A 下播腾出实例后才启动 D。
    e.store.add_target('D', now=e.clock())
    observe(e, {'D': RoomStatus.LIVE})
    e.scheduler.tick()
    assert len(e.children) == 2
    observe(e, {'A': RoomStatus.ENDED})
    observe(e, {'A': RoomStatus.ENDED})
    pump(e, lambda: len(e.children) == 3)
    assert e.children[-1].spec.web_rid == 'D'


def test_unknown_roundtrip_does_not_duplicate_or_lose_stop(system):
    e = system
    key = e.store.add_target('A', now=e.clock())
    observe(e, {'A': RoomStatus.LIVE})
    pump(e, lambda: len(e.children) == 1)
    for status in (RoomStatus.UNKNOWN, RoomStatus.LIVE, RoomStatus.UNKNOWN):
        observe(e, {'A': status})
        e.scheduler.tick()
    assert len(e.store.rows('SELECT * FROM jobs')) == 1
    assert e.children[0].alive
    observe(e, {'A': RoomStatus.ENDED})
    observe(e, {'A': RoomStatus.ENDED})
    pump(e, lambda: e.store.row('SELECT status FROM jobs WHERE target_id=?', (key,))['status'] == 'finished')
    assert not e.children[0].alive
    assert e.store.rows('SELECT * FROM segments')[0]['status'] == 'finished'


def test_manual_stop_suppresses_same_session_and_event_idempotence(system):
    e = system
    target = e.store.add_target('A', now=e.clock())
    observe(e, {'A': RoomStatus.LIVE})
    pump(e, lambda: len(e.children) == 1)
    job = e.store.rows('SELECT * FROM jobs')[0]
    event = dict(event='heartbeat', event_id='same-event', job_id=job['id'], status='recording')
    e.store.record_event(job['id'], event, e.clock())
    e.store.record_event(job['id'], event, e.clock())
    assert len(e.store.rows("SELECT * FROM events WHERE kind='heartbeat'")) == 1
    e.store.enqueue_command('stop', job['id'])
    pump(e, lambda: not e.children[0].alive and e.store.row('SELECT status FROM jobs WHERE id=?', (job['id'],))['status'] == 'finished')
    for _ in range(3):
        observe(e, {'A': RoomStatus.LIVE})
        e.scheduler.tick()
    assert len(e.children) == 1
    e.store.enqueue_command('record', target)
    pump(e, lambda: len(e.children) == 2)


def test_pending_launch_uses_fresh_room_not_old_scan(system):
    e = system
    e.store.add_target('A', now=e.clock())
    observe(e, {'A': RoomStatus.LIVE})
    e.network.rooms['A'] = ResolvedRoom('A', RoomStatus.ENDED)
    for _ in range(10):
        e.scheduler.tick()
        time.sleep(.005)
    assert not e.children


def test_start_failures_are_bounded(system):
    e = system
    class BrokenChild(Child):
        def start(self):
            raise OSError('injected spawn failure')
    calls = []
    def factory(**kw):
        calls.append(kw)
        return BrokenChild(**kw)
    e.scheduler.handle_factory = factory
    e.scheduler.max_attempts = 2
    e.store.add_target('A', now=e.clock())
    observe(e, {'A': RoomStatus.LIVE})
    pump(e, lambda: len(calls) == 1)
    e.clock.advance(60)
    observe(e, {'A': RoomStatus.LIVE})
    pump(e, lambda: len(calls) == 2)
    e.clock.advance(300)
    observe(e, {'A': RoomStatus.LIVE})
    for _ in range(5):
        e.scheduler.tick()
    assert len(calls) == 2


def test_reconcile_keeps_live_instance_reserved_and_never_kills(system):
    e = system
    target = e.store.add_target('A', now=e.clock())
    observe(e, {'A': RoomStatus.LIVE})
    pump(e, lambda: len(e.children) == 1)
    job = e.store.rows('SELECT * FROM jobs')[0]
    # 模拟主控内存丢失，不真正创建或终止 OS 进程。
    e.scheduler._handles.clear()
    e.scheduler.process_probe = lambda record: 'alive'
    e.scheduler.reconcile()
    assert e.store.row('SELECT status FROM jobs WHERE id=?', (job['id'],))['status'] == 'needs_reconcile'
    assert e.store.row('SELECT status FROM instances WHERE vmindex=?', (job['vmindex'],))['status'] == 'reconcile'
    for _ in range(3):
        e.scheduler.tick()
    assert len(e.children) == 1
    e.scheduler.process_probe = lambda record: 'absent'
    e.scheduler.reconcile(job['id'])
    assert e.store.row('SELECT status FROM jobs WHERE id=?', (job['id'],))['status'] == 'failed'
    assert e.store.row('SELECT suppressed_session FROM targets WHERE id=?', (target,))['suppressed_session'] == job['session_key']


def test_database_preserves_previous_records_on_reopen(tmp_path):
    path = tmp_path / 'state.sqlite3'
    store = Store(path)
    key = store.add_target('https://live.douyin.com/123')
    assert store.add_target('123') == key
    store.close()
    reopened = Store(path)
    assert reopened.rows('SELECT * FROM targets')[0]['id'] == key
    reopened.close()


def test_storage_failure_still_stops_owned_children(system, monkeypatch):
    e = system
    e.store.add_target('A', now=e.clock())
    observe(e, {'A': RoomStatus.LIVE})
    pump(e, lambda: len(e.children) == 1)
    def broken_store(*args, **kwargs):
        raise OSError('injected database write failure')
    monkeypatch.setattr(e.store, 'record_event', broken_store)
    e.children[0].emit('heartbeat', status='recording')
    with pytest.raises(OSError, match='database write failure'):
        e.scheduler.tick()
    with pytest.raises(RuntimeError, match='收尾存在异常'):
        e.scheduler.close()
    assert not e.children[0].alive
    assert e.store.rows('SELECT * FROM jobs')[0]['status'] != 'finished'

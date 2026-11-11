"""真实 SQLite + Scanner + InstancePool + Scheduler 集成；仅替换网络和 Worker 进程边界。"""
import json
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
    def __init__(self, spec, on_event, record=True, extra_args=()):
        Child.serial += 1
        self.spec, self.on_event = spec, on_event
        self.proc = SimpleNamespace(pid=Child.serial)
        self.created_at = time.time()
        self.command = ['python', '-m', 'src.worker.cli', '--job-id', spec.job_id, *extra_args]
        self.record = record
        self.code = 0
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
        if self.record:
            self.emit('segment_started', segment_index=1, file_path=str(self.file))
    def exit(self, code, reason='injected_exit'):
        """Worker 自行退出：封口已开始的切片后报告退出码。"""
        if self.record:
            self.file.write_bytes(b'synthetic-process-boundary-artifact')
            self.emit('segment_finished', segment_index=1, file_path=str(self.file), duration_sec=1,
                      file_size_mb=.01, is_final=True)
        self.terminal_event = dict(event='worker_stopped', reason=reason)
        self.emit('worker_stopped', reason=reason, exit_code=code)
        self.code = code
        self.alive = False
        self.events_complete = True
        return code
    def is_alive(self):
        return self.alive
    def poll_exit_code(self):
        return None if self.alive else self.code
    def stop(self, timeout_sec=30):
        return self.exit(0, 'stop_requested')
    def wait(self, timeout=None):
        return self.poll_exit_code()


@pytest.fixture
def system(tmp_path):
    clock, network = Clock(), Network()
    store = Store(tmp_path / 'state' / 'orchestrator.sqlite3')
    vms = {1: True, 2: True}
    mumu = SimpleNamespace(list_available_instances=lambda: [
        dict(vmindex=i, name=f'VM{i}', android_started=started) for i, started in sorted(vms.items())])
    pool = InstancePool(mumu)
    resets = []
    env = SimpleNamespace(clock=clock, network=network, store=store, children=[], tmp=tmp_path, pool=pool,
                          vms=vms, resets=resets, reset_ok=True, child_options={})
    def reset_app(vmindex):
        resets.append(vmindex)
        return env.reset_ok
    pool.reset_app = reset_app
    def factory(**kw):
        c = Child(**kw, **env.child_options)
        env.children.append(c)
        return c
    env.factory = factory
    env.scheduler = new_scheduler(env)
    yield env
    env.scheduler.close()
    store.close()


def new_scheduler(env):
    return Scheduler(env.store, env.tmp / 'recordings', [1, 2], pool=env.pool,
        scanner=RoomScanner(env.network), handle_factory=env.factory, clock=env.clock,
        ended_interval_sec=1, auto_scan=False, process_probe=lambda job: 'absent')

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


def row(env, sql, *params):
    return env.store.row(sql, params)


def start_one(env, raw='A'):
    target = env.store.add_target(raw, now=env.clock())
    observe(env, {raw: RoomStatus.LIVE})
    count = len(env.children)
    pump(env, lambda: len(env.children) == count + 1 and (not env.children[-1].record or
         row(env, 'SELECT status FROM jobs WHERE id=?', env.children[-1].spec.job_id)['status'] == 'running'))
    return target, row(env, 'SELECT * FROM jobs WHERE id=?', env.children[-1].spec.job_id)


def job_status(env, job_id):
    return row(env, 'SELECT status FROM jobs WHERE id=?', job_id)['status']


def test_start_failures_retry_with_doubling_backoff_without_limit(system):
    e = system
    class BrokenChild(Child):
        def start(self):
            raise OSError('injected spawn failure')
    calls = []
    def factory(**kw):
        calls.append(kw)
        return BrokenChild(**kw)
    e.scheduler.handle_factory = factory
    target = e.store.add_target('A', now=e.clock())
    observe(e, {'A': RoomStatus.LIVE})
    delays = []
    for n in range(1, 9):
        pump(e, lambda: len(calls) == n and job_status(e, calls[-1]['spec'].job_id) == 'failed')
        t = row(e, 'SELECT * FROM targets WHERE id=?', target)
        delays.append(round(t['retry_at'] - e.clock()))
        assert t['suppressed_session'] is None
        for _ in range(3):
            e.scheduler.tick()
        assert len(calls) == n
        e.clock.advance(delays[-1])
        observe(e, {'A': RoomStatus.LIVE})
    assert delays == [5, 10, 20, 40, 80, 160, 300, 300]
    # 进入 running 后失败计数清零，下一次故障从 5 秒重新开始。
    e.scheduler.handle_factory = e.factory
    pump(e, lambda: e.children and job_status(e, e.children[-1].spec.job_id) == 'running')
    assert row(e, 'SELECT attempts FROM targets WHERE id=?', target)['attempts'] == 0
    e.children[-1].exit(30)
    pump(e, lambda: job_status(e, e.children[-1].spec.job_id) == 'failed')
    assert row(e, 'SELECT retry_at FROM targets WHERE id=?', target)['retry_at'] == pytest.approx(e.clock() + 5)


@pytest.mark.parametrize('code', [10, 18, 30, 17, 2, 0])
def test_worker_failures_retry_same_session_after_backoff(system, code):
    e = system
    target, job = start_one(e)
    e.children[0].exit(code)
    pump(e, lambda: job_status(e, job['id']) in ('failed', 'finished'))
    t = row(e, 'SELECT * FROM targets WHERE id=?', target)
    assert t['suppressed_session'] is None and t['note'] is None
    assert t['retry_at'] == pytest.approx(e.clock() + 5)
    assert row(e, 'SELECT status FROM instances WHERE vmindex=?', job['vmindex'])['status'] == 'idle'
    for _ in range(3):
        e.scheduler.tick()
    assert len(e.children) == 1
    e.clock.advance(5)
    observe(e, {'A': RoomStatus.LIVE})
    pump(e, lambda: len(e.children) == 2)
    assert e.children[1].spec.job_id != job['id']


def test_total_duration_completion_suppresses_session(system):
    e = system
    e.child_options = dict(extra_args=('--total-duration', '60'))
    target, job = start_one(e)
    e.children[0].exit(0, 'total_duration_reached')
    pump(e, lambda: job_status(e, job['id']) == 'finished')
    assert row(e, 'SELECT suppressed_session FROM targets WHERE id=?', target)['suppressed_session'] == job['session_key']
    e.clock.advance(400)
    for _ in range(3):
        observe(e, {'A': RoomStatus.LIVE})
        e.scheduler.tick()
    assert len(e.children) == 1


def test_invalid_target_needs_fix_until_record(system):
    e = system
    e.child_options = dict(record=False)
    target, job = start_one(e)
    e.children[0].exit(9, 'target_invalid')
    pump(e, lambda: job_status(e, job['id']) == 'failed')
    assert row(e, 'SELECT note FROM targets WHERE id=?', target)['note'] == 'needs_fix: target_invalid'
    for _ in range(3):
        e.clock.advance(400)
        observe(e, {'A': RoomStatus.LIVE})
        e.scheduler.tick()
    assert len(e.children) == 1
    # 下一场同样不自动开录。
    observe(e, {'A': RoomStatus.ENDED})
    observe(e, {'A': RoomStatus.ENDED})
    e.network.rooms['A'] = ResolvedRoom('A', RoomStatus.LIVE, room_id='room-A-2')
    snap = e.scheduler.scanner.scan(['A'])
    e.clock.advance(2)
    snap.timestamp = e.clock()
    e.scheduler.apply_snapshot(snap)
    for _ in range(3):
        e.scheduler.tick()
    assert len(e.children) == 1
    e.store.enqueue_command('record', target)
    pump(e, lambda: len(e.children) == 2)
    assert row(e, 'SELECT note FROM targets WHERE id=?', target)['note'] is None
    e.children[1].exit(9, 'target_invalid')
    pump(e, lambda: row(e, 'SELECT note FROM targets WHERE id=?', target)['note'])
    # 重新添加同一地址也解除“需要修正”。
    assert e.store.add_target('A', now=e.clock()) == target
    assert row(e, 'SELECT note FROM targets WHERE id=?', target)['note'] is None


@pytest.mark.parametrize('reset_ok', [True, False])
def test_login_blocked_resets_instance_then_retries(system, reset_ok):
    e = system
    e.reset_ok = reset_ok
    e.child_options = dict(record=False)
    target, job = start_one(e)
    vm = job['vmindex']
    e.children[0].exit(15, 'login_blocked')
    pump(e, lambda: e.resets == [vm] and row(e, 'SELECT status FROM instances WHERE vmindex=?', vm)['status'] != 'resetting')
    slot = row(e, 'SELECT * FROM instances WHERE vmindex=?', vm)
    assert (slot['status'], slot['reason']) == (('idle', None) if reset_ok else ('offline', 'reset_failed'))
    assert row(e, 'SELECT suppressed_session FROM targets WHERE id=?', target)['suppressed_session'] is None
    e.clock.advance(5)
    observe(e, {'A': RoomStatus.LIVE})
    pump(e, lambda: len(e.children) == 2)
    assert e.children[1].spec.vmindex == (vm if reset_ok else 3 - vm)


def test_device_offline_moves_job_to_other_instance(system):
    e = system
    e.child_options = dict(record=False)
    target, job = start_one(e)
    e.children[0].exit(20, 'device_offline')
    pump(e, lambda: job_status(e, job['id']) == 'failed')
    assert row(e, 'SELECT status FROM instances WHERE vmindex=?', job['vmindex'])['status'] == 'offline'
    e.clock.advance(5)
    observe(e, {'A': RoomStatus.LIVE})
    pump(e, lambda: len(e.children) == 2)
    assert e.children[1].spec.vmindex != job['vmindex']


def test_instance_refresh_follows_power_state_but_keeps_busy_slot(system):
    e = system
    e.scheduler.auto_scan = True
    def refreshed(vm, status):
        e.clock.advance(31)
        pump(e, lambda: row(e, 'SELECT status FROM instances WHERE vmindex=?', vm)['status'] == status)
    e.vms[2] = False
    refreshed(2, 'offline')
    assert row(e, 'SELECT reason FROM instances WHERE vmindex=2')['reason'] == 'android_not_started'
    e.vms[2] = True
    refreshed(2, 'idle')
    target, job = start_one(e)
    assert job['vmindex'] == 1
    e.vms.update({1: False, 2: False})
    refreshed(2, 'offline')
    slot = row(e, 'SELECT * FROM instances WHERE vmindex=1')
    assert (slot['status'], slot['job_id']) == ('busy', job['id'])
    # 查询失败保持原状态。
    def broken():
        raise RuntimeError('MuMuManager unavailable')
    e.pool.refresh = broken
    e.clock.advance(31)
    for _ in range(20):
        e.scheduler.tick()
        time.sleep(.005)
    assert row(e, 'SELECT status FROM instances WHERE vmindex=2')['status'] == 'offline'


def test_scheduler_shutdown_does_not_suppress_session(system):
    e = system
    target, job = start_one(e)
    e.scheduler.close()
    assert job_status(e, job['id']) == 'finished'
    assert row(e, 'SELECT suppressed_session FROM targets WHERE id=?', target)['suppressed_session'] is None
    e.scheduler = new_scheduler(e)
    observe(e, {'A': RoomStatus.LIVE})
    pump(e, lambda: len(e.children) == 2)


def write_journal(job, events):
    path = Path(job['event_log_path'])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(ev) + '\n' for ev in events), encoding='utf-8')


def test_reconcile_absent_process_replays_journal_and_resumes(system):
    e = system
    target, job = start_one(e)
    child = e.children[0]
    assert child.spec.log_path == Path(job['event_log_path']).with_suffix('.log')
    stopped_at = e.clock() - 100
    write_journal(job, [
        dict(event='segment_started', event_id=f"{job['id']}:2", job_id=job['id'], segment_index=1, file_path=str(child.file)),
        dict(event='segment_finished', event_id='late-1', job_id=job['id'], segment_index=1, file_path=str(child.file),
             duration_sec=1, file_size_mb=.01, is_final=True),
        dict(event='worker_stopped', event_id='late-2', job_id=job['id'], reason='event_stream_lost', exit_code=0,
             timestamp=stopped_at)])
    e.scheduler._handles.clear()
    e.scheduler.reconcile()
    finished = row(e, 'SELECT * FROM jobs WHERE id=?', job['id'])
    assert (finished['status'], finished['error_reason'], finished['collection_complete']) == ('finished', 'event_stream_lost', 1)
    assert finished['ended_at'] == stopped_at
    assert row(e, 'SELECT status FROM segments WHERE job_id=?', job['id'])['status'] == 'finished'
    assert row(e, 'SELECT suppressed_session FROM targets WHERE id=?', target)['suppressed_session'] is None
    e.clock.advance(5)
    observe(e, {'A': RoomStatus.LIVE})
    pump(e, lambda: len(e.children) == 2)


def test_reconcile_waits_for_owned_old_worker_then_resumes(system):
    e = system
    target, job = start_one(e)
    e.scheduler._handles.clear()
    e.scheduler.process_probe = lambda record: 'alive'
    e.scheduler.reconcile()
    waiting = row(e, 'SELECT * FROM jobs WHERE id=?', job['id'])
    assert (waiting['status'], waiting['error_reason']) == ('stopping', 'waiting_old_worker_exit')
    assert row(e, 'SELECT status FROM instances WHERE vmindex=?', job['vmindex'])['status'] == 'busy'
    for _ in range(3):
        e.clock.advance(3)
        observe(e, {'A': RoomStatus.LIVE})
        e.scheduler.tick()
    assert len(e.children) == 1 and job_status(e, job['id']) == 'stopping'
    # 旧 Worker 自行封口退出后，按退避续录同一场。
    e.scheduler.process_probe = lambda record: 'absent'
    e.clock.advance(3)
    e.scheduler.tick()
    assert job_status(e, job['id']) == 'failed'
    assert row(e, 'SELECT error_reason FROM jobs WHERE id=?', job['id'])['error_reason'] == 'interrupted_host_restart'
    e.clock.advance(5)
    observe(e, {'A': RoomStatus.LIVE})
    pump(e, lambda: len(e.children) == 2)


def test_reconcile_old_worker_not_exiting_or_unowned_needs_manual(system):
    e = system
    target, job = start_one(e)
    e.scheduler._handles.clear()
    e.scheduler.process_probe = lambda record: 'alive'
    e.scheduler.reconcile()
    e.clock.advance(121)
    e.scheduler.tick()
    held = row(e, 'SELECT * FROM jobs WHERE id=?', job['id'])
    assert (held['status'], held['error_reason']) == ('needs_reconcile', 'old_worker_did_not_exit')
    assert row(e, 'SELECT status FROM instances WHERE vmindex=?', job['vmindex'])['status'] == 'reconcile'
    # 归属无法确认：保持人工对账，不派新任务，也不终止进程。
    e.scheduler.process_probe = lambda record: 'unknown'
    e.scheduler.reconcile(job['id'])
    assert job_status(e, job['id']) == 'needs_reconcile'
    for _ in range(3):
        e.clock.advance(10)
        observe(e, {'A': RoomStatus.LIVE})
        e.scheduler.tick()
    assert len(e.children) == 1 and e.children[0].alive
    e.scheduler.process_probe = lambda record: 'absent'
    e.scheduler.reconcile(job['id'])
    assert job_status(e, job['id']) == 'failed'
    assert row(e, 'SELECT suppressed_session FROM targets WHERE id=?', target)['suppressed_session'] is None


def test_unblock_command_is_removed(system):
    with pytest.raises(ValueError):
        system.store.enqueue_command('unblock', '1')

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


def test_quality_warnings_for_one_segment_are_all_kept(tmp_path):
    store = Store(tmp_path / 'quality.db')
    target = store.add_target('A', now=1.0)
    with store.transaction() as db:
        db.execute("INSERT INTO jobs(id,target_id,session_key,status,created_at) VALUES('job',?,'s','running',1)", (target,))
    for n, flag in enumerate(('overlay_live_panel_window', 'low_frame_rate', 'visual_check_unavailable')):
        store.record_event('job', {'event': 'segment_quality_warning', 'event_id': f'e{n}',
                                   'segment_index': 3, 'flag': flag}, 2.0)
    store.record_event('job', {'event': 'segment_quality_warning', 'event_id': 'e1',
                               'segment_index': 3, 'flag': 'low_frame_rate'}, 2.0)
    risks = json.loads(store.row("SELECT risk_json FROM segments WHERE job_id='job' AND segment_index=3")['risk_json'])
    assert [r['flag'] for r in risks] == ['overlay_live_panel_window', 'low_frame_rate', 'visual_check_unavailable']

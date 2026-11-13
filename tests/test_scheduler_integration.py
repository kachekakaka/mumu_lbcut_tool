"""真实 SQLite + Scanner + InstancePool + Scheduler 集成；仅替换网络和 Worker 进程边界。"""
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest
from src.mvp.room_resolver import RoomStatus, ResolvedRoom
from src.orchestrator.instance_pool import InstancePool
from src.orchestrator.scheduler import Scheduler
from src.orchestrator.storage import Store
from console_helpers import ImmediateQueries, observe_room


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
    env = SimpleNamespace(clock=clock, network=network, store=store, children=[], tmp=tmp_path, pool=pool,
                          vms=vms, child_options={})
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
        query_executor=ImmediateQueries(env.network), handle_factory=env.factory, clock=env.clock,
        ended_interval_sec=1, auto_scan=False, process_probe=lambda job: 'absent')

def observe(env, values):
    env.clock.advance(2)
    for raw, status in values.items():
        env.network.rooms[raw] = ResolvedRoom(raw, status, room_id='room-' + raw)
    for target in env.store.rows("SELECT * FROM targets WHERE enabled=1"):
        if target['raw'] in env.network.rooms:
            observe_room(env.store, target['id'], env.network.rooms[target['raw']], env.clock())


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


@pytest.mark.parametrize('notice', ['stall_detected','capture_startup'])
def test_login_checks_end_without_waiting_for_periodic_scan(system,notice):
    e = system
    target, job = start_one(e)
    e.scheduler.auto_scan = True
    e.scheduler._next_scan = e.clock() + 600
    e.network.resolved.clear()
    e.network.rooms['A'] = ResolvedRoom('A', RoomStatus.ENDED)
    e.children[0].emit(notice, reason='login_prompt_detected',
                       action='rejected' if notice=='capture_startup' else 'relocate_target')
    e.scheduler.tick()
    e.scheduler.tick()
    assert e.network.resolved == ['A']
    assert row(e, 'SELECT trusted_status,ended_count FROM targets WHERE id=?', target) == {'trusted_status':'live', 'ended_count':1}
    assert e.children[0].alive
    e.clock.advance(.5)
    e.scheduler.tick()
    assert e.network.resolved == ['A']
    e.clock.advance(.6)
    pump(e, lambda: not e.children[0].alive)
    assert e.network.resolved == ['A', 'A']
    assert row(e, 'SELECT stop_reason FROM jobs WHERE id=?', job['id'])['stop_reason'] == 'anchor_offline'


@pytest.mark.parametrize('status', [RoomStatus.LIVE, RoomStatus.UNKNOWN])
def test_login_live_or_unknown_does_not_mean_offline(system, status):
    e = system
    target, job = start_one(e)
    e.network.rooms['A'] = ResolvedRoom('A', status, room_id='room-A')
    e.children[0].emit('stall_detected', reason='login_prompt_detected')
    e.scheduler.tick()
    e.scheduler.tick()
    assert e.children[0].alive
    assert row(e, 'SELECT trusted_status FROM targets WHERE id=?', target)['trusted_status'] == 'live'
    assert row(e, 'SELECT stop_reason FROM jobs WHERE id=?', job['id'])['stop_reason'] is None
    e.clock.advance(121)
    e.scheduler.tick()
    assert not e.scheduler._login_checks
    assert e.children[0].alive


@pytest.mark.parametrize('change', ['healed', 'capture_started', 'removed', 'newer_observation', 'expired', 'new_recovery'])
def test_login_late_result_cannot_override_current_state(system, change):
    from console_helpers import DeferredQueries
    from src.orchestrator.room_query_process import QueryResult
    e = system
    target, job = start_one(e)
    queries = DeferredQueries()
    e.scheduler.room_queries = e.scheduler.observations.executor = queries
    e.children[0].emit('stall_detected', reason='login_prompt_detected')
    e.scheduler.tick()
    assert queries.calls[0][:2] == ('login', 'A')
    if change == 'healed':
        e.children[0].emit('stall_healed')
    elif change == 'capture_started':
        e.children[0].emit('capture_session',action='started')
    elif change == 'removed':
        with e.store.transaction() as db:
            db.execute('UPDATE targets SET enabled=0,control_revision=control_revision+1 WHERE id=?', (target,))
    elif change == 'newer_observation':
        observe_room(e.store, target, ResolvedRoom('A', RoomStatus.LIVE, room_id='room-A'), e.clock())
    elif change == 'new_recovery':
        # 同一时钟刻度也必须区分两轮恢复，不能用到期时间当作身份。
        e.children[0].emit('stall_healed')
        e.children[0].emit('stall_detected', reason='login_prompt_detected')
    else:
        e.clock.advance(121)
    queries.calls[0][2].set_result(QueryResult(ResolvedRoom('A', RoomStatus.ENDED)))
    e.scheduler.tick()
    assert row(e, 'SELECT ended_count FROM targets WHERE id=?', target)['ended_count'] == 0


def test_periodic_scan_does_not_supersede_login_check(system):
    e = system
    target, job = start_one(e)
    e.scheduler.auto_scan = True
    e.scheduler._next_scan = 0
    e.network.rooms['A'] = ResolvedRoom('A', RoomStatus.ENDED)
    e.children[0].emit('stall_detected', reason='login_prompt_detected')
    e.scheduler.tick()
    e.scheduler.tick()
    assert row(e, 'SELECT ended_count FROM targets WHERE id=?', target)['ended_count'] == 1


def test_login_recovery_keeps_checking_if_live_then_ends(system):
    e = system
    target, job = start_one(e)
    e.children[0].emit('stall_detected', reason='login_prompt_detected')
    e.scheduler.tick()
    e.scheduler.tick()
    assert e.children[0].alive and job['id'] in e.scheduler._login_checks
    e.network.rooms['A'] = ResolvedRoom('A', RoomStatus.ENDED)
    e.clock.advance(1.1)
    e.scheduler.tick()
    e.scheduler.tick()
    assert e.children[0].alive
    e.clock.advance(1.1)
    pump(e, lambda: not e.children[0].alive)
    assert row(e, 'SELECT stop_reason FROM jobs WHERE id=?', job['id'])['stop_reason'] == 'anchor_offline'


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
    e.clock.advance(2)
    observe_room(e.store, target, e.network.rooms['A'], e.clock())
    for _ in range(3):
        e.scheduler.tick()
    assert len(e.children) == 1
    e.store.enqueue_command('record', target)
    pump(e, lambda: len(e.children) == 2)
    assert row(e, 'SELECT note FROM targets WHERE id=?', target)['note'] is None
    e.children[1].exit(9, 'target_invalid')
    pump(e, lambda: row(e, 'SELECT note FROM targets WHERE id=?', target)['note'])
    # 重复添加不解除“需要修正”，必须显式重新开录。
    assert e.store.add_target('A', now=e.clock()) == target
    assert row(e, 'SELECT note FROM targets WHERE id=?', target)['note'] == 'needs_fix: target_invalid'


def test_login_blocked_releases_instance_and_retries_after_backoff(system):
    e = system
    e.child_options = dict(record=False)
    target, job = start_one(e)
    vm = job['vmindex']
    e.children[0].exit(15, 'login_blocked')
    pump(e, lambda: job_status(e, job['id']) == 'failed')
    slot = row(e, 'SELECT * FROM instances WHERE vmindex=?', vm)
    assert (slot['status'], slot['reason'], slot['job_id']) == ('idle', 'login_blocked', None)
    e.scheduler.tick()
    assert len(e.children) == 1
    assert row(e, 'SELECT suppressed_session FROM targets WHERE id=?', target)['suppressed_session'] is None
    e.clock.advance(5)
    observe(e, {'A': RoomStatus.LIVE})
    pump(e, lambda: len(e.children) == 2)
    assert e.children[1].spec.vmindex == vm


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


def test_dynamic_instance_discovery_defaults_disabled_and_cleans_removed_ghost(tmp_path):
    clock, network = Clock(), Network()
    store = Store(tmp_path / 'state' / 'orchestrator.sqlite3')
    vms = {2: True, 3: False}
    mumu = SimpleNamespace(list_available_instances=lambda: [
        dict(vmindex=i, name=f'VM{i}', android_started=started) for i, started in sorted(vms.items())])
    pool = InstancePool(mumu)
    children = []
    scheduler = Scheduler(
        store, tmp_path / 'recordings', None, pool=pool,
        query_executor=ImmediateQueries(network),
        handle_factory=lambda **kw: children.append(Child(**kw)) or children[-1],
        clock=clock, ended_interval_sec=1, auto_scan=True, process_probe=lambda job: 'absent',
    )
    try:
        rows = {r['vmindex']: r for r in store.rows('SELECT * FROM instances ORDER BY vmindex')}
        assert set(rows) == {2, 3}
        assert (rows[2]['status'], rows[2]['enabled']) == ('idle', 0)
        assert (rows[3]['status'], rows[3]['reason'], rows[3]['enabled']) == ('offline', 'android_not_started', 0)

        # 未启用任何实例时不派发录制任务
        target = store.add_target('A', now=clock())
        network.rooms['A'] = ResolvedRoom('A', RoomStatus.LIVE, room_id='room-A')
        observe_room(store, target, network.rooms['A'], clock())
        scheduler.tick()
        assert not children

        # 未启用实例在宿主机消失后应随刷新自动删除
        vms.pop(3)
        clock.advance(31)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and any(r['vmindex'] == 3 for r in store.rows('SELECT vmindex FROM instances')):
            scheduler.tick()
            time.sleep(.005)
        assert [r['vmindex'] for r in store.rows('SELECT vmindex FROM instances')] == [2]

        # 手动启用 2 后重开无预设调度器仍保留启用状态，且开始派发
        scheduler.close()
        with store.transaction() as db:
            db.execute("UPDATE instances SET enabled=1 WHERE vmindex=2")
        scheduler = Scheduler(
            store, tmp_path / 'recordings', None, pool=pool,
            query_executor=ImmediateQueries(network),
            handle_factory=lambda **kw: children.append(Child(**kw)) or children[-1],
            clock=clock, ended_interval_sec=1, auto_scan=False, process_probe=lambda job: 'absent',
        )
        assert store.row('SELECT enabled FROM instances WHERE vmindex=2')['enabled'] == 1
        observe_room(store, target, network.rooms['A'], clock())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not children:
            scheduler.tick()
            time.sleep(.005)
        assert len(children) == 1 and children[0].spec.vmindex == 2
    finally:
        scheduler.close()
        store.close()


def make_boot_env(tmp_path, vms, preset=(1, 2), auto_scan=False):
    import threading
    clock, network = Clock(), Network()
    store = Store(tmp_path / 'state' / 'orchestrator.sqlite3')
    shutdowns = []
    boot_calls = []
    boot_gate = {}
    boot_results = {}
    mumu = SimpleNamespace(
        list_available_instances=lambda: [
            dict(vmindex=i, name=f'VM{i}', android_started=started) for i, started in sorted(vms.items())
        ],
        shutdown_player=lambda vmindex: shutdowns.append(vmindex) or True,
    )
    pool = InstancePool(mumu)

    def boot_instance(vmindex, stop_event=None):
        boot_calls.append(vmindex)
        gate = boot_gate.get(vmindex)
        if gate is not None:
            gate.wait(timeout=2)
        ok = boot_results.get(vmindex, True)
        if ok:
            vms[vmindex] = True
        return ok

    pool.boot_instance = boot_instance
    children = []
    env = SimpleNamespace(
        clock=clock, network=network, store=store, pool=pool, vms=vms,
        children=children, shutdowns=shutdowns, boot_calls=boot_calls,
        boot_gate=boot_gate, boot_results=boot_results, threading=threading,
    )
    env.scheduler = Scheduler(
        store, tmp_path / 'recordings', list(preset) if preset is not None else None,
        pool=pool, query_executor=ImmediateQueries(network),
        handle_factory=lambda **kw: children.append(Child(**kw)) or children[-1],
        clock=clock, ended_interval_sec=1, auto_scan=auto_scan, process_probe=lambda job: 'absent',
    )
    return env


def test_on_demand_boot_starts_offline_enabled_and_keeps_idle_after_finish(tmp_path):
    e = make_boot_env(tmp_path, {1: False, 2: False}, preset=[1, 2])
    try:
        # 无排队任务时不触发自动开机
        e.scheduler.tick()
        assert e.boot_calls == []
        assert row(e, 'SELECT status FROM instances WHERE vmindex=1')['status'] == 'offline'

        # 门控 1 号开机过程，验证开机期间状态、refresh 保护与任务保留 pending
        gate1 = e.threading.Event()
        e.boot_gate[1] = gate1
        target_a = e.store.add_target('A', now=e.clock())
        observe(e, {'A': RoomStatus.LIVE})
        pump(e, lambda: row(e, 'SELECT status FROM instances WHERE vmindex=1')['status'] == 'booting')
        assert e.boot_calls == [1]
        assert row(e, 'SELECT status FROM instances WHERE vmindex=2')['status'] == 'offline'
        assert row(e, 'SELECT attempts FROM targets WHERE id=?', target_a)['attempts'] == 0
        assert row(e, 'SELECT status FROM jobs WHERE target_id=?', target_a)['status'] == 'pending'

        # 即使运行期定期刷新仍看到旧状态，也不应覆盖 booting
        e.store.sync_instances(e.pool.refresh(), e.clock())
        assert row(e, 'SELECT status FROM instances WHERE vmindex=1')['status'] == 'booting'

        # 开机完成后立即接单录制
        gate1.set()
        pump(e, lambda: len(e.children) == 1 and row(e, 'SELECT status FROM jobs WHERE target_id=?', target_a)['status'] == 'running')
        assert e.children[0].spec.vmindex == 1

        # 下播后不自动关机，实例留在 idle 且下一场直接复用 1 号，不额外开机 2 号
        observe(e, {'A': RoomStatus.ENDED})
        observe(e, {'A': RoomStatus.ENDED})
        pump(e, lambda: row(e, 'SELECT status FROM instances WHERE vmindex=1')['status'] == 'idle')
        assert e.shutdowns == []

        target_b = e.store.add_target('B', now=e.clock())
        observe(e, {'B': RoomStatus.LIVE})
        pump(e, lambda: len(e.children) == 2)
        assert e.children[1].spec.vmindex == 1
        assert e.boot_calls == [1]
    finally:
        for ev in e.boot_gate.values():
            ev.set()
        e.scheduler.close()
        e.store.close()


def test_boot_serial_cap_skips_disabled_and_not_discovered(tmp_path):
    e = make_boot_env(tmp_path, {1: False, 2: False, 3: False}, preset=[1, 2])
    try:
        # 增加一条已启用但宿主机已不存在的旧记录，绝不能被拉起
        with e.store.transaction() as db:
            db.execute("INSERT INTO instances(vmindex,name,status,enabled,reason,updated_at) VALUES(9,'VM9','offline',1,'not_discovered',1)")
        gate1 = e.threading.Event()
        gate2 = e.threading.Event()
        e.boot_gate[1] = gate1
        e.boot_gate[2] = gate2

        for raw in ('A', 'B', 'C'):
            e.store.add_target(raw, now=e.clock())
        observe(e, {'A': RoomStatus.LIVE, 'B': RoomStatus.LIVE, 'C': RoomStatus.LIVE})

        # 多任务同时排队时严格串行，一次只允许 1 台处于 booting
        pump(e, lambda: row(e, 'SELECT status FROM instances WHERE vmindex=1')['status'] == 'booting')
        for _ in range(5):
            e.scheduler.tick()
        assert len([r for r in e.store.rows('SELECT * FROM instances') if r['status'] == 'booting']) == 1
        assert e.boot_calls == [1]

        # 1 号就绪派走 A 后，才继续拉起 2 号
        gate1.set()
        pump(e, lambda: len(e.children) == 1 and row(e, 'SELECT status FROM instances WHERE vmindex=2')['status'] == 'booting')
        assert e.boot_calls == [1, 2]

        gate2.set()
        pump(e, lambda: len(e.children) == 2)
        # 3 号未启用、9 号 not_discovered，两者都不得开机
        assert e.boot_calls == [1, 2]
        assert row(e, 'SELECT status FROM instances WHERE vmindex=3')['status'] == 'offline'
        assert row(e, 'SELECT status FROM instances WHERE vmindex=9')['status'] == 'offline'
    finally:
        for ev in e.boot_gate.values():
            ev.set()
        e.scheduler.close()
        e.store.close()


def test_boot_failure_backoff_rotates_healthy_instance_and_clears_on_manual_boot(tmp_path):
    e = make_boot_env(tmp_path, {1: False, 2: False}, preset=[1, 2])
    try:
        e.boot_results[1] = False
        e.boot_results[2] = True
        target_a = e.store.add_target('A', now=e.clock())
        observe(e, {'A': RoomStatus.LIVE})

        # 1 号开机失败 -> 进冷却 30s、任务不扣 attempts，下一轮立即轮转 2 号开机成功
        pump(e, lambda: len(e.children) == 1)
        assert e.children[0].spec.vmindex == 2
        assert e.boot_calls == [1, 2]
        vm1 = row(e, 'SELECT * FROM instances WHERE vmindex=1')
        assert (vm1['status'], vm1['reason'], vm1['boot_attempts']) == ('offline', 'launch_failed', 1)
        assert vm1['boot_retry_at'] == pytest.approx(e.clock() + 30)
        assert row(e, 'SELECT attempts FROM targets WHERE id=?', target_a)['attempts'] == 0

        # 冷却期内定期刷新应保留 launch_failed 原因码，且不重试 1 号
        e.store.sync_instances(e.pool.refresh(), e.clock())
        assert row(e, 'SELECT reason FROM instances WHERE vmindex=1')['reason'] == 'launch_failed'
        target_b = e.store.add_target('B', now=e.clock())
        observe(e, {'B': RoomStatus.LIVE})
        e.scheduler.tick()
        assert e.boot_calls == [1, 2]
        e.clock.advance(31)
        pump(e, lambda: e.boot_calls == [1, 2, 1] and row(e, 'SELECT boot_attempts FROM instances WHERE vmindex=1')['boot_attempts'] == 2)
        vm1 = row(e, 'SELECT * FROM instances WHERE vmindex=1')
        assert vm1['boot_retry_at'] == pytest.approx(e.clock() + 60)

        # 手动在 MuMu 开启 1 号后，刷新探测到在线即清零冷却并直接派单
        e.vms[1] = True
        e.store.sync_instances(e.pool.refresh(), e.clock())
        vm1 = row(e, 'SELECT * FROM instances WHERE vmindex=1')
        assert (vm1['status'], vm1['boot_attempts'], vm1['boot_retry_at']) == ('idle', 0, 0)
        pump(e, lambda: len(e.children) == 2)
        assert e.children[1].spec.vmindex == 1
    finally:
        e.scheduler.close()
        e.store.close()


def test_booting_survives_pending_cancel_and_recovers_stale_booting_on_restart(tmp_path):
    e = make_boot_env(tmp_path, {1: False}, preset=[1])
    try:
        gate1 = e.threading.Event()
        e.boot_gate[1] = gate1
        target_a = e.store.add_target('A', now=e.clock())
        observe(e, {'A': RoomStatus.LIVE})
        pump(e, lambda: row(e, 'SELECT status FROM instances WHERE vmindex=1')['status'] == 'booting')

        # 开机途中任务被手动取消，已启动的开机流程继续完成到 idle 且不关机
        job_id = row(e, 'SELECT id FROM jobs WHERE target_id=?', target_a)['id']
        e.store.enqueue_command('stop', job_id)
        pump(e, lambda: row(e, 'SELECT status FROM jobs WHERE id=?', job_id)['status'] == 'cancelled')
        gate1.set()
        pump(e, lambda: row(e, 'SELECT status FROM instances WHERE vmindex=1')['status'] == 'idle')
        assert e.shutdowns == [] and e.children == []

        # 模拟服务异常关闭遗留 booting 状态，新调度器初始化时应自动纠偏回真实探测状态
        e.scheduler.close()
        e.vms[1] = False
        with e.store.transaction() as db:
            db.execute("UPDATE instances SET status='booting',reason='booting' WHERE vmindex=1")
        e.scheduler = Scheduler(
            e.store, tmp_path / 'recordings', [1], pool=e.pool,
            query_executor=ImmediateQueries(e.network),
            handle_factory=lambda **kw: e.children.append(Child(**kw)) or e.children[-1],
            clock=e.clock, ended_interval_sec=1, auto_scan=False, process_probe=lambda job: 'absent',
        )
        vm1 = row(e, 'SELECT * FROM instances WHERE vmindex=1')
        assert (vm1['status'], vm1['reason']) == ('offline', 'android_not_started')
    finally:
        for ev in e.boot_gate.values():
            ev.set()
        e.scheduler.close()
        e.store.close()


def test_exit_20_refreshes_and_reboots_crashed_enabled_instance(tmp_path):
    e = make_boot_env(tmp_path, {1: True}, preset=[1])
    try:
        target_a = e.store.add_target('A', now=e.clock())
        observe(e, {'A': RoomStatus.LIVE})
        pump(e, lambda: len(e.children) == 1)

        # 模拟模拟器崩溃导致 Worker 以退出码 20 结束，宿主机实例掉线
        e.vms[1] = False
        e.children[0].exit(20, 'device_unavailable')
        pump(e, lambda: row(e, 'SELECT status FROM jobs WHERE target_id=?', target_a)['status'] == 'failed')

        # 调度器应主动刷新查清为 android_not_started，并在重试时间到来后自动拉起续录
        pump(e, lambda: row(e, 'SELECT reason FROM instances WHERE vmindex=1')['reason'] == 'android_not_started')
        e.clock.advance(10)
        observe(e, {'A': RoomStatus.LIVE})
        pump(e, lambda: len(e.children) == 2 and e.boot_calls == [1])
    finally:
        e.scheduler.close()
        e.store.close()


def test_restart_with_same_preset_keeps_boot_backoff(tmp_path):
    e = make_boot_env(tmp_path, {1: False}, preset=[1])
    try:
        e.boot_results[1] = False
        e.store.add_target('A', now=e.clock())
        observe(e, {'A': RoomStatus.LIVE})
        pump(e, lambda: row(e, 'SELECT boot_attempts FROM instances WHERE vmindex=1')['boot_attempts'] == 1)
        before = row(e, 'SELECT boot_attempts,boot_retry_at FROM instances WHERE vmindex=1')
        e.scheduler.close()
        e.scheduler = Scheduler(e.store, tmp_path / 'recordings', [1], pool=e.pool,
                                query_executor=ImmediateQueries(e.network), clock=e.clock, auto_scan=False)
        assert row(e, 'SELECT boot_attempts,boot_retry_at FROM instances WHERE vmindex=1') == before
        e.scheduler.tick()
        assert e.boot_calls == [1]
        e.clock.advance(31)
        pump(e, lambda: row(e, 'SELECT boot_attempts FROM instances WHERE vmindex=1')['boot_attempts'] == 2)
        assert e.boot_calls == [1, 1]
    finally:
        e.scheduler.close()
        e.store.close()


def test_failed_instance_snapshot_keeps_enabled_and_disabled_rows(system, monkeypatch, caplog):
    from src.mvp.mumu_client import MuMuClient
    e = system
    with e.store.transaction() as db:
        db.execute('UPDATE instances SET enabled=0 WHERE vmindex=2')
    before = e.store.rows('SELECT * FROM instances ORDER BY vmindex')
    mumu = MuMuClient.__new__(MuMuClient)
    monkeypatch.setattr(mumu, '_run_manager', lambda *args, **kwargs: 'mainnx connect failed')
    e.pool._mumu = mumu
    e.scheduler._force_refresh = True
    e.scheduler.tick()
    pump(e, lambda: e.scheduler._refresh_future is None)
    assert e.store.rows('SELECT * FROM instances ORDER BY vmindex') == before
    assert '实例查询失败，保留原状态' in caplog.text


def test_close_waits_for_boot_before_releasing_host_lease(tmp_path):
    from src.orchestrator.scheduler import HostLease
    e = make_boot_env(tmp_path, {1: False}, preset=[1])
    entered, cancelled, finish, closed = [e.threading.Event() for _ in range(4)]
    errors = []
    def boot(vmindex, stop_event):
        entered.set()
        if stop_event.wait(3):
            cancelled.set()
        finish.wait(3)  # 模拟正在执行的单次有界外部命令退出。
        return False
    e.pool.boot_instance = boot
    def close():
        try:
            e.scheduler.close()
        except Exception as exc:
            errors.append(exc)
        finally:
            closed.set()
    closer = e.threading.Thread(target=close)
    try:
        e.store.add_target('A', now=e.clock())
        observe(e, {'A': RoomStatus.LIVE})
        e.scheduler.tick()
        assert entered.wait(1)
        closer.start()
        assert cancelled.wait(1)
        assert not closed.is_set()
        with pytest.raises(RuntimeError, match='已经有调度器'):
            HostLease(e.store.path.with_suffix('.host.lock'))
        finish.set()
        assert closed.wait(2)
        assert not errors
        assert not e.scheduler._boots
        vm = row(e, 'SELECT status,reason,boot_attempts FROM instances WHERE vmindex=1')
        assert vm == dict(status='offline', reason='boot_interrupted', boot_attempts=0)
        lease = HostLease(e.store.path.with_suffix('.host.lock'))
        lease.close()
        e.scheduler = Scheduler(e.store, tmp_path / 'recordings', [1], pool=e.pool,
                                query_executor=ImmediateQueries(e.network), clock=e.clock, auto_scan=False)
        assert row(e, 'SELECT reason FROM instances WHERE vmindex=1')['reason'] == 'android_not_started'
    finally:
        finish.set()
        if closer.ident is not None:
            closer.join(4)
        e.scheduler.close()
        e.store.close()

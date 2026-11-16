"""本场意图、命令事务与所有来源的迟到观测回归。"""
import json
import sqlite3
import uuid

import pytest
from src.mvp.room_resolver import ResolvedRoom, RoomStatus
from src.orchestrator.storage import Store, ControlError
from src.orchestrator.room_query_process import QueryResult
from console_helpers import DeferredQueries, observe_room
from test_scheduler_integration import system, start_one, observe, pump, row, new_scheduler


def command(e, kind, subject, payload=None):
    key = e.store.enqueue_command(kind, subject, payload=payload)
    e.scheduler.tick()
    return key


def result(e, key):
    return row(e, 'SELECT * FROM commands WHERE id=?', key)


def test_intent_survives_auto_off_failure_restart_and_target_stop(system):
    e = system
    target, job = start_one(e)
    command(e, 'update', target, {'auto_record': False})
    e.children[0].exit(30)
    pump(e, lambda: row(e, 'SELECT status FROM jobs WHERE id=?', job['id'])['status'] == 'failed')
    t = row(e, 'SELECT * FROM targets WHERE id=?', target)
    assert not t['auto_record'] and t['record_session_key'] == t['session_key']
    e.scheduler.close()
    e.scheduler = new_scheduler(e)
    e.clock.advance(5)
    observe(e, {'A': RoomStatus.LIVE})
    pump(e, lambda: len(e.children) == 2)
    e.children[1].exit(30)
    pump(e, lambda: not e.store.active_job(target))
    key = command(e, 'stop_target', target, {'session_key': t['session_key']})
    assert json.loads(result(e, key)['result_json'])['outcome'] == 'intent_cancelled'
    command(e, 'update', target, {'auto_record': True})
    e.clock.advance(600)
    observe(e, {'A': RoomStatus.LIVE})
    e.scheduler.tick()
    t = row(e, 'SELECT * FROM targets WHERE id=?', target)
    assert t['enabled'] and t['auto_record'] and t['record_session_key'] is None
    assert len(e.children) == 2


def test_add_restore_partial_results_and_policy_preservation(system):
    e = system
    payload = dict(targets=[' A ', '', 'https://invalid.example/foo'], manual_only=True, allow_unconfirmed_identity=True)
    key = command(e, 'add', '', payload)
    items = json.loads(result(e, key)['result_json'])['items']
    assert [v['outcome'] for v in items] == ['created', 'error', 'error']
    target = items[0]['target_id']
    with e.store.transaction() as db:
        db.execute("UPDATE targets SET note='needs_fix: wrong',suppressed_session='s' WHERE id=?", (target,))
    payload = dict(targets=['A'], manual_only=False, allow_unconfirmed_identity=False)
    key = command(e, 'add', '', payload)
    assert json.loads(result(e, key)['result_json'])['items'][0]['outcome'] == 'existing'
    assert not row(e, 'SELECT auto_record FROM targets WHERE id=?', target)['auto_record']
    command(e, 'remove', target)
    key = command(e, 'add', '', payload)
    assert json.loads(result(e, key)['result_json'])['items'][0]['outcome'] == 'restored'
    t = row(e, 'SELECT * FROM targets WHERE id=?', target)
    assert t['enabled'] and t['auto_record'] and t['allow_unconfirmed']
    assert (t['note'], t['suppressed_session']) == ('needs_fix: wrong', 's')


def test_command_atomicity_before_commit_and_after_commit(system, monkeypatch):
    e = system
    target = e.store.add_target('A')
    key = e.store.enqueue_command('update', target, payload={'auto_record': False})
    real = e.store.complete_command
    def fail(*a, **kw):
        raise OSError('提交前故障')
    monkeypatch.setattr(e.store, 'complete_command', fail)
    with pytest.raises(OSError):
        e.scheduler.tick()
    assert row(e, 'SELECT auto_record FROM targets WHERE id=?', target)['auto_record']
    assert result(e, key)['status'] == 'pending'
    monkeypatch.setattr(e.store, 'complete_command', real)
    e.scheduler.tick()
    e.store.interrupt_commands(e.clock())
    assert not row(e, 'SELECT auto_record FROM targets WHERE id=?', target)['auto_record']
    assert result(e, key)['status'] == 'succeeded'


def test_idempotency_precedes_removed_subject_check(system):
    e = system
    target = e.store.add_target('A')
    rid = str(uuid.uuid4())
    key = e.store.enqueue_command('remove', target, request_id=rid)
    e.scheduler.tick()
    assert e.store.enqueue_command('remove', target, request_id=rid) == key
    with pytest.raises(ControlError, match='不同操作'):
        e.store.enqueue_command('record', target, request_id=rid)
    assert len(e.store.rows('SELECT * FROM commands')) == 1


def deferred(e):
    queries = DeferredQueries()
    e.scheduler.room_queries = e.scheduler.observations.executor = queries
    return queries


@pytest.mark.parametrize('mutation', ['stop_target', 'remove_restore', 'update'])
def test_manual_query_late_after_control_does_not_reopen(system, mutation):
    e = system
    target = e.store.add_target('A', auto_record=False)
    observe(e, {'A': RoomStatus.LIVE})
    queries = deferred(e)
    key = command(e, 'record', target)
    assert result(e, key)['status'] == 'running'
    if mutation == 'remove_restore':
        command(e, 'remove', target)
        command(e, 'add', '', dict(targets=['A'], manual_only=True, allow_unconfirmed_identity=False))
    elif mutation == 'stop_target':
        command(e, mutation, target, {'session_key': 'room:1065'})
    else:
        command(e, mutation, target, {'auto_record': False})
    queries.calls[0][2].set_result(QueryResult(ResolvedRoom('A', RoomStatus.LIVE, room_id='1065', anchor_uid='900001')))
    e.scheduler.tick()
    assert result(e, key)['error_code'] == 'target_changed'
    assert not e.store.active_job(target)


def test_old_scan_cannot_overwrite_new_manual_session(system):
    e = system
    target = e.store.add_target('A', auto_record=False)
    observe(e, {'A': RoomStatus.LIVE})
    queries = deferred(e)
    t = row(e, 'SELECT * FROM targets WHERE id=?', target)
    assert e.scheduler.observations.dispatch('scan', t)
    key = command(e, 'record', target)
    queries.calls[1][2].set_result(QueryResult(ResolvedRoom('A', RoomStatus.LIVE, room_id='2065', anchor_uid='900001')))
    e.scheduler.tick()
    assert result(e, key)['status'] == 'succeeded'
    queries.calls[0][2].set_result(QueryResult(ResolvedRoom('A', RoomStatus.LIVE, room_id='1065', anchor_uid='900001')))
    e.scheduler.tick()
    t = row(e, 'SELECT * FROM targets WHERE id=?', target)
    assert t['session_key'] == t['record_session_key'] == 'room:2065'
    assert e.store.active_job(target)['session_key'] == 'room:2065'


def test_superseded_verify_retries_and_unknown_blocks_old_live(system):
    e = system
    target = e.store.add_target('A')
    observe(e, {'A': RoomStatus.LIVE})
    queries = deferred(e)
    e.scheduler.tick()
    first_verify = queries.calls[0][2]
    t = row(e, 'SELECT * FROM targets WHERE id=?', target)
    e.scheduler.observations.dispatch('scan', t)
    queries.calls[1][2].set_result(QueryResult(ResolvedRoom('A', RoomStatus.UNKNOWN)))
    e.scheduler.tick()
    first_verify.set_result(QueryResult(ResolvedRoom('A', RoomStatus.LIVE, room_id='1065', anchor_uid='900001')))
    e.scheduler.tick()
    assert not e.children
    assert row(e, 'SELECT observed_status FROM targets WHERE id=?', target)['observed_status'] == 'unknown'
    assert len(queries.calls) == 3 and queries.calls[2][0] == 'verify'
    queries.calls[2][2].set_result(QueryResult(ResolvedRoom('A', RoomStatus.LIVE, room_id='1065', anchor_uid='900001')))
    pump(e, lambda: len(e.children) == 1)


def test_duplicate_ended_seq_and_wall_clock_rollback(system):
    e = system
    target = e.store.add_target('A', auto_record=False)
    observe(e, {'A': RoomStatus.LIVE})
    meta = e.store.begin_observation(target, e.clock() + 2)
    ended = ResolvedRoom('A', RoomStatus.ENDED)
    assert e.store.apply_observation(meta, ended, 1)
    assert not e.store.apply_observation(meta, ended, 1)
    assert row(e, 'SELECT ended_count FROM targets WHERE id=?', target)['ended_count'] == 1
    # 排序使用序号，即使墙上时钟回退，新 UNKNOWN 也不能被旧 LIVE 覆盖。
    old = e.store.begin_observation(target, e.clock() + 3)
    new = e.store.begin_observation(target, e.clock() - 3)
    assert e.store.apply_observation(new, ResolvedRoom('A', RoomStatus.UNKNOWN))
    assert not e.store.apply_observation(old, ResolvedRoom('A', RoomStatus.LIVE))


@pytest.mark.parametrize('status,code', [(RoomStatus.ENDED, 'target_not_live'), (RoomStatus.UNKNOWN, 'live_status_unknown')])
def test_manual_non_live_has_no_reservation(system, status, code):
    e = system
    target = e.store.add_target('A', auto_record=False)
    e.network.rooms['A'] = ResolvedRoom('A', status)
    key = command(e, 'record', target)
    assert result(e, key)['error_code'] == code
    assert row(e, 'SELECT record_session_key FROM targets WHERE id=?', target)['record_session_key'] is None
    observe(e, {'A': RoomStatus.LIVE})
    e.scheduler.tick()
    assert not e.children


def test_target_stop_session_recheck_and_reconcile_intent(system):
    e = system
    target, job = start_one(e)
    key = e.store.enqueue_command('stop_target', target, payload={'session_key': job['session_key']})
    observe_room(e.store, target, ResolvedRoom('A', RoomStatus.LIVE, room_id='2065', anchor_uid='900001'), e.clock() + 2)
    e.scheduler._commands()
    assert result(e, key)['error_code'] == 'session_changed'
    assert row(e, 'SELECT suppressed_session FROM targets WHERE id=?', target)['suppressed_session'] is None
    with e.store.transaction() as db:
        db.execute("UPDATE jobs SET status='needs_reconcile',session_key='room:2065' WHERE id=?", (job['id'],))
        db.execute("UPDATE targets SET record_session_key=session_key WHERE id=?", (target,))
    key = command(e, 'stop_target', target, {'session_key': 'room:2065'})
    assert json.loads(result(e, key)['result_json'])['reconcile_job_ids'] == [job['id']]
    assert e.children[0].alive


@pytest.mark.parametrize('version', [1, 2])
def test_old_schema_is_rejected_without_migration(tmp_path, version):
    path = tmp_path / 'old.sqlite3'
    db = sqlite3.connect(path)
    db.executescript(f'CREATE TABLE old(value); INSERT INTO old VALUES(7); PRAGMA user_version={version};')
    db.close()
    before = path.read_bytes()
    with pytest.raises(ValueError, match='拒绝覆盖'):
        Store(path)
    assert path.read_bytes() == before


def test_manual_capacity_and_shutdown_interrupts_unapplied(system):
    e = system
    queries = deferred(e)
    targets = [e.store.add_target(raw, auto_record=False) for raw in ['A', 'B', 'C']]
    keys = [e.store.enqueue_command('record', target) for target in targets]
    e.scheduler.tick()
    statuses = [result(e, key)['status'] for key in keys]
    assert statuses.count('running') == 2 and statuses.count('failed') == 1
    assert next(result(e, key) for key in keys if result(e, key)['status'] == 'failed')['error_code'] == 'query_busy'
    assert len(queries.calls) == 2
    e.scheduler.close()
    assert sum(result(e, key)['status'] == 'interrupted' for key in keys) == 2
    assert not e.store.rows('SELECT * FROM jobs')


def test_batch_transaction_rolls_back_all_items_and_result(system, monkeypatch):
    e = system
    real = e.store.add_target
    def injected(raw, *a, **kw):
        if raw == 'B':
            raise OSError('整批事务故障')
        return real(raw, *a, **kw)
    monkeypatch.setattr(e.store, 'add_target', injected)
    key = e.store.enqueue_command('add', '', payload=dict(targets=['A', 'B'], manual_only=True, allow_unconfirmed_identity=False))
    with pytest.raises(OSError):
        e.scheduler.tick()
    assert not e.store.rows('SELECT * FROM targets')
    assert result(e, key)['status'] == 'pending'


def test_instance_enable_command_unlocks_dispatch_and_rejects_race_busy(system):
    e = system
    with e.store.transaction() as db:
        db.execute("INSERT INTO instances(vmindex,name,status,enabled,updated_at) VALUES(3,'VM3','idle',0,?)", (e.clock(),))
    # 先用掉 1 和 2，让 3 保持未启用；再通过命令启用 3 观察第三个任务落到 3 上
    targets = [e.store.add_target(raw, now=e.clock()) for raw in ('A', 'B', 'C')]
    observe(e, dict.fromkeys(('A', 'B', 'C'), RoomStatus.LIVE))
    pump(e, lambda: len(e.children) == 2)
    pending_c = row(e, "SELECT * FROM jobs WHERE target_id=?", targets[2])
    assert pending_c['status'] == 'pending'

    enable_key = command(e, 'instance', '3', {'enabled': True})
    assert result(e, enable_key)['status'] == 'succeeded'
    pump(e, lambda: len(e.children) == 3)
    assert row(e, "SELECT vmindex FROM jobs WHERE id=?", pending_c['id'])['vmindex'] == 3

    # 并发竞态：提交停用命令时 3 刚释放为空闲，但调度执行命令前被新任务抢占 -> 命令失败返回 instance_busy
    e.children[2].exit(0, 'stop_requested')
    pump(e, lambda: row(e, 'SELECT status FROM instances WHERE vmindex=3')['status'] == 'idle')
    target_d = e.store.add_target('D', now=e.clock())
    observe(e, {'D': RoomStatus.LIVE})
    job_d = e.store.create_pending(target_d, e.clock())
    race_key = e.store.enqueue_command('instance', '3', payload={'enabled': False})
    assert e.store.claim(job_d, 3, e.clock())
    e.scheduler._commands()
    res = result(e, race_key)
    assert res['status'] == 'failed' and res['error_code'] == 'instance_busy'
    assert row(e, 'SELECT enabled FROM instances WHERE vmindex=3')['enabled'] == 1

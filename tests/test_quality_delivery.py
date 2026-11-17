"""质量事件的真实 SQLite 入账、迟到收尾与事务边界。"""
import json
import logging
import sqlite3

import pytest

from src.orchestrator.storage import Store


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / 'quality.sqlite3')
    target = db.add_target('质量测试', now=1)
    with db.transaction() as tx:
        tx.execute("INSERT INTO jobs(id,target_id,session_key,status,created_at) VALUES('job',?,'s','running',1)", (target,))
    yield db
    db.close()


def warning(event_id='risk', index=1, stamp=30):
    return dict(event='segment_quality_warning', event_id=event_id, segment_index=index,
                timestamp=stamp, flag='low_frame_rate', suspected_start_time=10)


def segment(store, state='finished', index=1):
    if state == 'missing':
        return
    if state not in ('placeholder', 'unstarted_terminal'):
        store.record_event('job', dict(event='segment_started', event_id=f's{index}',
                           segment_index=index, timestamp=10, file_path='raw.ts'), 10)
    else:
        with store.transaction() as tx:
            tx.execute('INSERT INTO segments(job_id,segment_index,created_at,updated_at) VALUES(?,?,10,10)', ('job', index))
    if state in ('finished', 'aborted', 'unstarted_terminal'):
        store.record_event('job', dict(event='segment_aborted' if state == 'aborted' else 'segment_finished',
                           event_id=f'e{index}', segment_index=index, timestamp=20, file_path='clip.mp4',
                           duration_sec=10, file_size_mb=1, is_final=True), 20)


def snapshot(store):
    return {table: store.rows(f'SELECT * FROM {table}') for table in ('events', 'segments', 'jobs', 'targets', 'instances')}


def test_delivery_identity_and_content_fallback(store):
    first = warning()
    store.record_event('job', first, 31)
    before = snapshot(store)
    store.record_event('job', dict(first, flag='changed', timestamp=900), 999)
    assert snapshot(store) == before
    store.record_event('job', dict(first, event_id='another'), 32)
    no_id = {k: v for k, v in first.items() if k != 'event_id'}
    store.record_event('job', no_id, 33)
    store.record_event('job', dict(reversed(list(no_id.items()))), 34)
    store.record_event('job', dict(no_id, timestamp=31), 35)
    risks = json.loads(store.row('SELECT risk_json FROM segments')['risk_json'])
    assert len(risks) == 4 and all(r['flag'] == 'low_frame_rate' for r in risks)
    with store.transaction() as tx:
        tx.execute("INSERT INTO jobs(id,target_id,session_key,status,created_at) SELECT 'other',target_id,'s','finished',1 FROM jobs WHERE id='job'")
    store.record_event('other', first, 36)
    assert len(store.rows('SELECT * FROM events')) == 5


@pytest.mark.parametrize('state', ['finished', 'finished_with_warnings', 'failed', 'cancelled', 'running', 'starting', 'stopping', 'pending', 'needs_reconcile'])
def test_normal_late_warning_only_promotes_success(store, state):
    segment(store)
    with store.transaction() as tx:
        tx.execute("UPDATE jobs SET status=?,exit_code=0,ended_at=25,collection_complete=1,error_reason='original'", (state,))
    before = snapshot(store)
    store.record_event('job', warning(), 40)
    after = snapshot(store)
    expected = dict(before['jobs'][0], status='finished_with_warnings' if state == 'finished' else state)
    assert after['jobs'] == [expected]
    for table in ('targets', 'instances'):
        assert after[table] == before[table]
    expected_segment = dict(before['segments'][0], updated_at=30, risk_json=after['segments'][0]['risk_json'])
    assert after['segments'] == [expected_segment]
    store.record_event('job', warning(), 900)
    assert snapshot(store) == after


@pytest.mark.parametrize('state,slice_state', [
    (state, slice_state)
    for state in ('finished', 'finished_with_warnings', 'failed', 'cancelled')
    for slice_state in ('missing', 'placeholder', 'started', 'unstarted_terminal', 'incomplete_job')
    if slice_state != 'incomplete_job' or state in ('finished', 'finished_with_warnings')
])
def test_incomplete_late_warning_preserves_evidence_and_other_task(store, caplog, state, slice_state):
    segment(store, 'finished' if slice_state == 'incomplete_job' else slice_state)
    with store.transaction() as tx:
        tx.execute("UPDATE jobs SET status=?,exit_code=?,ended_at=25,collection_complete=?,error_reason='original',vmindex=2",
                   (state, 9 if state == 'failed' else 0, int(slice_state != 'incomplete_job')))
        tx.execute("INSERT INTO jobs(id,target_id,session_key,status,created_at,vmindex) SELECT 'new',target_id,'new','running',26,2 FROM jobs WHERE id='job'")
        tx.execute("INSERT INTO instances(vmindex,status,job_id,updated_at) VALUES(2,'busy','new',26)")
    before = snapshot(store)
    with caplog.at_level(logging.WARNING):
        store.record_event('job', warning(), 40)
    job = store.row("SELECT * FROM jobs WHERE id='job'")
    assert job['collection_complete'] == 0
    if state in ('finished', 'finished_with_warnings'):
        assert job['status'] == 'failed' and job['exit_code'] == 30
        assert job['error_reason'].startswith('event_collection_incomplete:')
        assert 'original' in job['error_reason'] and '1' in job['error_reason']
    else:
        assert (job['status'], job['exit_code'], job['error_reason']) == (state, 9 if state == 'failed' else 0, 'original')
    assert job['ended_at'] == 25 and job['stop_reason'] == before['jobs'][0]['stop_reason']
    after = snapshot(store)
    assert after['targets'] == before['targets'] and after['instances'] == before['instances']
    assert after['jobs'][1] == before['jobs'][1]
    assert len(json.loads(after['segments'][0]['risk_json'])) == 1
    assert len(caplog.records) == 1 and 'risk' in caplog.text and 'job' in caplog.text
    store.record_event('job', warning(), 100)
    assert snapshot(store) == after and len(caplog.records) == 1
    if slice_state == 'missing':
        segment(store)
        store.record_event('job', warning(), 101)
        assert store.row("SELECT status FROM jobs WHERE id='job'")['status'] == job['status']


@pytest.mark.parametrize('incomplete', [False, True])
def test_quality_transaction_rolls_back_and_retry_is_possible(store, caplog, incomplete):
    if not incomplete:
        segment(store)
    with store.transaction() as tx:
        tx.execute("UPDATE jobs SET status='finished',exit_code=0,collection_complete=1,ended_at=25")
        tx.execute("CREATE TRIGGER fail_summary BEFORE UPDATE ON jobs BEGIN SELECT RAISE(ABORT, 'injected'); END")
    before = snapshot(store)
    with pytest.raises(sqlite3.IntegrityError, match='injected'):
        store.record_event('job', warning(), 40)
    assert snapshot(store) == before and not caplog.records
    with store.transaction() as tx:
        tx.execute('DROP TRIGGER fail_summary')
    store.record_event('job', warning(), 41)
    assert store.row("SELECT status FROM jobs WHERE id='job'")['status'] == ('failed' if incomplete else 'finished_with_warnings')


def test_conflicting_terminal_event_rolls_back_time_and_event(store):
    segment(store)
    before = snapshot(store)
    with pytest.raises(ValueError, match='冲突终态'):
        store.record_event('job', dict(event='segment_aborted', event_id='conflict', segment_index=1,
                           timestamp=100, file_path='clip.mp4'), 100)
    assert snapshot(store) == before


def test_warning_before_start_still_finishes_with_warnings(store):
    store.record_event('job', warning(), 40)
    segment(store)
    store.finish_job('job', 0, 50, True)
    job = store.row('SELECT * FROM jobs')
    assert job['status'] == 'finished_with_warnings' and job['collection_complete'] == 1

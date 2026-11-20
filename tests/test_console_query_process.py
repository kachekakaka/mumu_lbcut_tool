"""真实 Windows 子进程、独立期限、容量隔离与回收验收。"""
from pathlib import Path
import sys
import threading
import time

import psutil
import pytest
from src.orchestrator.room_query_process import QueryProcesses

CHILD = Path(__file__).parent / 'fixtures' / 'console_query_child.py'


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    assert predicate()


def test_process_reuse_capacity_isolation_and_shared_shutdown():
    queries = QueryProcesses(timeout=30, child_command=[sys.executable, str(CHILD)])
    pids = []
    try:
        first = queries.submit('manual', 'ok').result(5)
        wait_for(lambda: all(s.future is None for s in queries.slots))
        second = queries.submit('manual', 'ok').result(5)
        pid1, seq1 = first.room.room_id.split(':')
        pid2, seq2 = second.room.room_id.split(':')
        assert pid1 == pid2 and (seq1, seq2) == ('1', '2')
        wait_for(lambda: all(s.future is None for s in queries.slots))
        futures = [queries.submit('manual', 'block_dns') for _ in range(2)]
        assert queries.submit('manual', 'ok') is None
        futures += [queries.submit('scan', 'block_response') for _ in range(5)]
        assert queries.submit('scan', 'ok') is None
        futures += [queries.submit('verify', 'block_lock')]
        assert queries.submit('verify', 'ok') is None
        # 扫描、启动和手动槽位全满时，登录复核仍能独立派发。
        futures += [queries.submit('login', 'block_response')]
        assert queries.submit('login', 'ok') is None
        wait_for(lambda: len([s for s in queries.slots if s.proc]) == 9)
        pids = [s.proc.pid for s in queries.slots]
        started = time.monotonic()
        queries.close()
        assert time.monotonic() - started < 5.5
        assert all(f.done() for f in futures)
        assert not any(psutil.pid_exists(pid) for pid in pids)
        assert not any(t.name == 'room-query-ipc' for t in threading.enumerate())
    finally:
        if not queries.closing.is_set():
            queries.close()


@pytest.mark.parametrize('phase', ['dns', 'partial'], ids=['无响应', '半条响应'])
def test_blocking_phase_deadline_reclaims_before_reuse(phase):
    queries = QueryProcesses(timeout=.7, child_command=[sys.executable, str(CHILD)])
    try:
        started = time.monotonic()
        future = queries.submit('manual', 'block_' + phase)
        wait_for(lambda: any(s.proc for s in queries.slots))
        pid = next(s.proc.pid for s in queries.slots if s.proc)
        result = future.result(6)
        assert result.error == 'live_query_timeout'
        assert time.monotonic() - started < 5.7
        wait_for(lambda: all(s.future is None for s in queries.slots))
        assert not psutil.pid_exists(pid)
        assert queries.submit('manual', 'ok').result(5).error is None
        assert not queries.fatal_error
    finally:
        queries.close()


@pytest.mark.parametrize('raw', ['oversize', 'exit'])
def test_bad_ipc_discards_channel(raw):
    queries = QueryProcesses(child_command=[sys.executable, str(CHILD)])
    try:
        assert queries.submit('manual', raw).result(5).error == 'live_query_failed'
        wait_for(lambda: all(s.future is None for s in queries.slots))
        assert queries.submit('manual', 'ok').result(5).error is None
    finally:
        queries.close()


def test_repeated_timeouts_keep_process_and_thread_count_bounded():
    queries = QueryProcesses(timeout=.4, child_command=[sys.executable, str(CHILD)])
    try:
        for _ in range(5):
            future = queries.submit('manual', 'block_dns')
            assert future.result(6).error == 'live_query_timeout'
            wait_for(lambda: all(s.future is None for s in queries.slots))
            assert not any(s.proc for s in queries.slots)
            assert not any(t.name == 'room-query-ipc' for t in threading.enumerate())
    finally:
        queries.close()


def test_reclaim_failure_reports_fatal_without_returning_slot():
    queries = QueryProcesses(timeout=.3, reclaim_timeout=.2, child_command=[sys.executable, str(CHILD)])
    kill = queries._kill
    queries._kill = lambda slot: None
    try:
        future = queries.submit('verify', 'block_dns')
        wait_for(lambda: queries.fatal_error, 3)
        assert not future.done()
        assert not queries.available('verify')
    finally:
        queries._kill = kill
        queries.reclaim_timeout = 5
        with pytest.raises(RuntimeError, match='回收预算'):
            queries.close()


def test_close_waits_for_active_ipc_to_return_its_process(monkeypatch):
    queries = QueryProcesses(reclaim_timeout=.3, child_command=[sys.executable, str(CHILD)])
    future = queries.submit('verify', 'block_dns')
    wait_for(lambda: any(s.proc for s in queries.slots))
    original = queries._reap
    entered, release = threading.Event(), threading.Event()
    captured = []
    slot = next(s for s in queries.slots if s.proc)
    pid = slot.proc.pid
    def delayed(proc, deadline):
        entered.set()
        assert release.wait(3)
        original(proc, deadline)
    monkeypatch.setattr(queries, '_reap', delayed)
    def close():
        try:
            queries.close()
        except Exception as exc:
            captured.append(exc)
    closer = threading.Thread(target=close)
    closer.start()
    try:
        assert entered.wait(2)
        closer.join(2)
        assert not closer.is_alive()
        assert len(captured) == 1 and 'IPC' in str(captured[0])
        assert future.result(1).error == 'service_interrupted'
        assert queries.job.handle is None
        queries.close()
    finally:
        release.set()
        slot.thread.join(2)
    assert slot.proc is None and not psutil.pid_exists(pid)

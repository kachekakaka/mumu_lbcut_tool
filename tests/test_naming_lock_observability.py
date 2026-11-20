"""用两个同时存活的进程验证实例锁。"""
from pathlib import Path
import subprocess
import sys
import time


CONTENDER = r'''
from pathlib import Path
import sys, time
from src.worker.naming_lock import WorkerInstanceLock, LockAcquisitionError
root, label = Path(sys.argv[1]), sys.argv[2]
lock = WorkerInstanceLock(root / 'locks', 2, label)
def await_file(path):
    until = time.monotonic() + 10
    while not path.exists():
        if time.monotonic() >= until:
            raise TimeoutError(path)
        time.sleep(.01)
(root / ('ready-' + label)).touch()
await_file(root / 'contend')
try:
    lock.acquire()
except LockAcquisitionError:
    (root / ('blocked-' + label)).touch()
    await_file(root / 'first-released')
    lock.acquire()
    (root / ('acquired-after-release-' + label)).touch()
    lock.release()
else:
    (root / ('held-' + label)).touch()
    await_file(root / 'release')
    lock.release()
    (root / 'first-released').touch()
'''


def test_instance_lock_across_two_processes(tmp_path):
    children = [subprocess.Popen([sys.executable, '-c', CONTENDER, str(tmp_path), label])
                for label in ('A', 'B')]
    def wait_for(pattern, count):
        until = time.monotonic() + 8
        while time.monotonic() < until:
            matches = list(tmp_path.glob(pattern))
            if len(matches) >= count:
                return matches
            time.sleep(.02)
        raise AssertionError(pattern)

    try:
        wait_for('ready-*', 2)
        (tmp_path / 'contend').touch()
        held, = wait_for('held-*', 1)
        blocked, = wait_for('blocked-*', 1)
        assert held.name[-1] != blocked.name[-1]
        assert not list(tmp_path.glob('acquired-after-release-*'))
        (tmp_path / 'release').touch()
        assert [proc.wait(5) for proc in children] == [0, 0]
        assert (tmp_path / ('acquired-after-release-' + blocked.name[-1])).is_file()
    finally:
        for proc in children:
            if proc.poll() is None:
                proc.kill()
                proc.wait(5)

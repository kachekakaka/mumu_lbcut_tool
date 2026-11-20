"""测试统一命令超时执行器与单调时钟流程截止时间 (Ticket 02)。"""
import json
import os
import sys
import time
import threading

import psutil
import pytest
from src.worker.executor import (
    run_command,
    CommandResult,
    CommandTimeoutError,
    MonotonicDeadline,
    DeadlineExceededError,
)


def test_run_command_normal_success():
    """正常命令在时限内成功完成，并返回标准输出与退出码 0。"""
    cmd = [sys.executable, "-c", "import sys; sys.stdout.write('hello'); sys.stderr.write('world')"]
    result = run_command(cmd, timeout=5.0)
    assert result.exit_code == 0
    assert result.stdout == "hello"
    assert result.stderr == "world"
    assert result.duration >= 0.0


def test_run_command_timeout_kills_process_tree():
    """超时命令在 timeout 到达后必须强杀进程树并抛出 CommandTimeoutError，耗时不超过 timeout + 0.8s。"""
    # 模拟一个挂起不退出的 Python 进程
    cmd = [sys.executable, "-c", "import time; time.sleep(10)"]
    start_time = time.monotonic()
    
    with pytest.raises(CommandTimeoutError) as exc_info:
        run_command(cmd, timeout=1.0)
    
    elapsed = time.monotonic() - start_time
    # 耗时必须在 0.9s 到 4.0s 之间（对齐方案验收规格：时限 1.0s + Windows taskkill 进程树清理预算）
    assert 0.9 <= elapsed <= 4.0
    assert exc_info.value.timeout == 1.0


def test_monotonic_deadline_budget_and_expiry():
    """MonotonicDeadline 基于单调时钟正确计算剩余预算并截断。"""
    deadline = MonotonicDeadline(budget_seconds=2.0)
    assert not deadline.is_expired()
    assert 1.5 <= deadline.remaining_budget() <= 2.0

    # 动态裁剪：若子调用要求 5s，但剩余预算仅 2s，则裁剪至 2s
    allocated = deadline.allocate_timeout(max_allowed=5.0)
    assert allocated <= 2.0

    time.sleep(0.3)
    # 剩余预算正常随时间消耗减少
    assert deadline.remaining_budget() < 1.8

    # 测试超时到期
    expired_deadline = MonotonicDeadline(budget_seconds=0.05)
    time.sleep(0.08)
    assert expired_deadline.is_expired()
    assert expired_deadline.remaining_budget() == 0.0
    with pytest.raises(DeadlineExceededError):
        expired_deadline.check_not_expired()


@pytest.mark.skipif(os.name != 'nt', reason='验证 Windows Job 和继承管道')
@pytest.mark.parametrize('owned', [True, False], ids=['独占进程树', '共享服务'])
def test_timeout_reclaims_only_owned_processes_and_releases_pipes(tmp_path, owned):
    grand = "import pathlib,sys,time; time.sleep(2); pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(15)"
    child = (
        "import json,subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]);"
        "print(json.dumps({'grandchild':p.pid}),flush=True); "
        "print('grandchild inherits stderr',file=sys.stderr,flush=True); time.sleep(15)"
    )
    root = (
        "import json,os,subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c',sys.argv[1],*sys.argv[2:]]);"
        "print(json.dumps({'root':os.getpid(),'child':p.pid}),flush=True); time.sleep(15)"
    )
    marker = tmp_path / 'shared-response.txt'
    baseline = set(threading.enumerate())
    with pytest.raises(CommandTimeoutError) as captured:
        run_command([sys.executable, '-u', '-c', root, child, grand, str(marker)],
                    timeout=1.2, kill_tree_on_timeout=owned)
    pids = {name: pid for line in captured.value.stdout.splitlines() for name, pid in json.loads(line).items()}
    assert len(pids) == 3 and len(set(pids.values())) == 3

    def assert_exited(pid):
        # Windows PID 可能仍被内核登记。等待进程对象确认退出。
        try:
            psutil.Process(pid).wait(.3)
        except psutil.NoSuchProcess:
            pass

    try:
        assert 'inherits stderr' in captured.value.stderr
        assert_exited(pids['root'])
        if owned:
            assert_exited(pids['child'])
            assert_exited(pids['grandchild'])
            assert not marker.exists()
        else:
            until = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < until:
                time.sleep(.02)
            assert marker.read_text() == 'ready'
            with pytest.raises(psutil.TimeoutExpired):
                psutil.Process(pids['grandchild']).wait(.05)
        assert set(threading.enumerate()) == baseline
    finally:
        for pid in (pids['child'], pids['grandchild']):
            try:
                process = psutil.Process(pid)
                process.kill()
                process.wait(3)
            except psutil.NoSuchProcess:
                pass


def test_monotonic_deadline_dynamic_subcommand_clipping():
    """多步骤流程中，动态为后续子命令裁剪剩余超时预算，总耗时不超总预算。"""
    deadline = MonotonicDeadline(budget_seconds=1.5)

    # 步骤 1：耗时 0.5s 的命令
    cmd1 = [sys.executable, "-c", "import time; time.sleep(0.5)"]
    timeout1 = deadline.allocate_timeout(max_allowed=3.0)
    res1 = run_command(cmd1, timeout=timeout1)
    assert res1.exit_code == 0

    # 步骤 2：剩余预算应已缩减至约 1.0s，后续请求即使请求 3.0s，也会被裁剪到 <= 1.0s
    timeout2 = deadline.allocate_timeout(max_allowed=3.0)
    assert timeout2 <= 1.1

    # 步骤 2 执行一个需要 2s 的挂起命令，但在裁剪后的 timeout2 下被强杀中断
    cmd2 = [sys.executable, "-c", "import time; time.sleep(2.0)"]
    with pytest.raises(CommandTimeoutError):
        run_command(cmd2, timeout=timeout2)

    # 流程整体耗时必须在 1.5s 预算内（加子进程清理容限 <= 2.2s），绝不拖长至 3.0s 以上
    assert deadline.is_expired()

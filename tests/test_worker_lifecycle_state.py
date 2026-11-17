"""真实 Controller 退出链路必须经过 STOPPING；仅替换设备和采集边界。"""
import pytest

from src.worker.naming_lock import WorkerInstanceLock
from src.worker.state_machine import WorkerState
from worker_harness import build_worker, assert_paired


@pytest.mark.parametrize('scenario,code,states', [
    ('completed', 0, ['PREFLIGHT', 'RECORDING', 'STOPPING', 'STOPPED']),
    ('manual_stop', 0, ['PREFLIGHT', 'RECORDING', 'STOPPING', 'STOPPED']),
    ('cancel_before_run', 0, ['PREFLIGHT', 'STOPPING', 'STOPPED']),
    ('invalid_target', 9, ['PREFLIGHT', 'STOPPING', 'STOPPED']),
    ('lock_busy', 20, ['PREFLIGHT', 'STOPPING', 'STOPPED']),
    ('preflight_failure', 16, ['PREFLIGHT', 'STOPPING', 'STOPPED']),
    ('recovery_failure', 16, ['PREFLIGHT', 'RECORDING', 'RECOVERING', 'STOPPING', 'STOPPED']),
    # 旧片已因恢复中止，取消后没有新成功片；沿用现有尾片失败退出码。
    ('stop_in_recovery', 30, ['PREFLIGHT', 'RECORDING', 'RECOVERING', 'STOPPING', 'STOPPED']),
    ('cleanup_error', 30, ['PREFLIGHT', 'RECORDING', 'STOPPING', 'STOPPED']),
])
def test_worker_exit_obeys_state_order(monkeypatch, tmp_path, valid_media, scenario, code, states):
    recovering = scenario in {'recovery_failure', 'stop_in_recovery'}
    h = build_worker(monkeypatch, tmp_path, valid_media, total=3 if recovering else 1,
                     gate=scenario != 'preflight_failure')
    c = h.controller
    observed = [c.state_machine.state.value]
    terminal_snapshots = []
    transition = c.state_machine.transition_to

    def trace(target):
        before = c.state_machine.state
        transition(target)  # 保留真实校验；不让观察器代替状态机执行迁移。
        if before != c.state_machine.state:
            observed.append(c.state_machine.state.value)
            if target == WorkerState.STOPPED:
                terminal_snapshots.append((all(p.stopped and not p.alive for p in h.pipelines),
                                           [e['event'] for e in h.events()]))
    monkeypatch.setattr(c.state_machine, 'transition_to', trace)
    if recovering:
        from src.worker.state_machine import PipelineAction, ActionType
        def request_relocation(now):
            if h.pipelines:
                c.action_dispatcher.post_action(PipelineAction(
                    action_type=ActionType.IN_PLACE_HEAL, generation_id=c.current_generation_id,
                    reason='test_relocate'))
                h.clock.on_sleep = None
        h.clock.on_sleep = request_relocation
    owner = None
    if scenario == 'cancel_before_run':
        c.request_stop('cancel_before_run')
    elif scenario == 'invalid_target':
        c.config.target = ''
    elif scenario == 'lock_busy':
        owner = WorkerInstanceLock(c.config.lock_dir, c.config.vmindex)
        owner.acquire()
    elif scenario == 'manual_stop':
        h.clock.on_sleep = lambda now: c.request_stop('manual_stop') if h.pipelines else None
    elif scenario == 'recovery_failure':
        monkeypatch.setattr(h.navigator_type, 'wait_live_page', lambda self, **kw: not h.pipelines)
    elif scenario == 'stop_in_recovery':
        navigate = h.navigator_type.navigate_route
        def stop_when_relocating(self, *args, **kwargs):
            if h.pipelines:
                c.request_stop('stop_in_recovery')
            return navigate(self, *args, **kwargs)
        monkeypatch.setattr(h.navigator_type, 'navigate_route', stop_when_relocating)
    elif scenario == 'cleanup_error':
        teardown = h.pipeline_type.teardown
        def fail_after_stop(self):
            teardown(self)
            raise RuntimeError('injected_cleanup_error')
        monkeypatch.setattr(h.pipeline_type, 'teardown', fail_after_stop)
    try:
        assert c.run() == code
    finally:
        if owner:
            owner.release()
    assert observed == states
    assert len(terminal_snapshots) == 1
    stopped, events_at_stop = terminal_snapshots[0]
    assert stopped
    assert 'worker_stopped' not in events_at_stop
    assert_paired(h.events())
    assert h.events()[-1]['event'] == 'worker_stopped'
    assert h.events()[-1]['exit_code'] == code
    if scenario == 'stop_in_recovery':
        assert c._last_segment_failed
        assert h.events()[-1]['had_segment_errors']
    lock = WorkerInstanceLock(c.config.lock_dir, c.config.vmindex)
    lock.acquire()
    lock.release()
    # 重复清理、重复停止请求不能重开资源或让终态逆迁。
    c._cleanup(None)
    c.request_stop()
    assert observed == states
    assert c.state_machine.state == WorkerState.STOPPED

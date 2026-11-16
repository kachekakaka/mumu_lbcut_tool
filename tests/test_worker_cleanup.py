"""从 Worker 生命周期验证应用清理；设备命令只在外部执行边界替换。"""
import io
from types import SimpleNamespace

import pytest

from src.worker.controller import WorkerController
from src.worker.events import WorkerEventEmitter
from src.worker.executor import CommandResult, CommandTimeoutError
from src.worker.naming_lock import WorkerInstanceLock, LockAcquisitionError
from worker_harness import build_worker, PassiveWatchdog, assert_paired


def assert_lock_released(h):
    lock = WorkerInstanceLock(h.controller.config.lock_dir, h.controller.config.vmindex)
    lock.acquire()
    lock.release()


@pytest.mark.parametrize('options,code', [
    ({}, 0),
    ({'gate': False, 'gate_reason': 'login_blocked'}, 15),
    ({'gate': False, 'gate_reason': 'render_timeout'}, 16),
    ({'gate': False, 'gate_reason': 'navigation_failed'}, 17),
    ({'uid': 'other-anchor'}, 18),
    ({'broken_segments': (1,)}, 30),
])
def test_cleanup_after_sealing_and_before_unlock(monkeypatch, tmp_path, valid_media, options, code):
    from src.worker import adb_client
    h = build_worker(monkeypatch, tmp_path, valid_media, total=1, **options)
    original = adb_client.run_command
    def run(cmd, **kwargs):
        contender = WorkerInstanceLock(h.controller.config.lock_dir, h.controller.config.vmindex)
        with pytest.raises(LockAcquisitionError):
            contender.acquire()
        return original(cmd, **kwargs)
    monkeypatch.setattr(adb_client, 'run_command', run)
    assert h.controller.run() == code
    assert len(h.adb_calls) == 1
    call = h.adb_calls[0]
    assert call['cmd'] == [str(h.adb_exe), '-s', 'synthetic-device', 'shell', 'am',
                           'force-stop', 'com.ss.android.ugc.aweme']
    assert call['options']['timeout'] == 4
    assert call['pipelines_stopped']
    assert not any(e['event'] == 'worker_stopped' for e in call['events'])
    assert_paired(call['events'])
    assert h.events()[-1]['exit_code'] == code
    assert_lock_released(h)


@pytest.mark.parametrize('reason', ['manual_stop', 'anchor_offline', 'scheduler_shutdown', 'event_stream_lost'])
def test_requested_stop_closes_app_after_tail(monkeypatch, tmp_path, valid_media, reason):
    h = build_worker(monkeypatch, tmp_path, valid_media, total=6)
    h.clock.on_sleep = lambda now: h.controller.request_stop(reason) if any(p.alive for p in h.pipelines) else None
    assert h.controller.run() == 0
    assert len(h.adb_calls) == 1 and h.adb_calls[0]['pipelines_stopped']
    assert all(e['event'] == 'segment_finished' for e in assert_paired(h.adb_calls[0]['events']))
    assert h.events()[-1]['reason'] == reason
    assert_lock_released(h)


@pytest.mark.parametrize('code', [0, 15])
@pytest.mark.parametrize('failure', ['exit', 'timeout', 'exception'])
def test_cleanup_failure_keeps_original_terminal(monkeypatch, tmp_path, valid_media, caplog, code, failure):
    h = build_worker(monkeypatch, tmp_path, valid_media, total=1, gate=code == 0, gate_reason='login_blocked')
    def run(cmd, **kwargs):
        if failure == 'timeout':
            raise CommandTimeoutError(cmd, kwargs['timeout'], 'raw-output', 'raw-error')
        if failure == 'exception':
            raise OSError('raw-error')
        return CommandResult(7, 'raw-output', 'raw-error', 0)
    monkeypatch.setattr('src.worker.adb_client.run_command', run)
    assert h.controller.run() == code
    assert h.events()[-1]['exit_code'] == code
    assert h.events()[-1]['reason'] == ('completed' if code == 0 else 'login_blocked')
    if code == 0:
        assert all(e['event'] == 'segment_finished' for e in assert_paired(h.events()))
    assert '实例 321 关闭抖音失败，serial=synthetic-device' in caplog.text
    assert 'raw-error' in caplog.text
    if failure != 'exception':
        assert 'raw-output' in caplog.text
    assert_lock_released(h)


@pytest.mark.parametrize('stage', ['watchdog', 'pipeline'])
def test_cleanup_resource_error_still_closes_app_and_unlocks(monkeypatch, tmp_path, valid_media, stage):
    class FailingWatchdog(PassiveWatchdog):
        def stop(self):
            raise RuntimeError('original-watchdog-error')
    h = build_worker(monkeypatch, tmp_path, valid_media, total=1,
                     watchdog_factory=FailingWatchdog if stage == 'watchdog' else PassiveWatchdog)
    if stage == 'pipeline':
        def inject(now):
            if not h.pipelines:
                return
            pipe = h.pipelines[0]
            def teardown():
                pipe.stop()
                raise RuntimeError('original-pipeline-error')
            pipe.teardown = teardown
        h.clock.on_sleep = inject
    assert h.controller.run() == 30
    assert len(h.adb_calls) == 1 and h.adb_calls[0]['pipelines_stopped']
    assert f'original-{stage}-error' in h.events()[-1]['reason']
    assert_lock_released(h)


@pytest.mark.parametrize('unavailable', ['launch', 'adb', 'lock'])
def test_no_cleanup_before_lock_and_connection(monkeypatch, tmp_path, valid_media, unavailable):
    h = build_worker(monkeypatch, tmp_path, valid_media)
    monkeypatch.setattr('src.worker.controller.MuMuClient', lambda **kw: SimpleNamespace(
        launch_player=lambda index: unavailable != 'launch', connect_adb=lambda index: None))
    owner = WorkerInstanceLock(h.controller.config.lock_dir, h.controller.config.vmindex)
    if unavailable == 'lock':
        owner.acquire()
    try:
        assert h.controller.run() == 20
        assert not h.adb_calls
    finally:
        owner.release()
    assert_lock_released(h)


def test_same_instance_can_record_again_after_cleanup(monkeypatch, tmp_path, valid_media):
    h = build_worker(monkeypatch, tmp_path, valid_media, total=1)
    assert h.controller.run() == 0
    second = WorkerController(h.controller.config, WorkerEventEmitter(io.StringIO()))
    assert second.run() == 0
    assert second.task_output_dir != h.controller.task_output_dir
    assert len(h.adb_calls) == 2
    assert_lock_released(h)


def test_desktop_navigation_starts_home_before_room_intent():
    import time
    from src.worker.navigator import WorkerNavigator, DOUYIN_PACKAGE
    nav = WorkerNavigator.__new__(WorkerNavigator)
    nav._operation_deadline = None
    nav.get_current_focus_activity = lambda: 'launcher.HomeActivity'
    nav.get_current_focus_package = lambda: 'launcher'
    calls = []
    nav._adb = lambda *args: calls.append(args) or 'Starting'
    nav._navigation_sleep = lambda *args: True
    nav._send_room_intent = lambda *args: calls.append(('room_intent', *args)) or True
    assert nav.navigate_route('123', None, 'primary', time.monotonic() + 10, lambda: False)
    assert calls == [('shell', 'am', 'start', '-n', f'{DOUYIN_PACKAGE}/.main.MainActivity'),
                     ('room_intent', '123', None)]

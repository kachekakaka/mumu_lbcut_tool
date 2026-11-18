"""T14：使用合成文件、设备替身和专属子进程验证操作边界。"""
import importlib
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import _common
from scripts.verify_worker_e2e_live import assess, ready_to_stop
from src.mvp.mumu_client import MuMuClient


ENTRIES = ['launch_vm','dump_screen','save_screenshot','parse_ui','inspect_events',
           'inspect_recording','scan_samples','verify_worker_e2e_live']


@pytest.mark.parametrize('name', ENTRIES)
def test_import_help_and_missing_parameters_have_no_commands(monkeypatch, name):
    forbidden = Mock(side_effect=AssertionError('不应执行命令'))
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    import requests
    monkeypatch.setattr(requests.sessions.Session, 'request', forbidden)
    module = importlib.reload(importlib.import_module('scripts.'+name))
    with pytest.raises(SystemExit) as help_exit:
        module.main(['--help'])
    assert help_exit.value.code == 0
    with pytest.raises(SystemExit) as missing:
        module.main([])
    assert missing.value.code == 2
    forbidden.assert_not_called()


@pytest.mark.parametrize('name,extra', [
    ('launch_vm', []), ('dump_screen', ['--output-dir']), ('save_screenshot', ['--output-dir']),
    ('verify_worker_e2e_live', ['--target','cctvnews','--output-dir'])])
def test_device_preview_never_creates_output_or_calls_manager(monkeypatch, tmp_path, name, extra):
    module = importlib.import_module('scripts.'+name)
    path = tmp_path/'not-created'
    args = ['--vmindex','2'] + extra + ([str(path)] if extra else [])
    forbidden = Mock(side_effect=AssertionError('预览不能操作设备'))
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    assert module.main(args) == 0
    assert not path.exists()
    forbidden.assert_not_called()


def test_reject_output_in_repository_and_preserve_existing(tmp_path):
    with pytest.raises(ValueError):
        _common.output_parent(_common.ROOT/'output')
    keep = tmp_path/'old.mp4'
    keep.write_bytes(b'old')
    a = _common.new_batch(tmp_path, 'probe')
    b = _common.new_batch(tmp_path, 'probe')
    assert a != b and keep.read_bytes() == b'old'
    _common.write_json(a/'report.json', {'a':1})
    with pytest.raises(FileExistsError):
        _common.write_json(a/'report.json', {'a':2})


def test_offline_inputs_and_partial_events(tmp_path, capsys):
    from scripts.parse_ui import main
    from scripts.inspect_events import main as events_main
    xml = tmp_path/'ui.xml'
    xml.write_text('<hierarchy><node text="关闭" bounds="[0,0][10,20]" /></hierarchy>', encoding='utf8')
    assert main(['--input',str(xml),'--filter','top']) is None
    assert '关闭' in capsys.readouterr().out
    xml.write_text('<broken>', encoding='utf8')
    with pytest.raises(Exception):
        main(['--input',str(xml)])
    events = tmp_path/'events'
    events.write_text('{"event":"worker_started"}\ninvalid\n', encoding='utf-16')
    assert events_main(['--input',str(events)]) == 4
    assert '"line": 2' in capsys.readouterr().out


def test_scan_validates_all_before_network(tmp_path, monkeypatch):
    from scripts.scan_samples import main
    file = tmp_path/'samples.json'
    file.write_text(json.dumps([{'id':1,'entry':'abc'}, {'id':2,'entry':'https://live.douyin.com/abc'}]))
    import requests
    forbidden = Mock(side_effect=AssertionError('输入非法不能联网'))
    monkeypatch.setattr(requests.sessions.Session, 'request', forbidden)
    with pytest.raises(ValueError, match='重复'):
        main(['--input',str(file),'--execute'])
    forbidden.assert_not_called()


def test_strict_mapping_rejects_formula_fallback_and_duplicate(monkeypatch):
    client = MuMuClient.__new__(MuMuClient)
    snapshot = {'2': {'index':2,'is_android_started':True,'adb_host_ip':'127.0.0.1'}}
    monkeypatch.setattr(client, 'get_all_players', lambda: snapshot)
    with pytest.raises(ValueError):
        client.confirmed_serial(2)
    snapshot['2']['adb_port'] = 16448
    assert client.confirmed_serial(2) == '127.0.0.1:16448'
    snapshot['3'] = dict(snapshot['2'], index=3)
    with pytest.raises(ValueError, match='多个实例'):
        client.confirmed_serial(2)
    del snapshot['3']
    snapshot['2']['adb_port'] = 16449
    with pytest.raises(ValueError, match='变化'):
        client.verify_serial(2, '127.0.0.1:16448')


def test_connect_is_bounded_pinned_and_cancellable(monkeypatch):
    client = MuMuClient.__new__(MuMuClient)
    client.adb_exe = Path('fake-adb.exe')
    calls = []
    def run(cmd, **kw):
        calls.append((cmd, kw['timeout']))
        return subprocess.CompletedProcess(cmd, 0, 'List of devices\nwrong-address device\n', '')
    monkeypatch.setattr('src.mvp.mumu_client.run_client', run)
    stop = Mock()
    stop.is_set.return_value = False
    stop.wait.return_value = False
    assert client.connect_adb(2, serial='confirmed', stop_event=stop) is None
    assert len(calls) == 10 and all(0 < timeout <= 5 for _,timeout in calls)
    assert all(cmd[-1] == 'confirmed' for cmd,_ in calls if cmd[1] == 'connect')
    stop.is_set.return_value = True
    with pytest.raises(InterruptedError):
        client.connect_adb(2, serial='confirmed', stop_event=stop)
    assert len(calls) == 10


def test_hung_client_is_killed_without_tree_cleanup(tmp_path):
    from src.mvp.command import run_client
    with pytest.raises(subprocess.TimeoutExpired):
        run_client([sys.executable,'-c','import time; time.sleep(60)'], timeout=.2, cwd=tmp_path)


def test_inherited_pipe_does_not_block_timeout_cleanup(tmp_path):
    import time
    from src.mvp.command import run_client
    code = 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c","import time;time.sleep(3)"]); time.sleep(60)'
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_client([sys.executable,'-c',code], timeout=.2, cwd=tmp_path)
    assert time.monotonic()-started < 2.5  # 不等待三秒后自己退出的专属后代，也不强杀它。


def test_local_tools_never_download(tmp_path, monkeypatch):
    from src.mvp.dep_manager import DependencyManager
    dep = DependencyManager(scrcpy_dir=tmp_path)
    forbidden = Mock(side_effect=AssertionError('不能下载'))
    monkeypatch.setattr(dep, '_get_latest_win64_url', forbidden)
    (tmp_path/'scrcpy.exe').write_bytes(b'fake')
    with pytest.raises(FileNotFoundError, match='scrcpy-server'):
        dep.require_scrcpy()
    forbidden.assert_not_called()


def test_cold_manager_boot_requires_installed_instance_then_live_confirmation(tmp_path, monkeypatch):
    client = MuMuClient.__new__(MuMuClient)
    client.manager_exe = tmp_path/'nx_main'/'MuMuManager.exe'
    installed = tmp_path/'vms'/'MuMuPlayer-15.0-2'/'configs'
    installed.mkdir(parents=True)
    (installed/'vm_config.json').write_text('{}')
    snapshot = {'2': {'index':2,'is_android_started':False}}
    query = Mock(side_effect=[subprocess.TimeoutExpired('info',5), snapshot])
    monkeypatch.setattr(client, 'get_all_players', query)
    boot = Mock(return_value=True)
    monkeypatch.setattr(client, 'ensure_main_process_running', boot)
    assert client.require_instance(2, prepare_main=True) == snapshot['2']
    assert boot.call_count == 1
    query.side_effect = subprocess.TimeoutExpired('info',5)
    with pytest.raises(ValueError, match='配置'):
        client.require_instance(3, prepare_main=True)
    assert boot.call_count == 1
    with pytest.raises(subprocess.TimeoutExpired):
        client.require_instance(2)  # 截图/UI 不得拉起主程序。
    assert boot.call_count == 1


def test_device_lock_busy_does_not_touch_instance_or_output(monkeypatch, tmp_path):
    from scripts._device import device_main
    from src.worker.naming_lock import WorkerInstanceLock, LockAcquisitionError
    manager = Mock()
    monkeypatch.setattr('src.mvp.mumu_client.MuMuClient', lambda: manager)
    owner = WorkerInstanceLock(tmp_path/'locks', 2)
    monkeypatch.setattr('src.worker.naming_lock.WorkerInstanceLock', lambda **kw: WorkerInstanceLock(tmp_path/'locks', kw['vmindex']))
    owner.acquire()
    try:
        with pytest.raises(LockAcquisitionError):
            device_main('screenshot', ['--vmindex','2','--output-dir',str(tmp_path/'out'),'--execute'])
        manager.require_instance.assert_not_called()
        assert not (tmp_path/'out').exists()
    finally:
        owner.release()


def test_pinned_client_timeout_releases_tool_lock(monkeypatch, tmp_path):
    from scripts._device import device_main
    from src.worker.naming_lock import WorkerInstanceLock
    manager = Mock(adb_exe=tmp_path/'adb.exe')
    manager.require_instance.return_value = {'is_android_started':True}
    manager.confirmed_serial.return_value = 'confirmed'
    manager.connect_adb.return_value = 'confirmed'
    monkeypatch.setattr('src.mvp.mumu_client.MuMuClient', lambda: manager)
    monkeypatch.setattr('src.worker.naming_lock.WorkerInstanceLock', lambda **kw: WorkerInstanceLock(tmp_path/'locks', kw['vmindex']))
    monkeypatch.setattr('src.mvp.command.run_client', Mock(side_effect=subprocess.TimeoutExpired('screenshot',10)))
    with pytest.raises(subprocess.TimeoutExpired):
        device_main('screenshot', ['--vmindex','2','--output-dir',str(tmp_path/'out'),'--execute'])
    lock = WorkerInstanceLock(tmp_path/'locks',2)
    lock.acquire(); lock.release()
    assert len(list((tmp_path/'out').glob('*/failure.json'))) == 1


@pytest.mark.parametrize('dump_code', [0, 139])
def test_fresh_xml_is_retained_but_nonzero_dump_never_passes(monkeypatch, tmp_path, dump_code):
    from scripts._device import device_main
    from src.worker.naming_lock import WorkerInstanceLock
    manager = Mock(adb_exe=tmp_path/'adb.exe')
    manager.require_instance.return_value = {'is_android_started':True}
    manager.confirmed_serial.return_value = 'confirmed'
    manager.connect_adb.return_value = 'confirmed'
    monkeypatch.setattr('src.mvp.mumu_client.MuMuClient', lambda: manager)
    monkeypatch.setattr('src.worker.naming_lock.WorkerInstanceLock', lambda **kw: WorkerInstanceLock(tmp_path/'locks', kw['vmindex']))
    commands = []
    def run(cmd, **kw):
        commands.append(cmd)
        if 'com.android.commands.uiautomator.Launcher dump ' in cmd[-1]:
            assert '/system/bin/app_process32 ' in cmd[-1]
            assert cmd[1:3] == ['-s', 'confirmed']
            assert kw['timeout'] <= 15
            return subprocess.CompletedProcess(cmd, dump_code, 'UI hierchary dumped to: '+cmd[-1], '')
        if 'cat' in cmd:
            return subprocess.CompletedProcess(cmd, 0, '<hierarchy><node text="抖音" /></hierarchy>', '')
        return subprocess.CompletedProcess(cmd,0,'','')
    monkeypatch.setattr('src.mvp.command.run_client', run)
    args=['--vmindex','2','--output-dir',str(tmp_path/'out'),'--execute']
    if dump_code:
        with pytest.raises(RuntimeError, match='XML 已留存'):
            device_main('dump', args)
        assert not list((tmp_path/'out').glob('*/result.json'))
        assert not any('rm' in cmd for cmd in commands)
    else:
        assert device_main('dump', args) == 0
        assert any('rm' in cmd for cmd in commands)
    assert len(list((tmp_path/'out').glob('*/ui.xml'))) == 1


@pytest.mark.parametrize('stage', ['launch','mapping','settings'])
def test_worker_cancel_before_navigation(monkeypatch, tmp_path, valid_media, stage):
    from worker_harness import build_worker
    h = build_worker(monkeypatch, tmp_path, valid_media)
    manager = SimpleNamespace()
    calls = []
    def step(name, result=None):
        def call(*a, **kw):
            calls.append(name)
            if name == stage:
                h.controller.request_stop()
            return result
        return call
    manager.require_instance = step('instance', {})
    manager.launch_player = step('launch', True)
    manager.confirmed_serial = step('mapping','synthetic-device')
    def connect(*a, **kw):
        if kw['stop_event'].is_set():
            raise InterruptedError('cancelled')
        calls.append('connect'); return 'synthetic-device'
    manager.connect_adb = connect
    manager.verify_serial = step('verify')
    manager.optimize_performance_for_recording = step('settings')
    manager.check_stop = MuMuClient.check_stop
    monkeypatch.setattr('src.worker.controller.MuMuClient', lambda **kw: manager)
    assert h.controller.run() == 0
    assert h.controller.stop_event.is_set()
    assert not h.launched and not h.adb_calls
    if stage == 'launch':
        assert calls == ['instance','launch']


def valid_events(tmp_path):
    task = tmp_path/'test'
    task.mkdir()
    a, b = task/'123_test_000001.mp4', task/'123_test_000002.mp4'
    a.write_bytes(b'a'); b.write_bytes(b'b')
    rows = [
        dict(event='worker_started',target='123'),
        dict(event='segment_started', segment_index=1,file_path=str(task/'capture_test'/'part_000001.mp4')),
        dict(event='segment_finished',segment_index=1,file_path=str(a),is_final=False),
        dict(event='segment_started', segment_index=2,file_path=str(task/'capture_test'/'part_000002.mp4')),
        dict(event='segment_finished',segment_index=2,file_path=str(b),is_final=True,timestamp=20),
        dict(event='worker_stopped',exit_code=0,reason='stop_requested',had_segment_errors=False)]
    return [dict(row,run_id='test',event_id=str(i)) for i,row in enumerate(rows)]


def test_acceptance_dedup_and_stop_conditions(tmp_path):
    rows = valid_events(tmp_path)
    assert ready_to_stop(rows[:4])
    assert assess(rows+[rows[2]],tmp_path,0,True,False,signal_time=15)['status']=='正常通过'
    assert assess(rows,tmp_path,0,False,False,signal_time=15)['status']=='验收条件未满足'
    assert assess(rows,tmp_path,0,True,True,signal_time=15)['status']=='失败'


@pytest.mark.parametrize('fault', ['conflict','aborted','recovered','had_errors','natural','wrong_path','one_piece',
                                 'wrong_source','swapped_files','other_run','missing_file'])
def test_acceptance_rejects_false_success(tmp_path, fault):
    rows = valid_events(tmp_path)
    if fault=='conflict': rows.append(dict(rows[2],is_final=True))
    if fault=='aborted': rows[2]['event']='segment_aborted'
    if fault=='recovered': rows.append(dict(event='stall_healed',run_id='test',event_id='recovery'))
    if fault=='had_errors': rows[-1]['had_segment_errors']=True
    if fault=='natural': rows[-1]['reason']='anchor_offline'
    if fault=='wrong_path': rows[2]['file_path']='another.mp4'
    if fault=='one_piece': rows=rows[:1]+rows[3:]
    if fault=='wrong_source': rows[1]['file_path']=rows[3]['file_path']
    if fault=='swapped_files': rows[2]['file_path'],rows[4]['file_path']=rows[4]['file_path'],rows[2]['file_path']
    if fault=='other_run': rows[2]['run_id']='another'
    if fault=='missing_file': Path(rows[2]['file_path']).unlink()
    assert assess(rows,tmp_path,0,True,False,signal_time=15)['status'] != '正常通过'

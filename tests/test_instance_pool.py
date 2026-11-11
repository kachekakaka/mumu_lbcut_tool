"""实例池：按 Android 启动状态报告在线，登录强阻断后冷启动抖音。"""
import subprocess
from types import SimpleNamespace

from src.orchestrator.instance_pool import DOUYIN_PACKAGE, InstancePool
from src.orchestrator.models import InstanceStatus


def make_mumu(serial='127.0.0.1:16416'):
    return SimpleNamespace(
        adb_exe='adb.exe',
        list_available_instances=lambda: [
            {'vmindex': 0, 'name': 'lb1', 'android_started': True},
            {'vmindex': 1, 'name': 'lb2', 'android_started': False},
        ],
        connect_adb=lambda vmindex, retries=5: serial)


def test_refresh_reports_offline_when_android_not_started():
    slots = InstancePool(make_mumu()).refresh()
    assert [(s.vmindex, s.name, s.status) for s in slots] == [
        (0, 'lb1', InstanceStatus.IDLE), (1, 'lb2', InstanceStatus.OFFLINE)]
    assert not hasattr(InstanceStatus, 'BLOCKED')
    assert not hasattr(InstancePool, 'unblock')


def test_reset_app_force_stops_and_starts_home(monkeypatch):
    calls = []
    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, 'Starting: Intent { cmp=... }', '')
    monkeypatch.setattr(subprocess, 'run', run)
    assert InstancePool(make_mumu()).reset_app(0)
    assert [c[3:] for c in calls] == [
        ['shell', 'am', 'force-stop', DOUYIN_PACKAGE],
        ['shell', 'am', 'start', '-n', f'{DOUYIN_PACKAGE}/.main.MainActivity']]
    assert all(c[:3] == ['adb.exe', '-s', '127.0.0.1:16416'] for c in calls)
    # 只关停并重启抖音，不清数据。
    assert not any('clear' in c for c in calls)


def test_reset_app_fails_when_adb_unreachable_or_command_errors(monkeypatch):
    assert not InstancePool(make_mumu(serial=None)).reset_app(0)
    monkeypatch.setattr(subprocess, 'run', lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, 'Error: Activity class does not exist.', ''))
    assert not InstancePool(make_mumu()).reset_app(0)
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 20)
    monkeypatch.setattr(subprocess, 'run', boom)
    assert not InstancePool(make_mumu()).reset_app(0)

"""实例池：按 Android 启动状态报告在线，按需开机。"""
from types import SimpleNamespace

from src.orchestrator.instance_pool import InstancePool
from src.orchestrator.models import InstanceStatus


def make_mumu(serial='127.0.0.1:16416', launch_ok=True):
    calls = []
    def launch_player(vmindex, *, stop_event=None):
        calls.append(vmindex)
        return launch_ok
    return SimpleNamespace(
        adb_exe='adb.exe',
        launch_calls=calls,
        launch_player=launch_player,
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


def test_pool_does_not_own_app_cleanup():
    assert not hasattr(InstancePool, 'reset_app')
    assert not hasattr(InstanceStatus, 'RESETTING')


def test_boot_instance_delegates_to_mumu_client_and_guards_errors():
    assert InstanceStatus.BOOTING.value == 'booting'
    mumu = make_mumu(launch_ok=True)
    assert InstancePool(mumu).boot_instance(1) is True
    assert mumu.launch_calls == [1]

    mumu_fail = make_mumu(launch_ok=False)
    assert InstancePool(mumu_fail).boot_instance(1) is False

    def raise_err(vmindex, *, stop_event=None):
        raise RuntimeError('launch exploded')
    mumu_err = SimpleNamespace(launch_player=raise_err)
    assert InstancePool(mumu_err).boot_instance(1) is False

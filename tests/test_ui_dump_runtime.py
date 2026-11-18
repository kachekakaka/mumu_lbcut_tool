"""32 位 UI 导出：命令边界、指标和失败时不误用界面数据。"""
from types import SimpleNamespace
import time

import pytest

from src.mvp.ui_dump import ui_dump_args
from src.worker.adb_client import AdbDeviceClient
from src.worker.popup_bypasser import PopupBypasser


def test_command_uses_system_32bit_runtime_and_exact_classpath():
    assert ui_dump_args('/sdcard/wp_ui_123.xml') == ('shell',
        'CLASSPATH=/system/framework/android.test.runner.jar:/system/framework/uiautomator.jar '
        '/system/bin/app_process32 /system/bin com.android.commands.uiautomator.Launcher dump /sdcard/wp_ui_123.xml')


@pytest.mark.parametrize('path', ['/sdcard/x.xml;reboot', '/sdcard/$(id).xml',
                                  '/sdcard/../x.xml', 'x.xml', '/sdcard/x.xml\n', '/sdcard/a b.xml'])
def test_remote_path_cannot_inject_shell_syntax(path):
    with pytest.raises(ValueError):
        ui_dump_args(path)


@pytest.mark.parametrize('code', [0, 127, 139])
def test_dump_metrics_preserve_exact_serial_and_error_count(monkeypatch, tmp_path, code):
    exe=tmp_path/'adb.exe'; exe.touch()
    seen=[]
    def run(cmd, **kw):
        seen.append((cmd,kw))
        return SimpleNamespace(exit_code=code,stdout='',stderr='')
    monkeypatch.setattr('src.worker.adb_client.run_command',run)
    adb=AdbDeviceClient('127.0.0.1:16448',exe)
    args=ui_dump_args('/data/local/tmp/test.xml')
    adb.run_adb(*args,timeout=2.5)
    assert seen[0][0]==[str(exe),'-s','127.0.0.1:16448',*args]
    assert seen[0][1]['timeout']==2.5
    assert adb.metrics_snapshot()['commands']['ui_dump']['failures']==int(code!=0)


@pytest.mark.parametrize('failure', [126,127,139,'timeout'])
def test_failed_dump_never_reads_xml_clicks_or_falls_back(failure):
    calls=[]
    class Adb:
        def run_adb(self,*args,**kw):
            calls.append((args,kw))
            if len(args)==2:
                if failure=='timeout': raise TimeoutError('timeout')
                return SimpleNamespace(exit_code=failure,stdout='UI hierchary dumped',stderr='failure')
            assert args[:3]==('shell','rm','-f')
            return SimpleNamespace(exit_code=0,stdout='',stderr='')
    assert not PopupBypasser(Adb()).detect_and_dismiss_popups()
    assert len(calls)==2
    assert calls[1][0][-1]==calls[0][0][1].split()[-1]


@pytest.mark.parametrize('xml', ['<hierarchy>', '<hierarchy/>', '<wrong><node/></wrong>'])
def test_zero_exit_requires_valid_nonempty_hierarchy(xml):
    calls=[]
    class Adb:
        def run_adb(self,*args,**kw):
            calls.append(args)
            return SimpleNamespace(exit_code=0,stdout=xml if args[:2]==('shell','cat') else '',stderr='')
    assert PopupBypasser(Adb()).get_ui_dump()==''
    assert calls[-1][:3]==('shell','rm','-f')


def test_expired_probe_budget_does_not_start_dump():
    calls=[]
    class Adb:
        def run_adb(self,*args,**kw):
            calls.append(args)
            assert args[:3]==('shell','rm','-f')
    reader=PopupBypasser(Adb()); reader.deadline=time.monotonic()-1
    assert reader.get_ui_dump()==''
    assert len(calls)==1

"""三个设备工具共用的显式执行流程。"""
import argparse
from contextlib import contextmanager
import io
import json
import signal
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from scripts._common import nonnegative, output_parent, new_batch, preview, write_json
from src.mvp.ui_dump import ui_dump_args

@contextmanager
def cancellation():
    stop = threading.Event()
    previous = {}
    def cancel(*_):
        stop.set()
    for name in ('SIGINT', 'SIGTERM', 'SIGBREAK'):
        sig = getattr(signal, name, None)
        if sig is not None:
            previous[sig] = signal.signal(sig, cancel)
    try:
        yield stop
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)

def device_main(kind, argv=None):
    p = argparse.ArgumentParser(description={'launch':'启动指定实例','dump':'获取指定实例 UI','screenshot':'获取指定实例截图'}[kind])
    p.add_argument('--vmindex', required=True, type=nonnegative)
    if kind != 'launch':
        p.add_argument('--output-dir', required=True)
    p.add_argument('--execute', action='store_true')
    args = p.parse_args(argv)
    parent = output_parent(args.output_dir) if kind != 'launch' else None
    if not args.execute:
        preview(args)
        return 0
    from src.mvp.mumu_client import MuMuClient
    from src.mvp.command import run_client
    from src.worker.naming_lock import WorkerInstanceLock
    manager = MuMuClient()
    lock = WorkerInstanceLock(vmindex=args.vmindex)
    batch = None
    with cancellation() as stop:
        lock.acquire()
        try:
            info = manager.require_instance(args.vmindex, stop_event=stop, prepare_main=kind=='launch')
            if kind == 'launch':
                if not manager.launch_player(args.vmindex, stop_event=stop):
                    manager.check_stop(stop)
                    raise RuntimeError('实例启动失败')
            elif not info.get('is_android_started'):
                raise ValueError('实例尚未启动，取证不会自动开机')
            manager.check_stop(stop)
            serial = manager.confirmed_serial(args.vmindex, stop_event=stop)
            if not manager.connect_adb(args.vmindex, serial=serial, stop_event=stop):
                raise RuntimeError('指定设备未连接')
            manager.verify_serial(args.vmindex, serial, stop_event=stop)
            if kind == 'launch':
                print(json.dumps(dict(vmindex=args.vmindex, serial=serial, ready=True), ensure_ascii=False))
                return 0
            batch = new_batch(parent, kind)
            until = time.monotonic() + (15 if kind == 'screenshot' else 25)
            def adb(*arguments, limit=5, binary=False):
                manager.check_stop(stop)
                remaining = until-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('取证预算耗尽')
                r = run_client([str(manager.adb_exe), '-s', serial, *arguments],
                               timeout=min(limit, remaining), cwd=manager.adb_exe.parent, binary=binary)
                manager.check_stop(stop)
                if r.returncode:
                    raise RuntimeError(f'ADB 命令失败 {arguments!r}，exit={r.returncode}，stdout={r.stdout!r}，stderr={r.stderr!r}')
                return r.stdout
            report = dict(vmindex=args.vmindex, serial=serial, kind=kind)
            if kind == 'screenshot':
                data = adb('exec-out', 'screencap', '-p', limit=10, binary=True)
                from PIL import Image
                with Image.open(io.BytesIO(data)) as picture:
                    if picture.format != 'PNG':
                        raise ValueError('截图不是 PNG')
                    picture.verify()
                with (batch/'screen.png').open('xb') as f:
                    f.write(data)
            else:
                report['focus'] = adb('shell', 'dumpsys', 'window', 'windows')
                remote = '/data/local/tmp/t14-' + uuid.uuid4().hex + '.xml'
                report['remote_xml'] = remote
                dump_error = None
                try:
                    adb(*ui_dump_args(remote), limit=15)
                except RuntimeError as exc:
                    # 非零时仅留存本次唯一 XML 作为失败材料，不改判成功。
                    dump_error = str(exc)
                    report['dump_error'] = dump_error
                xml = adb('shell', 'cat', remote)
                tree = ET.fromstring(xml)
                if tree.tag != 'hierarchy':
                    raise ValueError('未获得新的 hierarchy XML')
                with (batch/'ui.xml').open('x', encoding='utf-8') as f:
                    f.write(xml)
                report['texts'] = [n.get('text') for n in tree.iter('node') if n.get('text')]
                if dump_error:
                    raise RuntimeError('UI 命令失败；本次可解析 XML 已留存：' + dump_error)
                # 删除前再次确认归属；不能为清理改连其他设备。
                manager.verify_serial(args.vmindex, serial, stop_event=stop)
                removed = run_client([str(manager.adb_exe), '-s', serial, 'shell','rm','-f',remote],
                                     timeout=2, cwd=manager.adb_exe.parent)
                if removed.returncode:
                    raise RuntimeError(f'本次临时文件清理失败，保留位置：{remote}')
            manager.verify_serial(args.vmindex, serial, stop_event=stop)
            report['success'] = True
            write_json(batch/'result.json', report)
            print(str(batch))
            return 0
        except BaseException as exc:
            if batch is not None:
                write_json(batch/'failure.json', dict(error=str(exc), remote_xml=locals().get('remote'),
                                                     observation=locals().get('report', {})))
                print(f'失败材料：{batch}')
            raise
        finally:
            lock.release()

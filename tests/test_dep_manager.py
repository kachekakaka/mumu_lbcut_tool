"""依赖管理模块单元测试。"""
import unittest
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile

import pytest
from src.mvp.dep_manager import DependencyManager
from src.mvp.config import SCRCPY_DIR, get_adb_exe


class TestDependencyManager(unittest.TestCase):
    def test_adb_ready(self):
        manager = DependencyManager()
        adb_path = manager.get_adb_path()
        self.assertTrue(adb_path.exists())
        self.assertEqual(adb_path.name.lower(), "adb.exe")

    def test_scrcpy_path_getter(self):
        manager = DependencyManager()
        expected = SCRCPY_DIR / "scrcpy.exe"
        self.assertEqual(manager.scrcpy_exe_path, expected)


INSTALLER = r'''
import os, sys, time
from pathlib import Path
from src.mvp.dep_manager import DependencyManager
root, mode = Path(sys.argv[1]), sys.argv[2]
manager = DependencyManager(root / 'scrcpy')
def archive():
    (root / ('download-' + str(os.getpid()))).touch()
    if mode == 'crash':
        os._exit(37)
    if mode == 'hold':
        until = time.monotonic() + 8
        while not (root / 'release').exists():
            if time.monotonic() > until:
                raise TimeoutError('安装锁测试未放行')
            time.sleep(.02)
    return (root / 'bundle.zip').as_uri()
manager._get_latest_win64_url = archive
assert manager.ensure_scrcpy() == manager.require_scrcpy()
'''


def write_bundle(root):
    with zipfile.ZipFile(root / 'bundle.zip', 'w') as archive:
        archive.writestr('package/scrcpy.exe', 'new-exe')
        archive.writestr('package/scrcpy-server', 'new-server')


@pytest.mark.parametrize('crash_first', [False, True])
def test_independent_installers_share_one_complete_bundle(tmp_path, crash_first):
    write_bundle(tmp_path)
    script = [sys.executable, '-c', INSTALLER, str(tmp_path)]
    first = subprocess.Popen([*script, 'crash' if crash_first else 'hold'])
    children = [first]
    try:
        until = time.monotonic() + 5
        while not list(tmp_path.glob('download-*')) and time.monotonic() < until:
            time.sleep(.02)
        assert len(list(tmp_path.glob('download-*'))) == 1
        if crash_first:
            assert first.wait(5) == 37
        second = subprocess.Popen([*script, 'join'])
        children.append(second)
        (tmp_path / 'release').touch()
        assert second.wait(10) == 0
        if not crash_first:
            assert first.wait(10) == 0
        assert len(list(tmp_path.glob('download-*'))) == (2 if crash_first else 1)
        manager = DependencyManager(tmp_path / 'scrcpy')
        assert manager.require_scrcpy().read_text() == 'new-exe'
        assert (manager.scrcpy_dir / 'scrcpy-server').read_text() == 'new-server'
    finally:
        (tmp_path / 'release').touch(exist_ok=True)
        for proc in children:
            if proc.poll() is None:
                proc.kill()
                proc.wait(5)


def test_scrcpy_requires_server_and_preserves_an_installation_in_use(tmp_path, monkeypatch):
    write_bundle(tmp_path)
    manager = DependencyManager(tmp_path / 'scrcpy')
    manager.scrcpy_dir.mkdir()
    manager.scrcpy_exe_path.write_text('existing')
    assert not manager.is_scrcpy_ready()
    with pytest.raises(FileNotFoundError, match='scrcpy-server'):
        manager.ensure_scrcpy()
    server = manager.scrcpy_dir / 'scrcpy-server'
    server.write_text('original-server')
    monkeypatch.setattr(manager, '_get_latest_win64_url', lambda: (tmp_path / 'bundle.zip').as_uri())
    monkeypatch.setattr('src.mvp.dep_manager.psutil.process_iter', lambda _attrs: [
        type('RunningScrcpy', (), {'pid': os.getpid(), 'info': {'exe': str(manager.scrcpy_exe_path)}})()
    ])
    with pytest.raises(PermissionError, match='正在使用'):
        manager.ensure_scrcpy(force_download=True)
    assert manager.require_scrcpy().read_text() == 'existing' and server.read_text() == 'original-server'


if __name__ == "__main__":
    unittest.main()

"""固定媒体工具的部署边界与设备操作前失败行为。"""
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from src.mvp.config import FFMPEG_BUILD
from src.mvp.dep_manager import DependencyManager


def installed(tmp_path):
    directory = tmp_path / 'Gyan tools'
    (directory / 'bin').mkdir(parents=True)
    for tool in ('ffmpeg', 'ffprobe'):
        (directory / 'bin' / (tool + '.exe')).write_bytes(b'test executable')
    return directory


def version_result(command, **kwargs):
    name = Path(command[0]).stem
    return SimpleNamespace(returncode=0, stderr='', stdout=(
        f'{name} version {FFMPEG_BUILD} Copyright\n'
        'built with gcc 16.2.0\nconfiguration: --enable-gpl --enable-version3 --enable-static\n'))


def test_pinned_pair_ignores_path_and_uses_matching_build(monkeypatch, tmp_path):
    directory = installed(tmp_path)
    monkeypatch.setenv('PATH', str(tmp_path / 'untrusted PATH tools'))
    run = Mock(side_effect=version_result)
    monkeypatch.setattr('src.mvp.dep_manager.run_client', run)
    tools = DependencyManager(ffmpeg_dir=directory).get_media_tools()
    assert tools.ffmpeg == directory.resolve() / 'bin' / 'ffmpeg.exe'
    assert tools.ffprobe == directory.resolve() / 'bin' / 'ffprobe.exe'
    assert [call.args[0] for call in run.call_args_list] == [
        [str(tools.ffmpeg), '-version'], [str(tools.ffprobe), '-version']]


@pytest.mark.parametrize('problem', ['missing', 'old', 'essentials', 'mismatched', 'failed'])
def test_invalid_pair_is_not_replaced_from_path(monkeypatch, tmp_path, problem):
    directory = installed(tmp_path)
    if problem == 'missing':
        (directory / 'bin' / 'ffprobe.exe').unlink()
    def run(command, **kwargs):
        result = version_result(command)
        if Path(command[0]).stem == 'ffprobe':
            if problem == 'old':
                result.stdout = result.stdout.replace('9.0.2', '6.0')
            if problem == 'essentials':
                result.stdout = result.stdout.replace('full_build', 'essentials_build')
            if problem == 'mismatched':
                result.stdout = result.stdout.replace('--enable-static', '--enable-shared')
            if problem == 'failed':
                result.returncode = 1
        return result
    monkeypatch.setattr('src.mvp.dep_manager.run_client', run)
    with pytest.raises((FileNotFoundError, RuntimeError)):
        DependencyManager(ffmpeg_dir=directory).get_media_tools()


def test_missing_fixed_ffprobe_stops_at_real_dependency_gate(monkeypatch, tmp_path):
    from src.worker import controller as module
    from src.worker.controller import WorkerConfig, WorkerController
    from src.worker.events import WorkerEventEmitter
    directory = installed(tmp_path)
    (directory / 'bin' / 'ffprobe.exe').unlink()
    dependency = DependencyManager(ffmpeg_dir=directory)
    monkeypatch.setattr('src.mvp.dep_manager.run_client', version_result)
    monkeypatch.setattr(module, 'DependencyManager', lambda: dependency)
    device = Mock()
    monkeypatch.setattr(module, 'MuMuClient', device)
    config = WorkerConfig(vmindex=321, target='123', target_kind='room_id', expected_anchor_uid='456', output_dir=tmp_path/'out', lock_dir=tmp_path/'locks')
    stream = io.StringIO()
    worker = WorkerController(config, WorkerEventEmitter(stream))
    assert worker.run() != 0
    device.assert_not_called()
    stopped = json.loads(stream.getvalue().splitlines()[-1])
    assert stopped['event'] == 'worker_stopped'
    assert '固定媒体工具' in stopped['reason'] and 'ffprobe.exe' in stopped['reason']

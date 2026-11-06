"""实际临时文件与可控外部进程边界：启动需有媒体进展，拒绝覆盖。"""
from pathlib import Path
from unittest.mock import Mock
import pytest

from src.worker.pipeline import WorkerPipeline


def test_segment_lifecycle_requires_file_progress(tmp_path, monkeypatch):
    proc = Mock()
    proc.poll.return_value = None
    proc.stdout = proc.stderr = None
    monkeypatch.setattr('src.worker.pipeline.subprocess.Popen', lambda *a, **kw: proc)
    pipe = WorkerPipeline('synthetic-device', tmp_path, scrcpy_exe=tmp_path / 'scrcpy.exe')
    def write_progress():
        with pipe.current_file_path.open('ab') as out:
            out.write(b'x' * 2048)
        return True
    pipe.wait_hook = write_progress
    file_path = pipe.start_segment(1)
    assert file_path.exists() and file_path.stat().st_size >= 4096
    assert pipe.is_recording()
    proc.poll.return_value = 0
    assert pipe.stop_segment()
    assert not pipe.is_recording()


def test_segment_never_overwrites_existing_file(tmp_path, monkeypatch):
    monkeypatch.setattr('src.worker.pipeline.time.strftime', lambda pattern: 'fixed')
    existing = tmp_path / 'record_fixed_seg_001.mp4'
    existing.write_bytes(b'valuable original')
    spawn = Mock()
    monkeypatch.setattr('src.worker.pipeline.subprocess.Popen', spawn)
    pipe = WorkerPipeline('synthetic-device', tmp_path, scrcpy_exe=tmp_path / 'scrcpy.exe')
    with pytest.raises(FileExistsError):
        pipe.start_segment(1)
    assert existing.read_bytes() == b'valuable original'
    spawn.assert_not_called()


def test_alive_without_media_times_out_and_is_reaped(tmp_path, monkeypatch):
    proc = Mock()
    state = {'alive': True}
    proc.poll.side_effect = lambda: None if state['alive'] else 1
    proc.send_signal.side_effect = lambda sig: state.update(alive=False)
    proc.stdout = proc.stderr = None
    monkeypatch.setattr('src.worker.pipeline.subprocess.Popen', lambda *a, **kw: proc)
    pipe = WorkerPipeline('synthetic-device', tmp_path, scrcpy_exe=tmp_path / 'scrcpy.exe')
    pipe.startup_timeout_sec = .05
    with pytest.raises(TimeoutError):
        pipe.start_segment(1)
    assert not state['alive']
    assert proc.send_signal.called

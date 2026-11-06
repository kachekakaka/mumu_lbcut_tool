"""Worker 实际控制流程不负责公网查询；信号请求经统一收尾。"""
import pytest
from worker_harness import build_worker, assert_paired
from src.worker.resolver import clean_room_target


def test_local_worker_does_not_request_network(monkeypatch, tmp_path, valid_media):
    h = build_worker(monkeypatch, tmp_path, valid_media, total=1, segment=20)
    def forbidden(*args, **kwargs):
        raise AssertionError('Worker 录制主流程不得访问公网')
    monkeypatch.setattr('requests.sessions.Session.request', forbidden)
    assert h.controller.run() == 0
    assert_paired(h.events())


def test_stop_request_closes_active_recording(monkeypatch, tmp_path, valid_media):
    h = build_worker(monkeypatch, tmp_path, valid_media, total=0, segment=20)
    h.clock.on_sleep = lambda now: h.controller.request_stop() if now >= 1001 else None
    assert h.controller.run() == 0
    terminals = assert_paired(h.events())
    assert terminals[-1]['is_final']
    assert h.controller.state_machine.state.value == 'STOPPED'
    assert not any(p.alive for p in h.pipelines)


def test_unresolved_short_link_rejected_locally():
    with pytest.raises(ValueError, match='需由上层调度平台解析'):
        clean_room_target('https://v.douyin.com/abcxyz/')

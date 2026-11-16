"""持续分装与可见遮挡恢复：真实逻辑、隔离媒体/设备边界。"""
from pathlib import Path
import json
import shutil
import subprocess
import time
from unittest.mock import Mock
import pytest

from src.worker.continuous_pipeline import read_fragment_manifest, Fragment, ContinuousPipeline
from src.worker.navigator import WorkerNavigator
from src.mvp.dep_manager import MediaTools



def test_no_blind_background_tap_in_identity_cleanup():
    import inspect
    source=inspect.getsource(WorkerNavigator._observe_live_room)
    assert "int(frame.shape[0]*.2)" not in source
    assert "int(h*.25)" not in source
    assert "'input','tap'" not in source.split('finally:')[1]


def test_overlay_back_rechecks_and_does_not_leave_clean_live(monkeypatch):
    nav=WorkerNavigator.__new__(WorkerNavigator)
    commands=[]
    nav._adb=lambda *args,**kw: commands.append(args) or ''
    nav._navigation_sleep=lambda *a: True
    states=iter([{'activity':'LivePlayActivity','overlay':'live_panel_window','ui_available':True},
                 {'activity':'LivePlayActivity','overlay':None,'ui_available':True}])
    nav.inspect_live_surface=lambda **kw:next(states)
    assert nav.dismiss_live_overlay(time.monotonic()+10,lambda:False)
    assert commands==[('shell','input','keyevent','4')]
    nav.inspect_live_surface=lambda **kw:{'activity':'LivePlayActivity','overlay':None,'ui_available':True}
    commands.clear();assert nav.dismiss_live_overlay(time.monotonic()+10,lambda:False)
    assert commands==[]


def test_partial_manifest_is_not_closed(tmp_path):
    (tmp_path/'segments.csv').write_text('part_000001.mp4,0,6\npart_000002.mp4,6,',encoding='utf8')
    result=read_fragment_manifest(tmp_path)
    assert list(result)==[1] and result[1].closed
    assert result[1].end_pts==6


def test_no_window_and_no_control_are_used_for_continuous_capture():
    import inspect
    source=inspect.getsource(ContinuousPipeline.start)
    assert '--no-control' in source and '--record-format=mkv' in source
    assert "'-c','copy'" in source
    assert '-r' not in [p.strip("'\"") for p in source.split(',')]


def test_capture_error_after_total_duration_cutoff_is_tagged_expected(tmp_path,caplog):
    import logging
    from types import SimpleNamespace
    caplog.set_level(logging.INFO,logger='src.worker.continuous_pipeline')
    line='ERROR: Failed to write trailer to \\\\.\\pipe\\mumu_capture_x'
    p=ContinuousPipeline('synthetic',tmp_path,Path('scrcpy.exe'),Path('adb.exe'),2,
                         ffmpeg_exe=Path('ffmpeg.exe'),total_duration_sec=300)
    p._mux=SimpleNamespace(poll=lambda:None)
    p._log_capture_line(line)
    p._mux=SimpleNamespace(poll=lambda:0)
    p._log_capture_line(line)
    p.total_duration_sec=0
    p._log_capture_line(line)
    messages=[r.getMessage() for r in caplog.records]
    assert messages==['[capture] '+line,'[capture] (已录满总时长，预期收尾) '+line,'[capture] '+line]
    assert list(p.logs)==[line]*3


def test_real_controller_rotates_without_restarting_capture(monkeypatch,tmp_path,valid_media):
    from worker_harness import build_worker,assert_paired
    # 只替换 capture/muxer 输出边界；Controller、事件、封口校验、会话均真实执行。
    h=build_worker(monkeypatch,tmp_path,valid_media,total=7,segment=2)
    assert h.controller.run()==0
    events=h.events();terminals=assert_paired(events)
    assert len(terminals)>=3
    made=h.pipelines
    assert len(made)==1 and made[0].start_count==1 and made[0].stop_count==1
    assert sum(e.get('is_final',False) for e in terminals)==1
    assert all(e['event']=='segment_finished' for e in terminals)
    sessions=[e for e in events if e['event']=='capture_session']
    assert [e['action'] for e in sessions]==['started','stopped']


@pytest.mark.parametrize('flag',['--recording-mode','--overlap-duration'])
def test_worker_cli_has_no_restart_or_overlap_options(flag):
    from src.worker.cli import build_arg_parser
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args(['--vmindex','2','--target','123','--output-dir','unused',flag,'1'])


def test_runtime_overlay_recovery_keeps_same_capture_process(tmp_path):
    from types import SimpleNamespace
    from src.worker.continuous_session import ContinuousSession
    from src.worker.state_machine import WorkerStateMachine,WorkerState
    now=time.monotonic()
    emitted=[]
    nav=SimpleNamespace(dismiss_live_overlay=Mock(return_value=True),wait_live_picture=Mock(return_value=True))
    c=SimpleNamespace(stop_requested=False,current_generation_id=1,_navigator=nav,_watchdog=None,
        config=SimpleNamespace(stall_timeout_sec=10),target_room_id='123',
        state_machine=WorkerStateMachine(WorkerState.RECORDING),
        emitter=SimpleNamespace(emit=emitted.append),action_dispatcher=SimpleNamespace(post_action=lambda a:None))
    class Watchdog:
        def __init__(self,**kw):pass
        def start(self):pass
    s=ContinuousSession(c,'serial',Path('scrcpy'),Path('adb'),
                        MediaTools(Path('ffmpeg.exe'),Path('ffprobe.exe')),watchdog_factory=Watchdog)
    s.pipe=object();original=s.pipe;s.epoch=1;s.last_clear_wall=time.time()-5;s.last_surface=now
    s.ledger={1:{'epoch':1,'terminal':False,'wall':time.time()}}
    sync_count=[0]
    def sync(**kwargs):
        sync_count[0]+=1
        if sync_count[0]>=2:
            # 面板关闭期间后台已切到新文件，风险事件必须覆盖后发现的文件。
            s.ledger[2]={'epoch':1,'terminal':False,'wall':time.time()}
    s.sync=sync
    from concurrent.futures import Future
    s.surface_future=Future();s.surface_future.set_result({'activity':'LivePlayActivity','overlay':'live_panel_window','ui_available':True})
    s.surface_gen=1
    try:
        s.poll_surface()
        assert s.pipe is original
        assert c.state_machine.state==WorkerState.RECORDING
        assert nav.dismiss_live_overlay.call_count==1
        assert [e.action for e in emitted if e.event=='overlay_recovery']==['detected','closed']
        assert {e.segment_index for e in emitted if e.event=='segment_quality_warning'}=={1,2}
    finally:
        s.probes.shutdown(wait=True);s.pool.shutdown(wait=True)


@pytest.mark.parametrize('flag',['--recording-mode','--overlap-duration'])
def test_scheduler_cli_has_no_restart_or_overlap_options(flag):
    from src.orchestrator.cli import build_parser
    with pytest.raises(SystemExit):
        build_parser().parse_args(['run','--instances','2','--output-dir','output',flag,'1'])

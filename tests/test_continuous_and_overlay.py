"""持续分装与可见遮挡恢复：真实逻辑、隔离媒体/设备边界。"""
from pathlib import Path
import json
import shutil
import subprocess
import time
from unittest.mock import Mock
import pytest

from src.worker.live_overlay import classify_live_overlay
from src.worker.continuous_pipeline import read_fragment_manifest, Fragment, ContinuousPipeline
from src.worker.navigator import WorkerNavigator


def ui(*texts):
    import xml.sax.saxutils as escape
    return '<hierarchy>'+''.join('<node package="com.ss.android.ugc.aweme" text="'+escape.escape(t,{'"':'&quot;'})+'" />' for t in texts)+'</hierarchy>'


def test_gift_gallery_requires_actual_panel_markers():
    assert classify_live_overlay(ui('主播甄选展馆','展馆贡献','快速点亮')).kind=='gift_gallery'
    assert classify_live_overlay(ui('展馆','礼物','关注','送礼支持主播')) is None
    assert classify_live_overlay(ui('主播甄选展馆')) is None
    assert classify_live_overlay('stale or invalid') is None


def test_no_blind_background_tap_in_identity_cleanup():
    import inspect
    source=inspect.getsource(WorkerNavigator.observe_live_room)
    assert "int(frame.shape[0]*.2)" not in source
    assert "int(h*.25)" not in source
    assert 'dismiss_live_overlay' in source


def test_overlay_back_rechecks_and_does_not_leave_clean_live(monkeypatch):
    nav=WorkerNavigator.__new__(WorkerNavigator)
    commands=[]
    nav._adb=lambda *args,**kw: commands.append(args) or ''
    nav._navigation_sleep=lambda *a: True
    states=iter([{'activity':'LivePlayActivity','overlay':'gift_gallery','ui_available':True},
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


def test_real_controller_rotates_without_restarting_capture(monkeypatch,tmp_path,valid_media):
    import src.worker.continuous_session as cs
    from worker_harness import build_worker,assert_paired,PassiveWatchdog
    h=build_worker(monkeypatch,tmp_path,valid_media,total=7,segment=2)
    h.controller.config.recording_mode='continuous'
    # 只替换 capture/muxer 输出边界；Controller、事件、封口校验、会话均真实执行。
    made=[]
    class Pipe:
        def __init__(self,output_dir,start_index,**kwargs):
            self.output_dir=Path(output_dir);self.base=start_index
            self.start_count=0;self.stop_count=0;self.rows={};self.alive=False;self.wait_hook=None
            self._proc=type('P',(),{'pid':12345})();self._mux=type('P',(),{'pid':12346})()
            made.append(self)
        def start(self):
            self.start_count+=1;self.alive=True;self.begin=h.clock.monotonic()
        def fragments(self):
            n=max(1,int((h.clock.monotonic()-self.begin)//2)+1)
            for off in range(n):
                idx=self.base+off
                if idx not in self.rows:
                    p=self.output_dir/f'raw_cont_{idx}.mp4';shutil.copyfile(valid_media,p)
                    self.rows[idx]=p
            return [Fragment(i,p,not self.alive or i<max(self.rows),(i-self.base)*2,(i-self.base+1)*2) for i,p in self.rows.items()]
        def stop(self,*a,**kw):
            if self.alive:self.stop_count+=1
            self.alive=False;return True
        teardown=stop
        def get_exit_code(self):return None if self.alive else 0
        def is_recording(self):return self.alive
        def get_current_file_size_mb(self):return h.clock.monotonic()
        def failure_details(self):return ''
    original_init=cs.ContinuousSession.__init__
    def init(self,*a,**kw):
        original_init(self,*a,pipeline_factory=Pipe,watchdog_factory=PassiveWatchdog,
                      clock=h.clock.monotonic,sleeper=lambda sec:(h.clock.sleep(sec),time.sleep(.003)))
    monkeypatch.setattr(cs.ContinuousSession,'__init__',init)
    assert h.controller.run()==0
    events=h.events();terminals=assert_paired(events)
    assert len(terminals)>=3
    assert len(made)==1 and made[0].start_count==1 and made[0].stop_count==1
    assert sum(e.get('is_final',False) for e in terminals)==1
    assert all(e['event']=='segment_finished' for e in terminals)
    sessions=[e for e in events if e['event']=='capture_session']
    assert [e['action'] for e in sessions]==['started','stopped']


def test_continuous_is_cli_default_and_overlap_requires_restart():
    from src.worker.cli import build_arg_parser,validate_args
    args=build_arg_parser().parse_args(['--vmindex','2','--target','123','--output-dir','unused'])
    assert args.recording_mode=='continuous'
    args.overlap_duration=3
    with pytest.raises(SystemExit):validate_args(args)
    args.recording_mode='restart';validate_args(args)


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
    s=ContinuousSession(c,'serial',Path('scrcpy'),Path('adb'),watchdog_factory=Watchdog)
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
    s.surface_future=Future();s.surface_future.set_result({'activity':'LivePlayActivity','overlay':'gift_gallery','ui_available':True})
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


def test_scheduler_cli_defaults_to_continuous_and_accepts_explicit_diagnostic_mode():
    from src.orchestrator.cli import build_parser
    base=['run','--instances','2','--output-dir','output']
    assert build_parser().parse_args(base).recording_mode=='continuous'
    assert build_parser().parse_args(base+['--recording-mode','restart']).recording_mode=='restart'

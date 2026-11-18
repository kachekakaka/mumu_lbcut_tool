"""慢页面与阶段预算回归：保留实际定位器、导航器决策，只替换设备和时钟。"""
from types import SimpleNamespace
import time
import numpy as np
import pytest
from src.worker.room_locator import RoomEvidence, locate_target
from src.worker.navigator import WorkerNavigator


class Clock:
    def __init__(self): self.now=1000.0
    def monotonic(self): return self.now
    def time(self): return 1800000000+self.now
    def sleep(self, sec): self.now+=max(0,sec)


class SlowPage:
    last_gate_reason='render_timeout'
    def __init__(self, clock):
        self.clock=clock;self.routes=[];self.reads=0;self.pictures=[]
    def device_online(self): return True
    def navigate_route(self,target,web,route,deadline,stop):
        self.routes.append(route);self.clock.sleep(5);return True
    def wait_live_page(self,deadline,stop_requested):
        self.clock.sleep(10);return True
    def observe_live_room(self,deadline,stop_requested):
        self.reads+=1;self.clock.sleep(min(55,deadline-self.clock.monotonic()))
        return RoomEvidence('900','123',source='device')
    def wait_live_picture(self,deadline,stop_requested,**kw):
        remaining=deadline-self.clock.monotonic();self.pictures.append(remaining)
        if remaining<25:
            self.clock.sleep(max(0,remaining));return False
        self.clock.sleep(25);return True


def test_slow_evidence_does_not_starve_final_picture(monkeypatch):
    import src.worker.room_locator as m
    clock=Clock();monkeypatch.setattr(m,'time',clock);nav=SlowPage(clock);steps=[]
    result=locate_target(nav,'900',target_kind='room_id',anchor_uid='123',timeout_sec=150,
                         on_step=lambda **kw:steps.append(kw))
    assert result.success
    assert nav.routes==['primary'] and nav.reads==2
    assert nav.pictures[-1]>=45
    assert all('remaining_sec' in row for row in steps)


def test_final_picture_transient_failure_reuses_verified_room(monkeypatch):
    import src.worker.room_locator as m
    clock=Clock();monkeypatch.setattr(m,'time',clock);nav=SlowPage(clock)
    def picture(deadline,stop_requested,**kw):
        nav.pictures.append(deadline-clock.monotonic());clock.sleep(5)
        nav.last_gate_reason='page_probe_unavailable'
        return len(nav.pictures)>=2
    nav.wait_live_picture=picture
    result=locate_target(nav,'900',target_kind='room_id',anchor_uid='123',timeout_sec=300)
    assert result.success and nav.reads==2 and nav.routes==['primary']


def test_budget_exhaustion_never_launches_another_route(monkeypatch):
    import src.worker.room_locator as m
    clock=Clock();monkeypatch.setattr(m,'time',clock);nav=SlowPage(clock)
    def no_proof(deadline,stop_requested):
        nav.reads+=1;clock.sleep(max(0,deadline-clock.monotonic()))
        return RoomEvidence()
    nav.observe_live_room=no_proof
    result=locate_target(nav,'900',target_kind='room_id',timeout_sec=80)
    assert not result.success and clock.monotonic()<=1080
    assert len(nav.routes)==1


def test_stop_after_evidence_never_checks_picture(monkeypatch):
    import src.worker.room_locator as m
    clock=Clock();monkeypatch.setattr(m,'time',clock);nav=SlowPage(clock)
    result=locate_target(nav,'900',target_kind='room_id',stop_requested=lambda:nav.reads>0)
    assert result.reason=='cancelled' and not nav.pictures


def test_unknown_activity_is_retried_not_treated_as_wrong_page(monkeypatch):
    import src.worker.navigator as m
    clock=Clock();monkeypatch.setattr(m,'time',clock)
    nav=WorkerNavigator.__new__(WorkerNavigator);nav._operation_deadline=None
    activities=iter([None,'LivePlayActivity','LivePlayActivity','LivePlayActivity'])
    nav.get_current_focus_activity=lambda:next(activities,'LivePlayActivity')
    nav._navigation_sleep=lambda sec,deadline,stop:clock.sleep(sec) or not stop()
    a=np.zeros((100,100,3),dtype=np.uint8);b=a.copy();b[20:50,20:50]=200
    frames=iter([a,b]);nav._capture_frame_array=lambda:next(frames)
    assert nav.wait_live_picture(1030,lambda:False,check_overlay=False)


def test_too_little_command_budget_does_not_spawn_doomed_adb(monkeypatch):
    import src.worker.navigator as m
    clock=Clock();monkeypatch.setattr(m,'time',clock)
    nav=WorkerNavigator.__new__(WorkerNavigator);nav._operation_deadline=1000.08
    calls=[]
    nav.adb_client=SimpleNamespace(run_adb=lambda *a,**kw:calls.append(kw) or SimpleNamespace(exit_code=0,stdout='',stderr=''))
    assert nav._adb('shell','dumpsys','window','windows')==''
    assert not calls


def test_ui_dump_can_take_seven_seconds_with_explicit_probe_budget():
    from src.worker.popup_bypasser import PopupBypasser
    observed=[]
    class Adb:
        def run_adb(self,*args,timeout,**kwargs):
            observed.append((args,timeout))
            if len(args)==2 and 'com.android.commands.uiautomator.Launcher dump ' in args[1]:
                if timeout<7:raise TimeoutError('slow fresh UI')
                return SimpleNamespace(exit_code=0,stdout='',stderr='')
            if args[:2] == ("shell","cat"):
                return SimpleNamespace(exit_code=0,stdout='<hierarchy><node text="fresh"/></hierarchy>',stderr='')
            return SimpleNamespace(exit_code=0,stdout='',stderr='')
    reader=PopupBypasser(Adb());reader.dump_timeout_sec=8
    reader.deadline=time.monotonic()+12
    assert 'fresh' in reader.get_ui_dump()
    assert observed[0][1]>=7


def test_default_navigation_timeout_is_shared_with_cli_and_sdk(tmp_path):
    from src.worker.cli import build_arg_parser,validate_args
    from src.orchestrator.cli import build_parser
    from src.orchestrator.models import WorkerLaunchSpec
    args=build_arg_parser().parse_args(['--vmindex','2','--target','900','--output-dir',str(tmp_path)])
    assert args.navigation_timeout==300
    assert build_parser().parse_args(['run','--instances','2','--output-dir',str(tmp_path)]).navigation_timeout==300
    assert WorkerLaunchSpec(2,'900',tmp_path).navigation_timeout_sec==300
    for value in (0,-1,float('nan'),float('inf'),59):
        args.navigation_timeout=value
        with pytest.raises(SystemExit):validate_args(args)


def test_known_page_change_invalidates_previous_verified_evidence(monkeypatch):
    import src.worker.room_locator as m
    from src.worker.identity_verifier import IdentityStatus
    clock=Clock();monkeypatch.setattr(m,'time',clock);nav=SlowPage(clock)
    def changed(deadline,stop_requested,**kw):
        nav.last_gate_reason='navigation_failed';clock.sleep(2);return False
    nav.wait_live_picture=changed
    result=locate_target(nav,'900',target_kind='room_id',anchor_uid='123',timeout_sec=100,max_attempts=1)
    assert not result.success and result.status==IdentityStatus.UNCONFIRMED and result.evidence is None


def test_stuck_picture_stays_in_stage_and_ends_within_total_deadline(monkeypatch):
    import src.worker.room_locator as m
    clock=Clock();monkeypatch.setattr(m,'time',clock);nav=SlowPage(clock)
    def stuck(deadline,stop_requested,**kw):
        nav.last_gate_reason='render_timeout';clock.sleep(deadline-clock.monotonic());return False
    nav.wait_live_picture=stuck
    result=locate_target(nav,'900',target_kind='room_id',anchor_uid='123',timeout_sec=150)
    assert not result.success and result.reason=='render_timeout'
    assert nav.reads==2 and nav.routes==['primary'] and clock.monotonic()<=1150


def test_worker_cli_passes_configured_timeout_to_controller(monkeypatch,tmp_path):
    import src.worker.cli as cli
    configs=[]
    class Controller:
        def __init__(self,config):configs.append(config)
        def run(self):return 0
        def request_stop(self):pass
    monkeypatch.setattr(cli,'WorkerController',Controller)
    monkeypatch.setattr(cli.signal,'signal',lambda *a:None)
    assert cli.main(['--vmindex','2','--target','900','--output-dir',str(tmp_path),
                     '--navigation-timeout','420','--target-kind','room_id','--expected-anchor-uid','123'])==0
    assert configs[0].navigation_timeout_sec==420


def test_real_controller_uses_timeout_and_reports_phase(monkeypatch,tmp_path,valid_media):
    import src.worker.controller as cm
    from worker_harness import build_worker
    from src.worker.room_locator import LandingResult
    h=build_worker(monkeypatch,tmp_path,valid_media)
    h.controller.config.navigation_timeout_sec=420
    observed=[]
    def locate(nav,target,**kw):
        observed.append(kw['timeout_sec'])
        kw['on_step'](attempt=1,action='wait_picture',phase='picture',elapsed_sec=105,
                      remaining_sec=315,stage_budget_sec=45)
        return LandingResult(False,'render_timeout')
    monkeypatch.setattr(cm,'locate_target',locate)
    assert h.controller.run()==16
    assert observed==[420] and not h.launched
    event=next(e for e in h.events() if e['event']=='room_navigation')
    assert (event['phase'],event['remaining_sec'],event['stage_budget_sec'])==('picture',315,45)


def test_cancelled_observation_cleanup_does_not_spend_whole_remaining_stage(monkeypatch):
    import src.worker.navigator as m
    clock=Clock();monkeypatch.setattr(m,'time',clock)
    nav=WorkerNavigator.__new__(WorkerNavigator);nav._operation_deadline=None;nav.adb_client=object()
    nav.get_current_focus_activity=lambda:clock.sleep(2) or 'LivePlayActivity'
    nav.dismiss_live_overlay=lambda deadline,stop:clock.sleep(max(0,deadline-clock.monotonic())) or True
    nav.observe_live_room(1060,lambda:True)
    assert clock.monotonic()<=1010


def test_navigation_terminal_event_keeps_actual_phase_and_timing(monkeypatch,tmp_path,valid_media):
    import src.worker.controller as cm
    from src.worker.room_locator import LandingResult
    from src.worker.identity_verifier import IdentityStatus
    from worker_harness import build_worker
    h=build_worker(monkeypatch,tmp_path,valid_media)
    def locate(nav,target,**kw):
        h.clock.sleep(45)
        kw['on_step'](attempt=1,action='wait_picture',phase='picture',elapsed_sec=45,remaining_sec=255)
        h.clock.sleep(5)
        return LandingResult(True,'target_located',IdentityStatus.VERIFIED,RoomEvidence('900','123'),1)
    monkeypatch.setattr(cm,'locate_target',locate)
    h.controller._locate_target_room()
    event=next(e for e in h.events() if e['event']=='room_navigation' and e['action']=='target_ready')
    assert event['phase']=='picture' and event['elapsed_sec']==50 and event['remaining_sec']==250

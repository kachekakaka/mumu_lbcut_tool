"""K17：实际状态转换、只读取证与真实会话中的风险回标。"""
import time
import pytest
from types import SimpleNamespace

from src.worker.runtime_identity import RuntimeIdentity, IdentityObservation
from src.worker.room_locator import RoomEvidence, LandingResult
from src.worker.identity_verifier import IdentityStatus as Status
from src.worker.navigator import WorkerNavigator

A, B, UID = '7704666294802123554', '7704666294802123555', '900001'


def observation(at, room=A, uid=UID):
    evidence=RoomEvidence(room,uid,source='live_fragment_arguments',observed_at=10000+at) if room else None
    return IdentityObservation(evidence,'' if room else 'room_evidence_unavailable',at,at+.1,10000+at)


def verified():
    state=RuntimeIdentity(A,UID)
    state.adopt(LandingResult(True,'target_located',Status.VERIFIED,observation(0).evidence,1,0,10000))
    return state


def test_periodic_matching_requires_independent_pair_and_updates_conservative_time():
    state=verified()
    assert state.accept(observation(10))=='sample'
    assert state.confirmed_mono==0
    assert state.accept(observation(11.2))=='confirmed'
    assert state.confirmed_mono==11.2 and state.next_at==20


def test_conflict_grace_then_two_equal_samples():
    state=verified()
    state.accept(observation(10,B))
    assert state.status==Status.UNCONFIRMED
    state.accept(observation(12,B))
    assert not state.pending_reason
    state.accept(observation(15.2,B))
    assert not state.pending_reason
    state.accept(observation(16.4,B))
    assert state.pending_reason=='runtime_identity_mismatch'


def test_missing_and_changing_conflicts_do_not_extend_deadline_or_bypass_override():
    state=verified()
    state.allow_unconfirmed=True
    state.accept(observation(10,B))
    deadline=state.deadline()
    for at,room in [(13,None),(17,B),(20,'999'),(24,None)]:
        state.accept(observation(at,room))
        assert state.deadline()==deadline
    state.invalidate_samples()
    assert state.conflict_since==10.1
    assert state.recovery_reason(deadline)=='runtime_identity_conflict_unresolved'


def test_recovery_needs_two_matches_and_does_not_accept_single_match():
    state=verified()
    state.accept(observation(10,B))
    state.accept(observation(12))
    assert state.conflict_since is not None
    state.accept(observation(13.2))
    assert state.conflict_since is None and state.status==Status.VERIFIED


def test_starved_probe_expires_without_future_and_override_remains_unconfirmed():
    state=verified()
    assert state.recovery_reason(30)=='runtime_identity_unavailable'
    state=RuntimeIdentity(A,UID,True)
    state.accept(observation(0,None))
    state.accept(observation(31,None))
    assert state.next_at==41.1 and not state.recovery_reason(100)
    assert state.context(100)['room_identity_stale']


def test_readonly_probe_checks_context_and_never_touches_navigation_state():
    nav=WorkerNavigator.__new__(WorkerNavigator)
    nav._operation_deadline=123
    live='topResumedActivity=ActivityRecord{aa u0 com.ss.android.ugc.aweme/.live.LivePlayActivity t9}'
    dump=f'TASK app id=9\n  ACTIVITY com.ss.android.ugc.aweme/.live.LivePlayActivity aa\n mArguments=Bundle[{{live_backend_roomid={A}, is_LiveRoomFragment=true, live.intent.extra.USER_ID={UID}}}]'
    replies=iter([live,dump,live])
    calls=[]
    def run(*args,**kw):
        calls.append((args,kw))
        return SimpleNamespace(exit_code=0,stdout=next(replies))
    nav.adb_client=SimpleNamespace(run_adb=run)
    result=nav.inspect_live_identity(time.monotonic()+3)
    assert result.evidence.room_id==A and nav._operation_deadline==123
    assert all(args[:3]==('shell','dumpsys','activity') for args,kw in calls)
    assert calls[1][0][-1]=='com.ss.android.ugc.aweme/.live.LivePlayActivity'
    replies=iter([live,dump,live.replace('aa u0','bb u0')])
    assert nav.inspect_live_identity(time.monotonic()+3).evidence is None
    replies=iter([live,dump.replace('id=9','id=8'),live])
    assert nav.inspect_live_identity(time.monotonic()+3).evidence is None
    replies=iter([live,dump.replace('LivePlayActivity aa','LivePlayActivity bb'),live])
    assert nav.inspect_live_identity(time.monotonic()+3).evidence is None


def test_same_activity_wrong_room_is_recovered_and_finished_slice_backmarked(monkeypatch,tmp_path,valid_media):
    from worker_harness import build_worker, assert_paired
    h=build_worker(monkeypatch,tmp_path,valid_media,total=45,segment=2)
    original=h.navigator_type.inspect_live_identity
    injected=[False]
    def inspect(self,deadline,stop_requested=lambda:False):
        o=original(self,deadline,stop_requested)
        if h.pipelines and len(h.pipelines)==1 and not h.pipelines[0].stopped and h.pipelines[0].span()>12:
            injected[0]=True
            return IdentityObservation(RoomEvidence(B,'900002',source='live_fragment_arguments'),'',o.started,o.finished,o.wall_started)
        return o
    monkeypatch.setattr(h.navigator_type,'inspect_live_identity',inspect)
    assert h.controller.run()==0, (h.controller._failure_reason,[(e['event'],e.get('reason'),e.get('action')) for e in h.events() if e['event'] in ('stall_detected','worker_failed','room_identity_check')])
    events=h.events();assert_paired(events)
    assert injected[0] and len(h.pipelines)==2
    assert any(e['event']=='stall_detected' and e['reason']=='runtime_identity_mismatch' for e in events)
    risks=[e for e in events if e['event']=='segment_quality_warning' and e['flag']=='identity_mismatch']
    assert risks
    assert all(e['room_identity_status']=='unconfirmed' for e in risks)
    assert any(any(e['event']=='segment_finished' and e['segment_index']==r['segment_index']
                   for e in events[:events.index(r)]) for r in risks)


def test_profile_success_but_runtime_probe_unavailable_stops_before_capture(monkeypatch,tmp_path,valid_media):
    from worker_harness import build_worker
    h=build_worker(monkeypatch,tmp_path,valid_media)
    monkeypatch.setattr(h.navigator_type,'inspect_live_identity',lambda self,*a:
                        IdentityObservation(None,'room_evidence_unavailable',h.clock.now,h.clock.now,h.clock.time()))
    assert h.controller.run()==9
    assert not h.pipelines
    assert not any(e['event']=='segment_started' for e in h.events())
    assert h.events()[-1]['reason']=='runtime_identity_probe_unavailable'


def test_short_missing_then_matches_closes_risk_boundary_once(monkeypatch,tmp_path,valid_media):
    from worker_harness import build_worker,assert_paired
    h=build_worker(monkeypatch,tmp_path,valid_media,total=35,segment=2)
    original=h.navigator_type.inspect_live_identity
    missing=[False]
    def inspect(self,*args):
        o=original(self,*args)
        if h.pipelines and h.pipelines[0].span()>11 and not missing[0]:
            missing[0]=True
            return IdentityObservation(None,'room_evidence_unavailable',o.started,o.finished,o.wall_started)
        return o
    monkeypatch.setattr(h.navigator_type,'inspect_live_identity',inspect)
    assert h.controller.run()==0
    events=h.events();assert_paired(events)
    assert len(h.pipelines)==2
    assert sum(e['event']=='stall_detected' and e['reason']=='identity_risk_boundary_reset' for e in events)==1
    risks=[e for e in events if e['event']=='segment_quality_warning' and e['flag']=='identity_unconfirmed']
    assert risks and all(e['segment_index']<h.pipelines[1].base for e in risks)


def test_aged_landing_rechecks_before_first_recording_event(monkeypatch,tmp_path,valid_media):
    from worker_harness import build_worker
    h=build_worker(monkeypatch,tmp_path,valid_media,total=6)
    def slow_picture(self,**kwargs):
        h.clock.sleep(31)
        return True
    monkeypatch.setattr(h.navigator_type,'wait_live_picture',slow_picture)
    assert h.controller.run()==0
    events=h.events()
    start=next(i for i,e in enumerate(events) if e['event']=='capture_session' and e['action']=='started')
    assert any(e['event']=='room_identity_check' and e['action']=='recovered' for e in events[:start])
    assert not any(e['event']=='stall_detected' for e in events)
    assert len(h.pipelines)==1


@pytest.mark.parametrize('delay,epochs',[(20,1),(31,2)])
def test_slow_capture_start_marks_preparation_media_and_rebuilds_once(monkeypatch,tmp_path,valid_media,delay,epochs):
    from dataclasses import replace
    from worker_harness import build_worker,assert_paired
    h=build_worker(monkeypatch,tmp_path,valid_media,total=40,segment=20)
    observe=h.navigator_type.observe_live_room
    monkeypatch.setattr(h.navigator_type,'observe_live_room',lambda self,**kw:
                        replace(observe(self,**kw),source='live_fragment_arguments'))
    start=h.pipeline_type.start
    def slow_start(self):
        start(self)
        if self.epoch==1:h.clock.sleep(delay)
    monkeypatch.setattr(h.pipeline_type,'start',slow_start)
    assert h.controller.run()==0
    events=h.events();assert_paired(events)
    assert len(h.pipelines)==epochs
    assert not any(e['event']=='stall_detected' for e in events)
    risks=[e for e in events if e['event']=='segment_quality_warning' and e['flag']=='identity_unconfirmed']
    assert not risks
    if epochs==2:
        discarded=[e for e in events if e['event']=='capture_startup' and e['action']=='discarded']
        assert len(discarded)==1 and discarded[0]['reason']=='startup_identity_risk_boundary_reset'
        assert discarded[0]['room_identity_status']=='unconfirmed'
        assert sum(e['event']=='capture_session' and e['action']=='started' for e in events)==1


def test_conflict_then_capture_crash_upgrades_to_single_relocation(monkeypatch,tmp_path,valid_media):
    from worker_harness import build_worker,assert_paired
    h=build_worker(monkeypatch,tmp_path,valid_media,total=28,segment=2)
    inspect=h.navigator_type.inspect_live_identity
    def wrong(self,*args):
        o=inspect(self,*args)
        if h.pipelines and len(h.pipelines)==1 and not h.pipelines[0].stopped and h.pipelines[0].span()>10:
            return IdentityObservation(RoomEvidence(B,'900002',source='live_fragment_arguments'),'',o.started,o.finished,o.wall_started)
        return o
    monkeypatch.setattr(h.navigator_type,'inspect_live_identity',wrong)
    alive=h.pipeline_type.is_recording
    def crash(self):
        if self.alive and self.epoch==1 and h.controller.runtime_identity.conflict_since is not None:
            self._end(9)
        return alive(self)
    monkeypatch.setattr(h.pipeline_type,'is_recording',crash)
    assert h.controller.run()==0
    events=h.events();assert_paired(events)
    recoveries=[e for e in events if e['event']=='stall_detected']
    assert len(recoveries)==1 and recoveries[0]['action']=='relocate_target'
    assert recoveries[0]['reason']=='runtime_identity_conflict_unresolved'


def test_old_generation_result_cannot_refresh_identity_or_trigger_recovery(tmp_path):
    from concurrent.futures import Future
    from src.worker.controller import WorkerController,WorkerConfig
    from src.worker.continuous_session import ContinuousSession
    import io
    from src.worker.events import WorkerEventEmitter
    c=WorkerController(WorkerConfig(vmindex=999,target=A,target_kind='room_id',expected_anchor_uid=UID,
        output_dir=tmp_path),WorkerEventEmitter(io.StringIO()))
    c.target_room_id,c.target_uid=A,UID
    c.runtime_identity=verified()
    s=ContinuousSession(c,'serial','scrcpy','adb',SimpleNamespace(),clock=lambda:5)
    s.epoch=2;s.identity_token=2
    s.identity_request=(1,1,0)
    s.identity_future=Future();s.identity_future.set_result(observation(4,B))
    try:
        s.poll_identity(stopping=True)
        assert s.identity.confirmed_mono==0 and s.identity.conflict_since is None
    finally:
        s.identity_probes.shutdown();s.probes.shutdown();s.pool.shutdown()


def test_overlay_at_age_29_exits_at_identity_deadline_without_nested_recovery(tmp_path):
    from concurrent.futures import Future
    from src.worker.controller import WorkerController,WorkerConfig
    from src.worker.continuous_session import ContinuousSession
    from src.worker.state_machine import WorkerStateMachine,WorkerState
    from src.worker.events import WorkerEventEmitter
    import io
    now=[29.0]
    c=WorkerController(WorkerConfig(vmindex=999,target=A,target_kind='room_id',expected_anchor_uid=UID,
        output_dir=tmp_path),WorkerEventEmitter(io.StringIO()))
    c.target_room_id,c.target_uid=A,UID;c.current_generation_id=1
    c.runtime_identity=verified()
    c.state_machine=WorkerStateMachine(WorkerState.RECORDING)
    s=ContinuousSession(c,'serial','scrcpy','adb',SimpleNamespace(),clock=lambda:now[0])
    inside=[False]
    def dismiss(deadline,cancel,**kw):
        inside[0]=True
        assert deadline==30
        now[0]=30
        assert cancel()
        inside[0]=False
        return False
    c._navigator=SimpleNamespace(dismiss_live_overlay=dismiss)
    s.pipe=SimpleNamespace(media_time=lambda:29);s.epoch=1;s.last_surface=29
    s.sync=lambda **kw:None
    recoveries=[]
    def recover(reason,relocate):
        assert not inside[0]
        recoveries.append((reason,relocate))
    s.recover=recover
    s.surface_future=Future();s.surface_gen=1
    s.surface_future.set_result(dict(activity='LivePlayActivity',overlay='live_panel_window',ui_available=True))
    try:
        s.poll_surface()
        assert recoveries==[('runtime_identity_unavailable',True)]
        assert c.identity_status==Status.UNCONFIRMED
    finally:
        s.identity_probes.shutdown();s.probes.shutdown();s.pool.shutdown()


def test_reset_or_missing_pts_backmarks_entire_epoch_even_after_finished(tmp_path):
    from src.worker.controller import WorkerController,WorkerConfig
    from src.worker.continuous_session import ContinuousSession
    from src.worker.events import WorkerEventEmitter
    import io,json
    stream=io.StringIO()
    c=WorkerController(WorkerConfig(vmindex=999,target=A,target_kind='room_id',expected_anchor_uid=UID,
        output_dir=tmp_path),WorkerEventEmitter(stream))
    c.target_room_id,c.target_uid=A,UID;c.runtime_identity=verified()
    s=ContinuousSession(c,'serial','scrcpy','adb',SimpleNamespace(),clock=lambda:25)
    media=[25]
    s.pipe=SimpleNamespace(media_time=lambda:media[0]);s.epoch=1
    s.identity_checkpoint=(1,20)
    s.ledger={1:dict(epoch=1,closed=True,start_pts=0,end_pts=10,terminal=True),
              2:dict(epoch=1,closed=True,start_pts=10,end_pts=30,terminal=True)}
    try:
        s.mark_identity_risk()
        rows=lambda:[json.loads(line) for line in stream.getvalue().splitlines()]
        assert [e['segment_index'] for e in rows()]==[2]
        media[0]=5
        s.mark_identity_risk()
        assert [e['segment_index'] for e in rows()]==[2,1]
        s.ledger[3]=dict(epoch=1,closed=True,start_pts=None,end_pts=None,terminal=True)
        s.apply_identity_risks()
        assert [e['segment_index'] for e in rows()]==[2,1,3]
    finally:
        s.identity_probes.shutdown();s.probes.shutdown();s.pool.shutdown()

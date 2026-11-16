"""从 Controller 驱动启动门禁，媒体仍经过真实封口校验。"""
import pytest

from worker_harness import build_worker, assert_paired


@pytest.mark.parametrize('bad_probe,phase', [(1,'before_capture'),(2,'capture_starting'),(3,'before_recording')])
def test_late_login_returns_to_navigation_without_runtime_recovery(monkeypatch,tmp_path,valid_media,bad_probe,phase):
    h=build_worker(monkeypatch,tmp_path,valid_media,total=1)
    calls=[]
    original=h.navigator_type.inspect_live_surface
    def surface(nav,**kw):
        calls.append(kw)
        result=original(nav,**kw)
        if len(calls)==bad_probe:result['activity']='com.ss.android.ugc.aweme.DYLoginActivity'
        return result
    monkeypatch.setattr(h.navigator_type,'inspect_live_surface',surface)
    start=h.pipeline_type.start
    def start_with_media(pipe):
        start(pipe)
        pipe.fragments()  # 最终门禁拒绝时，已经写出的临时媒体也不能冒充正式切片。
    monkeypatch.setattr(h.pipeline_type,'start',start_with_media)
    assert h.controller.run()==0
    events=h.events()
    rejected=[e for e in events if e['event']=='capture_startup' and e['action']=='rejected']
    assert len(rejected)==1 and rejected[0]['phase']==phase
    assert rejected[0]['reason']=='login_prompt_detected' and 'DYLoginActivity' in rejected[0]['focus_activity']
    assert len([e for e in events if e['event']=='room_navigation' and e['action']=='target_ready'])==2
    assert not h.controller._recovery_times
    assert not any(e['event'] in ('stall_detected','stall_healed','segment_aborted') for e in events)
    assert all(e['event']=='segment_finished' for e in assert_paired(events))
    assert not events[-1]['had_segment_errors']
    accepted=[e for e in events if e['event']=='capture_session' and e['action']=='started']
    assert len(accepted)==1 and accepted[0]['capture_launch_at']>0 and accepted[0]['first_media_observed_at']>0
    assert all(p.stopped for p in h.pipelines)
    if bad_probe==3:
        assert list(h.pipelines[0].directory.glob('*.mp4'))
        assert all(str(h.pipelines[0].directory) not in e['file_path'] for e in events if e['event']=='segment_started')


def test_startup_rechecks_during_wait_not_only_at_entry(monkeypatch,tmp_path,valid_media):
    h=build_worker(monkeypatch,tmp_path,valid_media,total=1)
    original=h.navigator_type.inspect_live_surface
    def surface(nav,**kw):
        result=original(nav,**kw)
        if h.pipelines and h.pipelines[-1].alive and h.pipelines[-1].epoch==1 and h.clock.monotonic()-h.pipelines[-1].begin>=.5:
            result['activity']='com.ss.android.ugc.aweme.DYLoginActivity'
        return result
    monkeypatch.setattr(h.navigator_type,'inspect_live_surface',surface)
    start=h.pipeline_type.start
    def slow_start(pipe):
        start(pipe)
        h.clock.sleep(.6)
        try:pipe.wait_hook()
        except BaseException:
            pipe.stop()
            raise
    monkeypatch.setattr(h.pipeline_type,'start',slow_start)
    assert h.controller.run()==0
    assert len(h.pipelines)==2 and all(p.stopped for p in h.pipelines)
    assert any(e['event']=='capture_startup' and e['action']=='rejected' and e['phase']=='capture_starting' for e in h.events())
    assert not h.controller._recovery_times


@pytest.mark.parametrize('observation,reason', [({},'surface_probe_unavailable'),
    ({'activity':'com.ss.android.ugc.aweme.DYLoginActivity','ui_available':True},'login_prompt_detected'),
    ({'activity':'com.ss.android.ugc.aweme.LivePlayActivity','ui_available':True,'overlay':'profile_card'},'startup_overlay_detected')])
def test_startup_gate_exhaustion_is_bounded_and_never_records(monkeypatch,tmp_path,valid_media,observation,reason):
    h=build_worker(monkeypatch,tmp_path,valid_media,total=1)
    monkeypatch.setattr(h.navigator_type,'inspect_live_surface',lambda *a,**kw:observation)
    assert h.controller.run()==30
    assert h.controller._failure_reason=='startup_gate_exhausted: '+reason
    assert len([e for e in h.events() if e['event']=='capture_startup' and e['action']=='rejected'])==3
    assert not h.pipelines and not h.controller._recovery_times
    assert not any(e['event']=='segment_started' for e in h.events())


def test_stop_during_final_gate_discards_preparation_without_reopening(monkeypatch,tmp_path,valid_media):
    h=build_worker(monkeypatch,tmp_path,valid_media,total=1)
    calls=[]
    original=h.navigator_type.inspect_live_surface
    def surface(nav,**kw):
        calls.append(1)
        if len(calls)==3:h.controller.request_stop()
        return original(nav,**kw)
    monkeypatch.setattr(h.navigator_type,'inspect_live_surface',surface)
    assert h.controller.run()==0
    assert len(h.pipelines)==1 and h.pipelines[0].stopped
    assert not any(e['event']=='segment_started' for e in h.events())
    assert any(e['event']=='capture_startup' and e['action']=='discarded' for e in h.events())


def test_identity_mismatch_after_startup_rejection_still_blocks(monkeypatch,tmp_path,valid_media):
    h=build_worker(monkeypatch,tmp_path,valid_media,total=1)
    def surface(nav,**kw):
        from src.worker.room_locator import RoomEvidence
        monkeypatch.setattr(h.navigator_type,'observe_live_room',lambda *a,**kw:RoomEvidence(anchor_uid='456'))
        return {'activity':'com.ss.android.ugc.aweme.DYLoginActivity','ui_available':True}
    monkeypatch.setattr(h.navigator_type,'inspect_live_surface',surface)
    assert h.controller.run()==18
    assert not h.pipelines
    assert not any(e['event']=='segment_started' for e in h.events())

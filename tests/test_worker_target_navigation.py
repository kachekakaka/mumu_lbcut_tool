"""实际 Controller.run + 实际定位器：仅替换 Android/媒体进程边界。"""
from src.worker.room_locator import RoomEvidence
from src.worker.state_machine import ActionType, PipelineAction
from worker_harness import build_worker, assert_paired


def test_worker_corrects_other_room_then_records(monkeypatch,tmp_path,valid_media):
    import src.worker.controller as cm
    h=build_worker(monkeypatch,tmp_path,valid_media,uid=None)
    seen=[]
    observations=iter([RoomEvidence('999','wrong','其他主播','fresh_profile_intent'),
                       RoomEvidence('7704666294802123554','900001','目标主播','fresh_profile_intent')])
    monkeypatch.setattr(cm.WorkerNavigator,'observe_live_room',lambda self,**kw: next(observations))
    monkeypatch.setattr(cm.WorkerNavigator,'navigate_route',lambda self,t,w,route,d,stop: seen.append(route) or True)
    assert h.controller.run()==0
    assert seen==['primary','reset']
    assert h.launched[:1]==[1]
    assert_paired(h.events())
    assert any(e['event']=='room_navigation' and e['action']=='correct_wrong_room' for e in h.events())
    assert any(e['event']=='room_navigation' and e['action']=='target_ready'
               and e['observed_room_id']=='7704666294802123554' for e in h.events())


def test_room_evidence_starts_real_control_flow_without_ui_uid_or_escape(monkeypatch,tmp_path,valid_media):
    import src.worker.controller as cm
    h=build_worker(monkeypatch,tmp_path,valid_media,uid=None,expected_uid=None)
    monkeypatch.setattr(cm.WorkerNavigator,'observe_live_room',lambda self,**kw:
                        RoomEvidence(room_id='7704666294802123554',source='fresh_profile_intent'))
    assert h.controller.config.allow_unconfirmed_identity is False
    assert h.controller.run()==0
    assert_paired(h.events())
    assert all(e['room_identity_status']=='verified' for e in h.events() if e['event']=='segment_finished')


def test_runtime_relocation_closes_capture_before_opening_profile(monkeypatch,tmp_path,valid_media):
    import src.worker.controller as cm
    h=build_worker(monkeypatch,tmp_path,valid_media,total=5,segment=30)
    calls=[]
    def observe(self,**kwargs):
        assert not any(p.alive for p in h.pipelines), '取证操作不能录入当前录像'
        calls.append(True)
        return RoomEvidence('7704666294802123554','900001',source='fresh_profile_intent')
    monkeypatch.setattr(cm.WorkerNavigator,'observe_live_room',observe)
    posted=[False]
    def drift(now):
        if now>=1001 and not posted[0]:
            posted[0]=True
            h.controller.action_dispatcher.post_action(PipelineAction(ActionType.IN_PLACE_HEAL,
                h.controller.current_generation_id,'runtime_left_live_page'))
    h.clock.on_sleep=drift
    assert h.controller.run()==0
    assert len(calls)==2 and h.launched==[1,2]
    terminals=assert_paired(h.events())
    assert [e['event'] for e in terminals]==['segment_aborted','segment_finished']
    assert h.controller._total_crash_count==1


def test_cancel_during_target_navigation_never_starts_media(monkeypatch,tmp_path,valid_media):
    import src.worker.controller as cm
    h=build_worker(monkeypatch,tmp_path,valid_media)
    def navigate(self,*args):
        h.controller.request_stop()
        return False
    monkeypatch.setattr(cm.WorkerNavigator,'navigate_route',navigate)
    assert h.controller.run()==0
    assert not h.launched

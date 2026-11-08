"""实际页面驱动的定位/纠偏合同；不把目标 Intent 当作实际房间证据。"""
from src.worker.room_locator import RoomEvidence, fresh_profile_evidence, current_live_fragment_evidence, compare_evidence, locate_target
from src.worker.identity_verifier import IdentityStatus
from src.worker.navigator import WorkerNavigator


def dump(token='new', task='41', uid='123', room='900', previous='live'):
    return f'''topResumedActivity=ActivityRecord{{login u0 com.ss.android.ugc.aweme/.account.business.login.DYLoginActivity t{task}}}
 * Hist #3: ActivityRecord{{login u0 com.ss.android.ugc.aweme/.account.business.login.DYLoginActivity t{task}}}
   Intent {{ cmp=com.ss.android.ugc.aweme/.account.business.login.DYLoginActivity (has extras) }}
 * Hist #2: ActivityRecord{{{token} u0 com.ss.android.ugc.aweme/.profile.ui.UserProfileActivity t{task}}}
   Intent {{ dat=aweme://user/profile?uid={uid}&room_id={room}&anchor_id={uid}&previous_page={previous} flg=0x10000000 }}
 * Hist #1: ActivityRecord{{live u0 com.ss.android.ugc.aweme/.live.LivePlayActivity t{task}}}
   Intent {{ cmp=com.ss.android.ugc.aweme/.live.LivePlayActivity (has extras) }}'''


def test_new_local_profile_intent_proves_uid_and_room_without_visible_uid():
    evidence = fresh_profile_evidence('', dump(), '41', '样例主播')
    assert evidence.anchor_uid == '123' and evidence.room_id == '900'
    assert evidence.source == 'fresh_profile_intent'
    assert compare_evidence(evidence, '900', '123') == IdentityStatus.VERIFIED


def test_old_profile_other_task_and_non_live_origin_are_not_evidence():
    assert fresh_profile_evidence(dump(), dump(), '41') is None
    assert fresh_profile_evidence('', dump(task='42'), '41') is None
    assert fresh_profile_evidence('', dump(previous='feed'), '41') is None
    assert fresh_profile_evidence('', 'Intent { dat=snssdk1128://live?room_id=900 }', '41') is None



def live_top(room='900', uid='123', source_room='777', include_room_arg=True, unique=True):
    room_arg = f'live.intent.extra.ROOM_ID={room},' if include_room_arg else ''
    unique_arg = f'live.intent.extra.EXTRA_ROOM_UNIQUE_KEY={room}_2,' if unique else ''
    return ('topResumedActivity=ActivityRecord{live u0 com.ss.android.ugc.aweme/.live.LivePlayActivity t41}\n'
            '* Hist #1: ActivityRecord{live u0 com.ss.android.ugc.aweme/.live.LivePlayActivity t41}\n'
            f'mArguments=Bundle[{{live.intent.extra.FROM_ROOM_ID={source_room}, live_backend_roomid={room}, '
            f'live.intent.extra.USER_ID={uid}, {room_arg} {unique_arg} is_LiveRoomFragment=true, '
            'live.intent.extra.EXTRA_COMMON_LOG=Bundle[{}]}]')


def test_current_live_fragment_arguments_prove_room_and_uid_without_profile_page():
    evidence = current_live_fragment_evidence(live_top(), 'com.ss.android.ugc.aweme.live.LivePlayActivity')
    assert evidence.room_id == '900' and evidence.anchor_uid == '123'
    assert evidence.source == 'live_fragment_arguments'


def test_current_fragment_ignores_from_room_id_and_accepts_missing_room_id_extra():
    evidence = current_live_fragment_evidence(live_top(room='901', source_room='900', include_room_arg=False, unique=False),
                                              'com.ss.android.ugc.aweme.live.LivePlayActivity')
    assert evidence.room_id == '901' and evidence.anchor_uid == '123'


def test_current_fragment_rejects_conflicting_room_or_fragment_copies():
    bad_room_arg = live_top().replace('live.intent.extra.ROOM_ID=900', 'live.intent.extra.ROOM_ID=901')
    assert current_live_fragment_evidence(bad_room_arg, 'com.ss.android.ugc.aweme.live.LivePlayActivity') is None
    conflicting = live_top() + '\n' + live_top(room='901', uid='456')
    assert current_live_fragment_evidence(conflicting, 'com.ss.android.ugc.aweme.live.LivePlayActivity') is None


def test_current_fragment_requires_uid_and_live_activity():
    assert current_live_fragment_evidence(live_top(uid='0'), 'com.ss.android.ugc.aweme.live.LivePlayActivity') is None
    assert current_live_fragment_evidence(live_top(), 'com.ss.android.ugc.aweme.main.MainActivity') is None

def test_shared_nickname_does_not_hide_mismatching_stable_identity():
    evidence = RoomEvidence('901','124','same name','fresh_profile_intent')
    assert compare_evidence(evidence,'900','123') == IdentityStatus.MISMATCH
    assert compare_evidence(RoomEvidence(nickname='same name'),'900','123') == IdentityStatus.UNCONFIRMED


class NavigationFixture:
    last_gate_reason='navigation_failed'
    def __init__(self, observations, pages=None):
        self.observations = iter(observations); self.pages=iter(pages or [True]*10)
        self.routes=[]; self.reads=0; self.ready_checks=0
    def navigate_route(self,target,web,route,deadline,stop_requested):
        self.routes.append(route); return True
    def wait_live_page(self,**kwargs): return next(self.pages)
    def observe_live_room(self,**kwargs): self.reads+=1; return next(self.observations)
    def wait_live_picture(self,**kwargs): self.ready_checks+=1; return True


def test_wrong_room_is_corrected_instead_of_only_rejected():
    nav=NavigationFixture([RoomEvidence('901','124'),RoomEvidence('900','123')])
    steps=[]
    result=locate_target(nav,'900',target_kind='room_id',web_rid='88',anchor_uid='123',on_step=lambda **kw: steps.append(kw))
    assert result.success and result.attempts==2
    assert nav.routes==['primary','reset']
    assert any(s['action']=='correct_wrong_room' for s in steps)
    assert nav.ready_checks==1  # 错房不做重复画面检查，只检查最终正确直播。


def test_intent_accepted_but_home_page_changes_route():
    nav=NavigationFixture([RoomEvidence('900','123')],pages=[False,True])
    result=locate_target(nav,'900',target_kind='room_id',web_rid='88')
    assert result.success and nav.routes==['primary','web']


def test_unavailable_uid_uses_fresh_room_proof_not_escape_flag():
    nav=NavigationFixture([RoomEvidence(room_id='900',source='fresh_profile_intent')])
    result=locate_target(nav,'900',target_kind='room_id')
    assert result.success and result.status==IdentityStatus.VERIFIED


def test_no_evidence_tries_all_routes_then_reports_real_reason():
    nav=NavigationFixture([RoomEvidence()]*6)
    result=locate_target(nav,'900',target_kind='room_id',web_rid='88')
    assert not result.success and result.reason=='room_evidence_unavailable'
    assert nav.routes==['primary','web','reset']
    assert nav.reads==6


def test_stop_prevents_navigation_and_recording():
    nav=NavigationFixture([])
    result=locate_target(nav,'900',stop_requested=lambda: True)
    assert not result.success and result.reason=='cancelled' and not nav.routes


def test_real_navigator_exposes_active_observation_and_correction():
    assert callable(getattr(WorkerNavigator,'observe_live_room',None))
    assert callable(getattr(WorkerNavigator,'navigate_route',None))


def test_ui_dump_accepts_complete_xml_when_mumu_uiautomator_exits_139():
    from src.worker.popup_bypasser import PopupBypasser
    from types import SimpleNamespace
    calls=[]
    class Adb:
        def run_adb(self,*args,**kwargs):
            calls.append((args,kwargs))
            if args[:3] == ("shell","uiautomator","dump"):
                assert len(args)==4 and args[3].startswith("/sdcard/wp_ui_")
                assert kwargs.get("check") is False
                return SimpleNamespace(exit_code=139,stdout="UI hierarchy dumped",stderr="Segmentation fault")
            if args[:2] == ("shell","cat"):
                assert args[2] == next(c[0][3] for c in reversed(calls) if c[0][:3] == ("shell","uiautomator","dump"))
                return SimpleNamespace(exit_code=0,stdout='<?xml version="1.0"?><hierarchy><node text="当前页面"/></hierarchy>',stderr="")
            if args[:3] == ("shell","rm","-f"):
                return SimpleNamespace(exit_code=0,stdout="",stderr="")
            raise AssertionError(args)
    probe=PopupBypasser(Adb())
    assert "当前页面" in probe.get_ui_dump()
    assert "当前页面" in probe.get_ui_dump()
    paths=[c[0][3] for c in calls if c[0][:3] == ("shell","uiautomator","dump")]
    assert len(set(paths))==2
    assert sum(c[0][:3] == ("shell","rm","-f") for c in calls)==2


def test_ui_dump_failure_never_reads_old_fixed_file():
    from src.worker.popup_bypasser import PopupBypasser
    from types import SimpleNamespace
    calls=[]
    class Adb:
        def run_adb(self,*args,**kwargs):
            calls.append(args)
            if args[:3] == ("shell","uiautomator","dump"):
                return SimpleNamespace(exit_code=139,stdout="",stderr="Segmentation fault")
            if args[:2] == ("shell","cat"):
                return SimpleNamespace(exit_code=1,stdout="",stderr="No such file")
            return SimpleNamespace(exit_code=0,stdout="",stderr="")
    assert PopupBypasser(Adb()).get_ui_dump()==""
    assert any(x[:2] == ("shell","cat") for x in calls)
    assert any(x[:3] == ("shell","rm","-f") for x in calls)


def test_ui_dump_timeout_returns_empty_and_cleans_unique_path():
    from src.worker.popup_bypasser import PopupBypasser
    calls=[]
    class Adb:
        def run_adb(self,*args,**kwargs):
            calls.append(args)
            if args[:3] == ("shell","uiautomator","dump"):
                raise TimeoutError("dump timeout")
            return type("R",(),{"exit_code":0,"stdout":"","stderr":""})()
    assert PopupBypasser(Adb()).get_ui_dump()==""
    assert len([x for x in calls if x[:3] == ("shell","rm","-f")])==1


def test_profile_proof_accepts_full_component_but_not_other_app():
    full=dump().replace('/.profile.ui.UserProfileActivity','/com.ss.android.ugc.aweme.profile.ui.UserProfileActivity')
    assert fresh_profile_evidence('',full,'41').anchor_uid=='123'
    wrong=dump().replace('com.ss.android.ugc.aweme/.profile.ui.UserProfileActivity','other.app/.profile.ui.UserProfileActivity')
    assert fresh_profile_evidence('',wrong,'41') is None


def test_conflicting_uid_and_room_cannot_be_verified():
    assert compare_evidence(RoomEvidence('900','456'),'900','123')==IdentityStatus.MISMATCH
    assert compare_evidence(RoomEvidence('901','123'),'900','123')==IdentityStatus.MISMATCH


def test_live_page_settles_before_repeating_navigation():
    nav=NavigationFixture([RoomEvidence(),RoomEvidence('900','123')])
    result=locate_target(nav,'900',target_kind='room_id',anchor_uid='123')
    assert result.success and result.attempts==1
    assert nav.routes==['primary'] and nav.reads==2


def test_actual_header_entry_excludes_chat_user_and_follow_control():
    import xml.etree.ElementTree as ET
    tree=ET.fromstring("""<hierarchy>
      <node package="com.ss.android.ugc.aweme" resource-id="com.ss.android.ugc.aweme:id/apz" class="android.widget.Button" clickable="true" bounds="[36,98][495,206]">
        <node package="com.ss.android.ugc.aweme" resource-id="com.ss.android.ugc.aweme:id/avatar_container" bounds="[36,98][144,206]" />
        <node package="com.ss.android.ugc.aweme" resource-id="com.ss.android.ugc.aweme:id/user_name" bounds="[150,109][348,161]" />
      </node>
      <node package="com.ss.android.ugc.aweme" resource-id="com.ss.android.ugc.aweme:id/user_name" bounds="[36,1606][452,1678]" />
      <node package="com.ss.android.ugc.aweme" resource-id="com.ss.android.ugc.aweme:id/follow" bounds="[360,110][483,194]" />
    </hierarchy>""")
    assert WorkerNavigator._anchor_entry_point(tree,1080,1920)==(90,152)

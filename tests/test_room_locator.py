"""实际页面驱动的定位/纠偏合同；不把目标 Intent 当作实际房间证据。"""
from src.worker.room_locator import RoomEvidence, fresh_profile_evidence, compare_evidence, locate_target
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
    assert nav.ready_checks==3


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


def test_ui_dump_accepts_valid_new_xml_after_mumu_exit139():
    from src.worker.popup_bypasser import PopupBypasser
    from types import SimpleNamespace
    calls=[]
    class Adb:
        def run_adb(self,*args,**kwargs):
            calls.append(args)
            if args[1]=='uiautomator':
                return SimpleNamespace(exit_code=139,stdout='UI hierchary dumped',stderr='')
            return SimpleNamespace(exit_code=0,stdout='<hierarchy><node text="当前页面"/></hierarchy>',stderr='')
    probe=PopupBypasser(Adb())
    assert '当前页面' in probe.get_ui_dump()
    assert '当前页面' in probe.get_ui_dump()
    dumps=[a[-1] for a in calls if a[1]=='uiautomator']
    assert len(set(dumps))==2
    assert all(path.startswith('/sdcard/wp_ui_') for path in dumps)
    assert {a[-1] for a in calls if a[1]=='cat'}==set(dumps)


def test_ui_dump_failure_never_reads_old_fixed_file():
    from src.worker.popup_bypasser import PopupBypasser
    from types import SimpleNamespace
    calls=[]
    class Adb:
        def run_adb(self,*args,**kwargs):
            calls.append(args)
            if args[1]=='uiautomator':raise TimeoutError('synthetic device busy')
            return SimpleNamespace(exit_code=0,stdout='',stderr='')
    assert PopupBypasser(Adb()).get_ui_dump()==''
    assert not any('window_dump.xml' in str(a) for a in calls)
    assert not any(a[1]=='cat' for a in calls)


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
      <node package="com.ss.android.ugc.aweme" resource-id="com.ss.android.ugc.aweme:id/user_name" bounds="[150,109][348,161]" />
      <node package="com.ss.android.ugc.aweme" resource-id="com.ss.android.ugc.aweme:id/user_name" bounds="[36,1606][452,1678]" />
      <node package="com.ss.android.ugc.aweme" resource-id="com.ss.android.ugc.aweme:id/follow" bounds="[360,110][483,194]" />
    </hierarchy>""")
    assert WorkerNavigator._anchor_entry_point(tree,1080,1920)==(249,135)

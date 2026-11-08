"""实机验收补出的边界：只读证据识别与最后一次点击后的确认。"""
import time
import numpy as np
from src.worker.live_overlay import classify_live_overlay
from src.worker.navigator import WorkerNavigator

PACKAGE = 'com.ss.android.ugc.aweme'

def fan_panel(*, title=True, stats=True, close=True):
    values=[]
    if title:values.append(f'<node package="{PACKAGE}" class="android.webkit.WebView" text="粉丝团等级" />')
    if stats:values.append(f'<node package="{PACKAGE}" text="已达到最高等级 | 已累计0团力值" />')
    if close:values.append(f'<node package="{PACKAGE}" resource-id="{PACKAGE}:id/a2k" content-desc="关闭" clickable="true" />')
    return '<hierarchy>'+''.join(values)+'</hierarchy>'


def test_confirmed_fan_club_panel_is_recoverable():
    assert classify_live_overlay(fan_panel()).kind == 'fan_club_panel'


def test_fan_club_labels_without_overlay_structure_do_not_trigger_back():
    assert classify_live_overlay(fan_panel(title=False)) is None
    assert classify_live_overlay(fan_panel(stats=False)) is None
    assert classify_live_overlay(fan_panel(close=False)) is None
    assert classify_live_overlay(f'<hierarchy><node package="{PACKAGE}" text="粉丝团等级 已累计0团力值" /></hierarchy>') is None


def test_final_anchor_click_gets_read_only_confirmation(monkeypatch):
    from src.worker.popup_bypasser import PopupBypasser
    nav=WorkerNavigator.__new__(WorkerNavigator)
    nav._operation_deadline=None;nav.adb_client=object()
    nav._navigation_generation=2;nav._profile_entry_clicked_generation=None
    nav.get_current_focus_activity=lambda:'com.ss.android.ugc.aweme.live.LivePlayActivity'
    live=np.full((1920,1080,3),20,dtype=np.uint8)
    sheet=live.copy();sheet[int(.27*1920):,:,:]=255
    sheet[int(.29*1920):int(.38*1920),int(.07*1080):int(.22*1080),:]=[120,130,140]
    frames=iter([live,sheet]);nav._capture_frame_array=lambda:next(frames)
    nav._navigation_sleep=lambda *args:True
    nav.dismiss_live_overlay=lambda *args:True
    normal=f'<hierarchy><node package="{PACKAGE}" resource-id="{PACKAGE}:id/apz" clickable="true" bounds="[36,98][495,206]"><node package="{PACKAGE}" resource-id="{PACKAGE}:id/avatar_container" bounds="[36,98][144,206]" /><node package="{PACKAGE}" resource-id="{PACKAGE}:id/user_name" bounds="[150,109][348,161]" /></node></hierarchy>'
    monkeypatch.setattr(PopupBypasser,'get_ui_dump',lambda self:normal)
    before='topResumedActivity=ActivityRecord{live u0 com.ss.android.ugc.aweme/.live.LivePlayActivity t42}\n* Hist #0: ActivityRecord{live u0 com.ss.android.ugc.aweme/.live.LivePlayActivity t42}\n'
    after='* Hist #1: ActivityRecord{profile u0 com.ss.android.ugc.aweme/.profile.ui.UserProfileActivity t42}\n Intent { dat=aweme://user/profile?uid=100&room_id=200&anchor_id=100&previous_page=live }\n'+before
    dumps=iter([before,after]);taps=[]
    def adb(*args,**kwargs):
        if args[:4]==('shell','dumpsys','activity','top'):return ''
        if args[:4]==('shell','dumpsys','activity','activities'):return next(dumps)
        if args[:3]==('shell','input','tap'):taps.append(args[3:])
        return ''
    nav._adb=adb
    result=nav.observe_live_room(time.monotonic()+30,lambda:False)
    assert result.anchor_uid=='100' and result.room_id=='200'
    assert len(taps)==2  # one header tap + screenshot-confirmed profile avatar tap


def test_real_gallery_without_accessible_text_is_detected_as_native_sheet():
    from pathlib import Path
    xml=(Path(__file__).parent/'fixtures/live_gallery_native_sheet.xml').read_text(encoding='utf8')
    assert classify_live_overlay(xml).kind=='live_bottom_sheet'


def test_native_sheet_needs_close_mask_and_large_content():
    from pathlib import Path
    import xml.etree.ElementTree as ET
    xml=(Path(__file__).parent/'fixtures/live_gallery_native_sheet.xml').read_text(encoding='utf8')
    root=ET.fromstring(xml)
    for n in root.iter('node'):
        if n.get('resource-id','').endswith('/a2k'):n.set('content-desc','')
    assert classify_live_overlay(ET.tostring(root,encoding='unicode')) is None
    root=ET.fromstring(xml)
    for n in root.iter('node'):
        if n.get('resource-id','').endswith('/a2q'):n.set('bounds','[0,1700][1080,1920]')
    assert classify_live_overlay(ET.tostring(root,encoding='unicode')) is None


def test_white_gallery_loading_is_not_a_confirmed_login():
    from test_worker_watchdog import build_dog
    dog,pipe,nav,actions=build_dog()
    nav._capture_frame_array.return_value=np.full((100,100,3),250,dtype=np.uint8)
    for tick in (0.,1.):
        pipe.get_current_file_size_mb.return_value=10+tick
        dog.check_once(tick)
    assert dog.snapshot().login_blocked is False
    assert not any(a.reason in ('login_prompt_detected','login_blocked') for a in actions)


def test_recovery_reuses_fresh_probe_and_requires_observed_clear():
    nav=WorkerNavigator.__new__(WorkerNavigator)
    nav.get_current_focus_activity=lambda:'LivePlayActivity'
    commands=[];calls=[]
    nav._adb=lambda *args,**kw:commands.append(args) or ''
    nav._navigation_sleep=lambda *args:True
    observations=iter([{'activity':'LivePlayActivity','overlay':None,'ui_available':False},
                       {'activity':'LivePlayActivity','overlay':None,'ui_available':True}])
    def inspect(**kwargs):calls.append(True);return next(observations)
    nav.inspect_live_surface=inspect
    current={'activity':'LivePlayActivity','overlay':'live_bottom_sheet','ui_available':True,'observed_at':time.time()}
    assert nav.dismiss_live_overlay(time.monotonic()+20,lambda:False,
        initial_observation=current,require_observed_clear=True)
    assert len(calls)==2 and commands==[('shell','input','keyevent','4')]


def test_unknown_surface_is_not_successful_runtime_confirmation():
    nav=WorkerNavigator.__new__(WorkerNavigator)
    nav._navigation_sleep=lambda *args:True
    nav.inspect_live_surface=lambda **kw:{'activity':'LivePlayActivity','overlay':None,'ui_available':False}
    nav._adb=lambda *args,**kw:(_ for _ in ()).throw(AssertionError('must not click unknown surface'))
    assert not nav.dismiss_live_overlay(time.monotonic()+10,lambda:False,require_observed_clear=True)


def test_picture_confirmation_can_avoid_duplicate_ui_dump():
    nav=WorkerNavigator.__new__(WorkerNavigator);nav._operation_deadline=None
    nav.get_current_focus_activity=lambda:'LivePlayActivity'
    nav.dismiss_live_overlay=lambda *args:(_ for _ in ()).throw(AssertionError('duplicate UI probe'))
    nav._navigation_sleep=lambda *args:True
    a=np.zeros((100,100,3),dtype=np.uint8);b=a.copy();b[20:40,20:40]=200
    frames=iter([a,b]);nav._capture_frame_array=lambda:next(frames)
    assert nav.wait_live_picture(time.monotonic()+2,lambda:False,check_overlay=False)



def test_final_picture_reuses_fresh_overlay_observation(monkeypatch):
    nav=WorkerNavigator.__new__(WorkerNavigator)
    nav._operation_deadline=None
    cached={"activity":"LivePlayActivity","overlay":None,"ui_available":True,"observed_at":time.time()}
    nav._last_overlay_observation=cached
    nav.get_current_focus_activity=lambda:"LivePlayActivity"
    calls=[]
    def dismiss(deadline,stop,**kwargs):
        calls.append(kwargs.get("initial_observation"))
        return kwargs.get("initial_observation") is cached
    nav.dismiss_live_overlay=dismiss
    nav._navigation_sleep=lambda *args:True
    a=np.zeros((100,100,3),dtype=np.uint8);b=a.copy();b[20:40,20:40]=200
    frames=iter([a,b]);nav._capture_frame_array=lambda:next(frames)
    assert nav.wait_live_picture(time.monotonic()+20,lambda:False)
    assert calls==[cached]
    assert nav._last_overlay_observation is None

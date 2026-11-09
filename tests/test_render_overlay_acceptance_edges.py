"""实机验收补出的边界：只读证据识别与最后一次点击后的确认。"""
import time
import numpy as np
from src.worker.navigator import WorkerNavigator

PACKAGE = 'com.ss.android.ugc.aweme'


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



def test_white_gallery_loading_is_not_a_confirmed_login():
    from test_worker_watchdog import build_dog
    dog,pipe,nav,actions=build_dog()
    pipe.monitor.set_value(250)
    for tick in (0.,1.):
        pipe.get_current_file_size_mb.return_value=10+tick
        dog.check_once(tick)
    assert dog.snapshot().login_blocked is False
    assert not any(a.reason in ('login_prompt_detected','login_blocked') for a in actions)


def test_recovery_reprobes_before_back_and_requires_observed_clear():
    nav=WorkerNavigator.__new__(WorkerNavigator)
    commands=[];calls=[]
    nav._adb=lambda *args,**kw:commands.append(args) or ''
    nav._navigation_sleep=lambda *args:True
    observations=iter([{'activity':'LivePlayActivity','overlay':'live_panel_window','ui_available':True},
                       {'activity':'LivePlayActivity','overlay':None,'ui_available':False},
                       {'activity':'LivePlayActivity','overlay':None,'ui_available':True}])
    def inspect(**kwargs):calls.append(True);return next(observations)
    nav.inspect_live_surface=inspect
    assert nav.dismiss_live_overlay(time.monotonic()+20,lambda:False,require_observed_clear=True)
    assert len(calls)==3 and commands==[('shell','input','keyevent','4')]


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

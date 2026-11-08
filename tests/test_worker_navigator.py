"""Worker 页面导航与 Intent 控制器单元测试。"""
import unittest
from unittest.mock import MagicMock, patch
from src.worker.navigator import WorkerNavigator


class TestWorkerNavigator(unittest.TestCase):
    @patch("src.worker.adb_client.AdbDeviceClient.run_adb")
    def test_launch_live_room(self, mock_run):
        mock_run.side_effect = [
            MagicMock(exit_code=0, stdout="Starting: Intent { act=android.intent.action.VIEW dat=snssdk1128://live/... }", stderr=""),
            MagicMock(exit_code=0, stdout="mCurrentFocus=Window{123 u0 com.ss.android.ugc.aweme/.live.LivePlayActivity}", stderr=""),
            MagicMock(exit_code=0, stdout="mCurrentFocus=Window{123 u0 com.ss.android.ugc.aweme/.live.LivePlayActivity}", stderr=""),
        ]
        nav = WorkerNavigator(serial="127.0.0.1:16448")
        ok = nav.launch_room("737141", wait_sec=0)
        self.assertTrue(ok)

    @patch("src.worker.adb_client.AdbDeviceClient.run_adb")
    def test_launch_room_with_19digit_internal_id(self, mock_run):
        mock_run.side_effect = [
            MagicMock(exit_code=0, stdout="Starting: Intent ...", stderr=""),
            MagicMock(exit_code=0, stdout="mCurrentFocus=Window{123 u0 com.ss.android.ugc.aweme/.live.LivePlayActivity}", stderr=""),
            MagicMock(exit_code=0, stdout="mCurrentFocus=Window{123 u0 com.ss.android.ugc.aweme/.live.LivePlayActivity}", stderr=""),
        ]
        nav = WorkerNavigator(serial="127.0.0.1:16448")
        internal_room_id = "7704666294802123554"  # 19 位真实内部 ID
        ok = nav.launch_room(internal_room_id, wait_sec=0)
        self.assertTrue(ok)
        
        # 验证调用了 snssdk1128 直达 Intent
        first_call_args = mock_run.call_args_list[0][0]
        expected_uri = f"snssdk1128://live?room_id={internal_room_id}"
        self.assertIn(expected_uri, first_call_args)

    @patch("src.worker.adb_client.AdbDeviceClient.run_adb")
    def test_launch_room_with_short_web_rid_fallback(self, mock_run):
        mock_run.side_effect = [
            MagicMock(exit_code=0, stdout="Starting: Intent ...", stderr=""),
            MagicMock(exit_code=0, stdout="mCurrentFocus=Window{123 u0 com.ss.android.ugc.aweme/.live.LivePlayActivity}", stderr=""),
            MagicMock(exit_code=0, stdout="mCurrentFocus=Window{123 u0 com.ss.android.ugc.aweme/.live.LivePlayActivity}", stderr=""),
        ]
        nav = WorkerNavigator(serial="127.0.0.1:16448")
        short_id = "852703780"  # 展示短号
        ok = nav.launch_room(short_id, wait_sec=0)
        self.assertTrue(ok)
        
        # 验证 fallback 到带包名的 Web Intent
        first_call_args = mock_run.call_args_list[0][0]
        expected_web_uri = f"https://live.douyin.com/{short_id}"
        self.assertIn(expected_web_uri, first_call_args)
        self.assertIn("-p", first_call_args)
        self.assertIn("com.ss.android.ugc.aweme", first_call_args)

    @patch("src.worker.adb_client.AdbDeviceClient.run_adb")
    def test_launch_room_with_full_web_url(self, mock_run):
        mock_run.side_effect = [
            MagicMock(exit_code=0, stdout="Starting: Intent ...", stderr=""),
            MagicMock(exit_code=0, stdout="mCurrentFocus=Window{123 u0 com.ss.android.ugc.aweme/.live.LivePlayActivity}", stderr=""),
            MagicMock(exit_code=0, stdout="mCurrentFocus=Window{123 u0 com.ss.android.ugc.aweme/.live.LivePlayActivity}", stderr=""),
        ]
        nav = WorkerNavigator(serial="127.0.0.1:16448")
        full_url = "https://live.douyin.com/852703780"
        ok = nav.launch_room(full_url, wait_sec=0)
        self.assertTrue(ok)
        
        # 验证没有重复拼接前缀
        first_call_args = mock_run.call_args_list[0][0]
        self.assertIn(full_url, first_call_args)
        self.assertIn("-p", first_call_args)
        self.assertIn("com.ss.android.ugc.aweme", first_call_args)

    @patch("src.worker.adb_client.AdbDeviceClient.run_adb")
    def test_refresh_current_room(self, mock_run):
        mock_run.return_value = MagicMock(exit_code=0, stdout="Starting: Intent ...", stderr="")
        nav = WorkerNavigator(serial="127.0.0.1:16448")
        ok = nav.refresh_current_room("737141")
        self.assertTrue(ok)

    @patch("src.worker.navigator.WorkerNavigator._capture_frame_array")
    @patch("src.worker.navigator.WorkerNavigator.get_current_focus_activity")
    def test_preflight_content_gate_success(self, mock_focus, mock_capture):
        import numpy as np
        nav = WorkerNavigator(serial="127.0.0.1:16448")
        mock_focus.return_value = "com.ss.android.ugc.aweme.live.LivePlayActivity"
        
        # 模拟两帧具有动态变化且色彩丰富的真实直播帧
        frame1 = np.full((100, 100, 3), 100, dtype=np.uint8)
        frame2 = np.full((100, 100, 3), 100, dtype=np.uint8)
        frame2[10:30, 10:30, :] = 200  # 像素变化
        mock_capture.side_effect = [frame1, frame2]

        passed = nav.verify_preflight_content_gate(timeout_sec=2.0)
        self.assertTrue(passed)

    @patch("src.worker.adb_client.AdbDeviceClient.run_adb")
    def test_dismiss_center_dialog(self, mock_run):
        mock_run.return_value = MagicMock(exit_code=0, stdout="", stderr="")
        nav = WorkerNavigator(serial="127.0.0.1:16448")
        nav.dismiss_center_dialog()
        # 验证调用了 tap 330 1392 (点击拒绝按钮)
        args_tuple = mock_run.call_args[0]
        self.assertTrue("330" in args_tuple and "1392" in args_tuple)

    @patch("src.worker.navigator.WorkerNavigator.dismiss_center_dialog")
    @patch("src.worker.navigator.WorkerNavigator._capture_frame_array")
    @patch("src.worker.navigator.WorkerNavigator.get_current_focus_activity")
    def test_preflight_content_gate_blocked_by_center_dialog(self, mock_focus, mock_capture, mock_dismiss_center):
        import numpy as np
        nav = WorkerNavigator(serial="127.0.0.1:16448")
        mock_focus.return_value = "com.ss.android.ugc.aweme.splash.SplashActivity"
        
        # 构造居中卡片白底帧（全屏白底不高，但中心卡片区域 [27:76, 11:89] 均为 255）
        frame = np.full((100, 100, 3), 50, dtype=np.uint8)
        frame[27:76, 11:89, :] = 255
        mock_capture.return_value = frame

        passed = nav.verify_preflight_content_gate(timeout_sec=0.5)
        self.assertFalse(passed)
        # 验证触发了 dismiss_center_dialog
        self.assertTrue(mock_dismiss_center.called)


    @patch("src.worker.navigator.WorkerNavigator._capture_frame_array")
    @patch("src.worker.navigator.WorkerNavigator.get_current_focus_activity")
    def test_preflight_content_gate_rejects_main_activity_feed(self, mock_focus, mock_capture):
        import numpy as np
        nav = WorkerNavigator(serial="127.0.0.1:16448")
        # 前台停留在首页推荐短视频 MainActivity
        mock_focus.return_value = "com.ss.android.ugc.aweme.main.MainActivity"
        
        # 即使短视频中画面在动（动态变化）
        frame1 = np.full((100, 100, 3), 100, dtype=np.uint8)
        frame2 = np.full((100, 100, 3), 100, dtype=np.uint8)
        frame2[10:40, 10:40, :] = 220
        mock_capture.side_effect = [frame1, frame2, frame1, frame2]

        passed = nav.verify_preflight_content_gate(timeout_sec=0.8)
        # 坚决拒绝盲录短视频
        self.assertFalse(passed)


if __name__ == "__main__":
    unittest.main()


def test_profile_entry_is_clicked_once_and_sheet_avatar_is_opened_without_second_dump(monkeypatch):
    import numpy as np
    import time
    from src.worker.navigator import WorkerNavigator

    nav = WorkerNavigator.__new__(WorkerNavigator)
    nav._operation_deadline = None
    nav._navigation_generation = 4
    nav._profile_entry_clicked_generation = None
    nav._last_overlay_observation = None
    nav.adb_client = object()
    activities = iter([
        "com.ss.android.ugc.aweme.live.LivePlayActivity",
        "com.ss.android.ugc.aweme.profile.ui.UserProfileActivity",
        "com.ss.android.ugc.aweme.live.LivePlayActivity",
    ])
    nav.get_current_focus_activity = lambda: next(activities, "com.ss.android.ugc.aweme.live.LivePlayActivity")
    h,w=1920,1080
    live=np.full((h,w,3),20,dtype=np.uint8)
    sheet=live.copy();sheet[int(.27*h):,:,:]=255
    sheet[int(.29*h):int(.38*h),int(.07*w):int(.22*w),:]=[120,130,140]
    frames=iter([live,sheet])
    nav._capture_frame_array=lambda:next(frames)
    nav._navigation_sleep=lambda *args:True
    nav.dismiss_live_overlay=lambda *args,**kwargs:True
    normal='<hierarchy><node package="com.ss.android.ugc.aweme" resource-id="com.ss.android.ugc.aweme:id/apz" clickable="true" bounds="[36,98][495,206]"><node package="com.ss.android.ugc.aweme" resource-id="com.ss.android.ugc.aweme:id/avatar_container" bounds="[36,98][144,206]" /><node package="com.ss.android.ugc.aweme" resource-id="com.ss.android.ugc.aweme:id/user_name" bounds="[150,109][348,161]" /></node></hierarchy>'
    class Reader:
        calls=0
        def __init__(self,adb): self.deadline=None; self.dump_timeout_sec=12
        def get_ui_dump(self): self.calls+=1; return normal
    monkeypatch.setattr("src.worker.popup_bypasser.PopupBypasser",Reader)
    before="topResumedActivity=ActivityRecord{live u0 com.ss.android.ugc.aweme/.live.LivePlayActivity t41}"
    after="* Hist #1: ActivityRecord{profile u0 com.ss.android.ugc.aweme/.profile.ui.UserProfileActivity t41} Intent { dat=aweme://user/profile?uid=123&room_id=900&anchor_id=123&previous_page=live }\n"+before
    dumps=iter([before,after]); calls=[]
    def adb(*args,**kwargs):
        calls.append(args)
        if args[:4]==("shell","dumpsys","activity","top"): return ''
        if args[:4]==("shell","dumpsys","activity","activities"): return next(dumps)
        return ""
    nav._adb=adb
    result=nav.observe_live_room(time.monotonic()+30,lambda:False)
    taps=[c for c in calls if c[:3]==("shell","input","tap")]
    assert result.anchor_uid=="123" and result.room_id=="900"
    assert len(taps)==2
    assert taps[0][3:]==("90","152")
    assert taps[1][3:]==("151","643")



def test_live_fragment_fast_path_skips_uiautomator_and_all_ui_taps():
    import time
    from src.worker.navigator import WorkerNavigator
    nav=WorkerNavigator.__new__(WorkerNavigator)
    nav._operation_deadline=None
    nav.get_current_focus_activity=lambda:'com.ss.android.ugc.aweme.live.LivePlayActivity'
    top = (
        'topResumedActivity=ActivityRecord{live u0 com.ss.android.ugc.aweme/.live.LivePlayActivity t41}\n'
        'mArguments=Bundle[{live_backend_roomid=900, live.intent.extra.USER_ID=123, '
        'live.intent.extra.ROOM_ID=900, live.intent.extra.EXTRA_ROOM_UNIQUE_KEY=900_2, '
        'is_LiveRoomFragment=true, live.intent.extra.EXTRA_COMMON_LOG=Bundle[{}]}]'
    )
    calls=[]
    nav._adb=lambda *args,**kwargs: calls.append(args) or top
    nav._capture_frame_array=lambda: (_ for _ in ()).throw(AssertionError('fast path must not capture screenshot'))
    nav.dismiss_live_overlay=lambda *args,**kwargs: (_ for _ in ()).throw(AssertionError('fast path must not mutate UI'))
    result=nav.observe_live_room(time.monotonic()+20,lambda:False)
    assert result.room_id=='900' and result.anchor_uid=='123'
    assert result.source=='live_fragment_arguments'
    assert calls==[('shell','dumpsys','activity','top')]

def test_second_observation_on_same_navigation_does_not_repeat_header_tap(monkeypatch):
    import numpy as np
    import time
    from src.worker.navigator import WorkerNavigator
    nav = WorkerNavigator.__new__(WorkerNavigator)
    nav._operation_deadline = None
    nav._navigation_generation = 7
    nav._profile_entry_clicked_generation = None
    nav._last_overlay_observation = None
    nav.adb_client = object()
    nav.get_current_focus_activity = lambda: "com.ss.android.ugc.aweme.live.LivePlayActivity"
    nav._capture_frame_array = lambda: np.zeros((1920,1080,3),dtype=np.uint8)
    nav._navigation_sleep = lambda *args: True
    dismissed=[]
    nav.dismiss_live_overlay = lambda *args,**kwargs: dismissed.append(True) or True
    taps=[]
    nav._adb = lambda *args,**kwargs: taps.append(args) or ""
    class Reader:
        def __init__(self,adb): self.deadline=None; self.dump_timeout_sec=8
        def get_ui_dump(self):
            return '<hierarchy><node package="com.ss.android.ugc.aweme" resource-id="com.ss.android.ugc.aweme:id/user_name" bounds="[150,109][348,161]" /></hierarchy>'
    monkeypatch.setattr("src.worker.popup_bypasser.PopupBypasser",Reader)
    first=nav.observe_live_room(time.monotonic()+30,lambda:False)
    second=nav.observe_live_room(time.monotonic()+30,lambda:False)
    header_taps=[x for x in taps if x[:3]==("shell","input","tap")]
    assert first.source=="anchor_card_not_ready" and second.source=="anchor_card_not_ready"
    assert len(header_taps)==1
    assert dismissed==[]  # 不在下一轮读取前关闭可能刚打开的资料卡。


def test_new_navigation_resets_profile_entry_attempt(monkeypatch):
    import time
    from src.worker.navigator import WorkerNavigator, DOUYIN_PACKAGE
    nav=WorkerNavigator.__new__(WorkerNavigator)
    nav._navigation_generation=4
    nav._profile_entry_clicked_generation=4
    nav._last_overlay_observation={"activity":"LivePlayActivity"}
    nav._focus_snapshot_cache=(time.monotonic(),"com.ss.android.ugc.aweme","old.Activity")
    nav._operation_deadline=None
    nav.get_current_focus_activity=lambda:"LivePlayActivity"
    nav.get_current_focus_package=lambda:DOUYIN_PACKAGE
    nav._send_room_intent=lambda *args:True
    nav._navigation_sleep=lambda *args:True
    assert nav.navigate_route("900",None,"primary",time.monotonic()+5,lambda:False)
    assert nav._navigation_generation==5
    assert nav._profile_entry_clicked_generation is None
    assert nav._last_overlay_observation is None
    assert nav._focus_snapshot_cache is None


def test_foreground_package_and_activity_share_one_short_lived_snapshot():
    from types import SimpleNamespace
    from src.worker.navigator import WorkerNavigator
    nav=WorkerNavigator.__new__(WorkerNavigator)
    nav._operation_deadline=None
    nav._last_adb_failed=False
    nav._focus_snapshot_cache=None
    calls=[]
    def adb(*args,**kwargs):
        calls.append(args)
        return SimpleNamespace(exit_code=0,stdout="mCurrentFocus=Window{123 u0 com.ss.android.ugc.aweme/.live.LivePlayActivity}",stderr="")
    nav.adb_client=SimpleNamespace(run_adb=adb)
    assert nav.get_current_focus_activity()=="com.ss.android.ugc.aweme.live.LivePlayActivity"
    assert nav.get_current_focus_package()=="com.ss.android.ugc.aweme"
    assert len(calls)==1


def test_foreground_snapshot_is_invalidated_before_ui_mutation():
    import time
    from types import SimpleNamespace
    from src.worker.navigator import WorkerNavigator
    nav=WorkerNavigator.__new__(WorkerNavigator)
    nav._operation_deadline=None
    nav._last_adb_failed=False
    nav._focus_snapshot_cache=(time.monotonic(),"com.ss.android.ugc.aweme","old.Activity")
    calls=[]
    def adb(*args,**kwargs):
        calls.append(args)
        output="" if args[:3]==("shell","input","tap") else "mCurrentFocus=Window{123 u0 com.ss.android.ugc.aweme/.live.LivePlayActivity}"
        return SimpleNamespace(exit_code=0,stdout=output,stderr="")
    nav.adb_client=SimpleNamespace(run_adb=adb)
    nav._adb("shell","input","tap","1","2")
    assert nav._focus_snapshot_cache is None
    assert nav.get_current_focus_activity()=="com.ss.android.ugc.aweme.live.LivePlayActivity"
    assert len(calls)==2



def test_profile_sheet_fallback_requires_visible_sheet_and_returns_avatar_point():
    import numpy as np
    from src.worker.navigator import WorkerNavigator
    h,w=1920,1080
    live=np.full((h,w,3),20,dtype=np.uint8)
    assert WorkerNavigator._profile_sheet_avatar_point(live) is None
    sheet=live.copy()
    sheet[int(.27*h):,:,:]=255
    sheet[int(.29*h):int(.38*h),int(.07*w):int(.22*w),:]=[120,130,140]
    assert WorkerNavigator._profile_sheet_avatar_point(sheet)==(int(.14*w),int(.335*h))


def test_profile_sheet_fallback_rejects_landscape_and_nonwhite_overlays():
    import numpy as np
    from src.worker.navigator import WorkerNavigator
    assert WorkerNavigator._profile_sheet_avatar_point(np.full((1080,1920,3),255,dtype=np.uint8)) is None
    frame=np.full((1920,1080,3),20,dtype=np.uint8)
    frame[int(.27*1920):,:,:]=[40,40,40]
    assert WorkerNavigator._profile_sheet_avatar_point(frame) is None

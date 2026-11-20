"""抖音直播间 Intent 导航与原地刷新控制器。"""
from pathlib import Path
from dataclasses import replace
import logging
import subprocess
import time
import threading
import re
import xml.etree.ElementTree as ET

from src.mvp.config import get_adb_exe
from src.worker.adb_client import AdbDeviceClient
from src.worker.room_locator import (RoomEvidence, activity_records, fresh_profile_evidence,
                                      current_live_fragment_evidence)

logger = logging.getLogger(__name__)

DOUYIN_PACKAGE = "com.ss.android.ugc.aweme"
FAST_IDENTITY_TRIES = 6
FAST_IDENTITY_INTERVAL_SEC = 1.0


class WorkerNavigator:
    """负责通过 ADB 执行抖音直播间的秒级唤起、原地自愈刷新及权限放行。"""

    def __init__(self, serial: str, adb_exe: Path | None = None):
        self.serial = serial
        adb = adb_exe or get_adb_exe()
        if not adb or not Path(adb).exists():
            raise FileNotFoundError(f"未找到 adb.exe: {adb}")
        self.adb_exe = Path(adb).resolve()
        self.adb_client = AdbDeviceClient(serial=self.serial, adb_exe=self.adb_exe)
        self.target_kind = "auto"
        self.target_web_rid = None
        self.last_gate_reason = "render_timeout"
        self._operation_deadline = None
        self._last_adb_failed = False
        self._navigation_generation = 0
        self._profile_entry_clicked_generation = None
        self._probe_metrics = {}
        self._probe_metrics_lock = threading.Lock()
        self._focus_snapshot_cache = None

    def _invalidate_focus_cache(self) -> None:
        self._focus_snapshot_cache = None

    def device_online(self) -> bool:
        return self.adb_client.is_device_ready(timeout=3.0) or self.adb_client.ensure_connected(timeout=5.0)

    def _adb(self, *args: str, timeout: float = 5.0) -> str:
        """执行 ADB 命令，遇到断连自动有界重试。"""
        if args[:2] == ("shell", "input") or (args[:2] == ("shell", "am") and "start" in args):
            self._invalidate_focus_cache()
        try:
            self._last_adb_failed = False
            if self._operation_deadline is not None:
                timeout = min(timeout, self._operation_deadline - time.monotonic())
                if timeout < 1.0:
                    raise TimeoutError("导航流程预算耗尽")
            reclaim = .2 if self._operation_deadline is not None else 0
            res = self.adb_client.run_adb(*args, timeout=timeout-reclaim)
            self._last_adb_failed = res.exit_code != 0
            output = res.stdout.strip() or res.stderr.strip()
            if "not found" in output or "offline" in output:
                remaining = (self._operation_deadline - time.monotonic()) if self._operation_deadline is not None else 8.0
                if remaining < 4.0:
                    return ''
                self.adb_client.ensure_connected(timeout=min(3.0, remaining-2.0))
                retry_budget = min(timeout, self._operation_deadline-time.monotonic()) if self._operation_deadline is not None else timeout
                if retry_budget < 1.0:
                    return ''
                res2 = self.adb_client.run_adb(*args, timeout=retry_budget-reclaim)
                self._last_adb_failed = res2.exit_code != 0
                output = res2.stdout.strip()
            return output
        except Exception as e:
            self._last_adb_failed = True
            logger.warning(f"执行 ADB 指令失败: {args}, 错误: {e}")
            return ""

    def get_current_focus_snapshot(self) -> tuple[str | None, str | None]:
        """一次读取同时提供 package/activity；短时复用，UI 操作会主动失效。"""
        now = time.monotonic()
        cached = getattr(self, "_focus_snapshot_cache", None)
        if cached is not None and now - cached[0] <= 0.25:
            return cached[1], cached[2]

        package = activity = None
        out = self._adb("shell", "dumpsys", "window", "windows")
        match = re.search(r"mCurrentFocus=.*?([a-zA-Z0-9._]+/[a-zA-Z0-9._]+)", out)
        if match:
            component = match.group(1)
        else:
            out = self._adb("shell", "dumpsys", "activity", "activities")
            match = re.search(
                r"(?:topResumedActivity|mResumedActivity|ResumedActivity).*?ActivityRecord\{.*?\s+([a-zA-Z0-9._]+/[a-zA-Z0-9._]+)",
                out,
            )
            component = match.group(1) if match else None
        if component:
            package, activity_name = component.split("/", 1)
            activity = package + activity_name if activity_name.startswith(".") else activity_name
        self._focus_snapshot_cache = (time.monotonic(), package, activity)
        return package, activity

    def get_current_focus_package(self) -> str | None:
        """获取当前前台聚焦的应用包名。"""
        return self.get_current_focus_snapshot()[0]

    def get_current_focus_activity(self) -> str | None:
        """获取当前前台聚焦的完整 Activity 类名。"""
        return self.get_current_focus_snapshot()[1]

    def configure_target(self, kind: str = "auto", web_rid: str | None = None):
        """保留目标种类；同一任务首次进入和自愈使用完全相同的路由。"""
        if kind not in {"auto", "room_id", "web_rid"}:
            raise ValueError("未知 target_kind")
        self.target_kind = kind
        self.target_web_rid = web_rid

    def _send_room_intent(self, room_id: str, web_rid: str | None = None) -> bool:
        internal = self.target_kind == "room_id" or (
            self.target_kind == "auto" and re.fullmatch(r"\d{19}", str(room_id)) is not None)
        fallback = web_rid or self.target_web_rid
        if internal:
            uri = f"snssdk1128://live?room_id={room_id}"
            out = self._adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", uri,
                            "-p", DOUYIN_PACKAGE)
            if out and "Error" not in out and not self._last_adb_failed:
                return True
            if not fallback:
                return False
        target = fallback or room_id
        uri = str(target) if str(target).startswith(("http://", "https://")) else f"https://live.douyin.com/{target}"
        out = self._adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", uri,
                        "-p", DOUYIN_PACKAGE)
        return bool(out) and "Error" not in out and not self._last_adb_failed

    def refresh_current_room(self, room_id: str) -> bool:
        """复用首次唤起路由，短号不得被当作内部场次 ID。"""
        return self._send_room_intent(room_id)

    def capture_screenshot(self, save_path: Path) -> bool:
        """截屏用于心跳差分或证据归档。"""
        target_path = Path(save_path).resolve()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        remote_path = "/sdcard/wp_probe.png"
        self._adb("shell", "screencap", "-p", remote_path)
        subprocess.run(
            [str(self.adb_exe), "-s", self.serial, "pull", remote_path, str(target_path)],
            capture_output=True,
            text=True,
            timeout=5.0,
            cwd=self.adb_exe.parent,
        )
        self._adb("shell", "rm", "-f", remote_path)
        return target_path.exists()

    def _record_probe_timing(self, name: str, elapsed_ms: float, status: str) -> None:
        lock = getattr(self, "_probe_metrics_lock", None)
        if lock is None:
            self._probe_metrics_lock = threading.Lock()
            lock = self._probe_metrics_lock
        with lock:
            if not hasattr(self, "_probe_metrics"):
                self._probe_metrics = {}
            row = self._probe_metrics.setdefault(name, {
                "count": 0, "total_ms": 0.0, "max_ms": 0.0, "failures": 0,
            })
            row["count"] += 1
            row["total_ms"] += elapsed_ms
            row["max_ms"] = max(row["max_ms"], elapsed_ms)
            row["failures"] += int(status != "ok")

    def reset_metrics(self) -> None:
        lock = getattr(self, "_probe_metrics_lock", None)
        if lock is None:
            self._probe_metrics_lock = threading.Lock()
            lock = self._probe_metrics_lock
        with lock:
            self._probe_metrics = {}
        self.adb_client.reset_metrics()

    def metrics_snapshot(self) -> dict:
        lock = getattr(self, "_probe_metrics_lock", None)
        if lock is None:
            self._probe_metrics_lock = threading.Lock()
            lock = self._probe_metrics_lock
        with lock:
            probes = {
                name: {**row, "total_ms": round(row["total_ms"], 1),
                       "max_ms": round(row["max_ms"], 1)}
                for name, row in self._probe_metrics.items()
            }
        return {"adb": self.adb_client.metrics_snapshot(), "probes": probes}

    def _capture_frame_array(self):
        """通过 ADB 直接从内存截帧解码为 RGB 图像矩阵，并记录耗时。"""
        started = time.monotonic()
        status = "error"
        try:
            import io
            import numpy as np
            from PIL import Image
            cmd = [str(self.adb_exe), "-s", self.serial, "exec-out", "screencap", "-p"]
            # Allow for slower emulator frame readback under host GPU throttling.
            # A short visual-probe delay must not become a capture failure.
            budget = 8.0
            if self._operation_deadline is not None:
                budget = min(budget, self._operation_deadline - time.monotonic())
                if budget < 1.0:
                    return None
            res = subprocess.run(cmd, capture_output=True, timeout=budget, cwd=self.adb_exe.parent)
            if res.returncode == 0 and len(res.stdout) > 0:
                img = Image.open(io.BytesIO(res.stdout)).convert("RGB")
                status = "ok"
                return np.array(img)
        except Exception as e:
            logger.warning(f"内存截帧解码异常: {e}")
        finally:
            self._record_probe_timing("screenshot", (time.monotonic()-started)*1000.0, status)
        return None

    def detect_current_room_identity(self) -> tuple[str | None, str | None]:
        """只读取主播 UID 证据；展示短号、场次 room_id 绝不冒充 UID。"""
        try:
            from src.worker.popup_bypasser import PopupBypasser
            text = PopupBypasser(self.adb_client).get_ui_dump()
            begin = text.find("<hierarchy")
            if begin < 0:
                return None, None
            end = text.rfind("</hierarchy>")
            tree = ET.fromstring(text[begin:end + len("</hierarchy>")])
            uids, nickname = set(), None
            for node in tree.iter("node"):
                key = node.get("resource-id", "").rsplit("/", 1)[-1]
                value = node.get("text", "").strip()
                if key in {"author_uid", "author_id", "user_id"} and re.fullmatch(r"\d+", value):
                    uids.add(value)
                if key in {"user_name", "author_name"} and value:
                    nickname = value
            return (next(iter(uids)) if len(uids) == 1 else None), nickname
        except Exception as exc:
            logger.warning("当前主播身份无法可靠读取: %s", exc)
            return None, None

    def _navigation_sleep(self, seconds, deadline, stop_requested):
        end = min(deadline, time.monotonic() + seconds)
        while time.monotonic() < end:
            if stop_requested():
                return False
            time.sleep(max(0.0, min(.1, end-time.monotonic())))
        return not stop_requested()

    def navigate_route(self, target, web_rid, route, deadline, stop_requested):
        """根据页面反馈选择入口，不把系统已受理当作定位完成。"""
        self._navigation_generation = getattr(self, "_navigation_generation", 0) + 1
        self._profile_entry_clicked_generation = None
        self._invalidate_focus_cache()
        old = self._operation_deadline
        self._operation_deadline = deadline
        try:
            if stop_requested():
                return False
            for _ in range(3):
                act = self.get_current_focus_activity() or ''
                if 'UserProfileActivity' in act or 'login' in act.lower():
                    self._adb('shell', 'input', 'keyevent', '4')
                    if not self._navigation_sleep(.3, deadline, stop_requested):
                        return False
                else:
                    break
            if route == 'reset' or self.get_current_focus_package() != DOUYIN_PACKAGE:
                self._adb('shell', 'am', 'start', '-n', f'{DOUYIN_PACKAGE}/.main.MainActivity')
                if not self._navigation_sleep(2.5, deadline, stop_requested):
                    return False
            if route == 'web':
                uri = str(web_rid) if str(web_rid).startswith('https://') else f'https://live.douyin.com/{web_rid}'
                output = self._adb('shell', 'am', 'start', '-a', 'android.intent.action.VIEW',
                                   '-d', uri, '-p', DOUYIN_PACKAGE)
                ok = bool(output) and 'Error' not in output and not self._last_adb_failed
            else:
                ok = self._send_room_intent(target, web_rid)
            if ok:
                self._navigation_sleep(1.0, deadline, stop_requested)
            return ok and not stop_requested()
        finally:
            self._operation_deadline = old

    def wait_live_page(self, deadline, stop_requested):
        from src.worker.popup_bypasser import PopupBypasser
        old = self._operation_deadline
        self._operation_deadline = deadline
        main_seen_since = None
        next_home_popup_check = 0.0

        def dismiss_popup():
            bypasser = PopupBypasser(self.adb_client)
            bypasser.deadline = min(deadline, time.monotonic()+4)
            if bypasser.detect_and_dismiss_popups():
                self._invalidate_focus_cache()
                return True
            return False

        try:
            while time.monotonic() < deadline and not stop_requested():
                act = self.get_current_focus_activity() or ''
                if 'LivePlayActivity' in act:
                    self.last_gate_reason = ''
                    return True
                if 'login' in act.lower():
                    self.last_gate_reason = 'login_blocked'
                    self._adb('shell', 'input', 'keyevent', '4')
                elif 'MainActivity' in act:
                    if main_seen_since is None:
                        main_seen_since = time.monotonic()
                    self.last_gate_reason = 'navigation_failed'
                    # 首次启动的协议框、青少年模式等白名单弹窗叠在首页上，会吞掉直播 Intent。
                    if time.monotonic() >= next_home_popup_check:
                        next_home_popup_check = time.monotonic() + 3
                        if dismiss_popup():
                            main_seen_since = None
                            continue
                    if time.monotonic()-main_seen_since >= 10:
                        return False
                elif not act:
                    main_seen_since = None
                    self.last_gate_reason = 'page_probe_unavailable'
                else:
                    dismiss_popup()
                self._navigation_sleep(.5, deadline, stop_requested)
            return False
        finally:
            self._operation_deadline = old

    @staticmethod
    def _anchor_entry_point(tree, width, height):
        """优先定位直播顶部真实头像容器，不点昵称文字或关注按钮。"""
        parent = {child: node for node in tree.iter() for child in node}
        choices = []
        for node in tree.iter('node'):
            key = node.get('resource-id','').rsplit('/',1)[-1]
            if node.get('package') != DOUYIN_PACKAGE or key not in {'avatar_container','user_avatar','author_avatar'}:
                continue
            match = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]',node.get('bounds',''))
            if not match:
                continue
            x1,y1,x2,y2 = map(int,match.groups())
            if 0 <= x1 < x2 < width*.25 and height*.03 <= y1 < y2 < height*.18:
                choices.append(((x1+x2)//2,(y1+y2)//2))
        unique = list(dict.fromkeys(choices))
        if len(unique)==1:
            return unique[0]

        # 某些版本只把头像节点作为不可点击子项暴露；只有唯一头像且父容器可点击才接受。
        fallback=[]
        for node in tree.iter('node'):
            key=node.get('resource-id','').rsplit('/',1)[-1]
            if node.get('package') != DOUYIN_PACKAGE or key not in {'user_name','author_name'}:
                continue
            ancestor=parent.get(node)
            while ancestor is not None:
                if ancestor.tag=='node' and ancestor.get('package')==DOUYIN_PACKAGE and ancestor.get('clickable')=='true':
                    match=re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]',ancestor.get('bounds',''))
                    if match:
                        x1,y1,x2,y2=map(int,match.groups())
                        if 0 <= x1 < x2 < width*.65 and height*.03 <= y1 < y2 < height*.18:
                            fallback.append(((x1+x2)//2,(y1+y2)//2))
                    break
                ancestor=parent.get(ancestor)
        fallback=list(dict.fromkeys(fallback))
        return fallback[0] if len(fallback)==1 else None

    @staticmethod
    def _profile_sheet_avatar_point(frame):
        """在已打开的主播资料半屏卡片中定位头像；仅支持已验证的竖屏浅色卡片。

        Android 15 的直播动效可能令 UIAutomator 第二次 dump 报
        'could not get idle state'。此时用截图确认底部浅色资料卡存在，
        再点击已实测的头像相对位置；未检测到卡片时绝不盲点。
        """
        if frame is None:
            return None
        import numpy as np
        h, w = frame.shape[:2]
        if not 1.6 < h / w < 2.3:
            return None

        def patch_luma(rx, ry, radius=10):
            x, y = int(w * rx), int(h * ry)
            patch = frame[max(0, y-radius):min(h, y+radius),
                          max(0, x-radius):min(w, x+radius), :3]
            return float(np.mean(patch)) if patch.size else 0.0

        # 资料卡以底部白色/浅色 sheet 覆盖直播画面；两处独立采样降低误判。
        live_bg = patch_luma(.95, .20)
        sheet_right_top = patch_luma(.95, .50)
        sheet_right_mid = patch_luma(.95, .70)
        avatar_area = patch_luma(.14, .335)
        if live_bg > 150 or sheet_right_top < 225 or sheet_right_mid < 220:
            return None
        if avatar_area > 248 or avatar_area < 25:
            return None
        return int(w * .14), int(h * .335)

    def observe_live_room(self, deadline, stop_requested):
        self._observation_returned_to_live = True
        evidence = self._observe_live_room(deadline, stop_requested)
        return replace(evidence, returned_to_live=self._observation_returned_to_live)

    def _observe_live_room(self, deadline, stop_requested):
        """优先从当前 LivePlayFragment 直接读取场次 UID/room_id，失败才走资料页回退。

        快速路径只读取当前 LivePlayActivity 的 Fragment mArguments，不查询公网，
        不读取账号凭证，不点击任何 UI。证据缺失或冲突时才使用原有资料卡取证。
        """
        from src.worker.popup_bypasser import PopupBypasser
        old = self._operation_deadline
        self._operation_deadline = deadline
        operation_deadline = deadline - min(10.0, max(2.0, (deadline-time.monotonic())*.2))
        self._operation_deadline = operation_deadline
        card_open = False
        profile_open = False
        fast_identity = False
        live_task = None
        frame = None
        try:
            if stop_requested():
                return RoomEvidence(source='cancelled')
            current_activity = self.get_current_focus_activity() or ''
            if 'LivePlayActivity' not in current_activity:
                return RoomEvidence(source='login_blocked' if 'login' in current_activity.lower() else 'page_changed' if current_activity else 'page_probe_unavailable')
            # 先读取当前 Activity 顶部 Fragment 参数；成功时完全跳过 UIAutomator 和头像点击。
            # 直播页刚打开时参数可能尚未就绪，先短暂重读，避免过早落入耗时且需点击的资料卡取证。
            for fast_try in range(FAST_IDENTITY_TRIES):
                if getattr(self, '_profile_entry_clicked_generation', None) == getattr(self, '_navigation_generation', 0):
                    break  # 已点击的资料卡先处理完，不用其背后的 Fragment 跳过返回确认。
                if stop_requested() or time.monotonic() >= operation_deadline:
                    return RoomEvidence(source='observation_budget_exhausted')
                activity_top = self._adb('shell', 'dumpsys', 'activity', 'top',
                                         timeout=min(8.0, max(1.0, operation_deadline-time.monotonic())))
                fast_evidence = current_live_fragment_evidence(activity_top, current_activity)
                if fast_evidence is not None:
                    fast_identity = True
                    logger.info('从当前 LivePlayFragment 取得身份：room_id=%s uid=%s（第 %d 次读取）',
                                fast_evidence.room_id, fast_evidence.anchor_uid, fast_try+1)
                    return fast_evidence
                if fast_try+1 < FAST_IDENTITY_TRIES:
                    if operation_deadline-time.monotonic() < FAST_IDENTITY_INTERVAL_SEC+15:
                        break
                    if not self._navigation_sleep(FAST_IDENTITY_INTERVAL_SEC, operation_deadline, stop_requested):
                        return RoomEvidence(source='cancelled')
                    current_activity = self.get_current_focus_activity() or ''
                    if 'LivePlayActivity' not in current_activity:
                        return RoomEvidence(source='login_blocked' if 'login' in current_activity.lower() else 'page_changed' if current_activity else 'page_probe_unavailable')
            self._observation_returned_to_live = False
            frame = self._capture_frame_array()
            if frame is None:
                return RoomEvidence(source='screenshot_unavailable')
            h, w = frame.shape[:2]
            if not 1.6 < h/w < 2.3:
                return RoomEvidence(source='unsupported_live_layout')

            bypasser = PopupBypasser(self.adb_client)
            bypasser.dump_timeout_sec = 4.0  # live 动画无法 idle 时快速切换截图证据，不等待长超时
            bypasser.deadline = min(operation_deadline, time.monotonic()+15)
            xml = bypasser.get_ui_dump()  # 一次取证只请求一次 UIAutomator，避免 live UI idle 超时
            tree = ET.fromstring(xml) if xml else None
            avatars = ([n for n in tree.iter('node')
                        if n.get('package') == DOUYIN_PACKAGE
                        and n.get('content-desc', '').endswith('的头像')]
                       if tree is not None else [])

            if len(avatars) == 1:
                # 已经处于资料卡状态：直接按实际节点坐标进入完整资料页。
                avatar = avatars[0]
                nickname = avatar.get('content-desc', '')[:-3]
                points = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', avatar.get('bounds',''))
                if not points:
                    return RoomEvidence(nickname=nickname)
                x1,y1,x2,y2 = map(int,points.groups())
                if not (0 <= x1 < x2 <= w and 0 <= y1 < y2 <= h):
                    return RoomEvidence(nickname=nickname)
                avatar_point = ((x1+x2)//2,(y1+y2)//2)
                card_open = True
            else:
                # 当前直播布局的主播入口是顶部 user_name；没有 XML 时仅用已验证竖屏头像坐标。
                generation = getattr(self, '_navigation_generation', 0)
                already_clicked = getattr(self, '_profile_entry_clicked_generation', None) == generation
                if not already_clicked:
                    point = self._anchor_entry_point(tree, w, h) if tree is not None else None
                    if point is None:
                        # 已实测 MuMu 竖屏顶部主播头像中心；不点击昵称或关注按钮。
                        point = (int(w*.0833), int(h*.079))
                    self._adb('shell','input','tap',str(point[0]),str(point[1]))
                    self._profile_entry_clicked_generation = generation
                    # 网络慢或直播页刚完成冷启动时，资料卡动画可能晚于入口点击；
                    # 多等一小段再截图确认，不重复点击入口。
                    if not self._navigation_sleep(2.5, operation_deadline, stop_requested):
                        return RoomEvidence(source='cancelled')
                # 不再二次 UI dump。直播动画会令 UiAutomation waitForIdle 报错；
                # 截图只在确认底部资料卡出现后，才点击其头像。
                sheet_frame = self._capture_frame_array()
                avatar_point = self._profile_sheet_avatar_point(sheet_frame)
                if avatar_point is None:
                    logger.info('主播入口已点击，但截图未确认资料卡；不执行盲点')
                    # 未确认前不能把它标记为已打开；也不能发送 Back，避免误退直播页。
                    return RoomEvidence(source='anchor_card_not_ready')
                card_open = True

            before = self._adb('shell', 'dumpsys', 'activity', 'activities')
            active = re.search(
                r'(?:topResumedActivity|mResumedActivity|ResumedActivity).*?ActivityRecord\{.*?\s+u\d+\s+[^ ]*LivePlayActivity\s+t(\d+)',
                before,
            )
            if not active:
                return RoomEvidence()
            live_task = active.group(1)
            self._adb('shell', 'input', 'tap', str(avatar_point[0]), str(avatar_point[1]))
            profile_open = True
            until = min(operation_deadline, time.monotonic()+12)
            while time.monotonic() < until and not stop_requested():
                after = self._adb('shell','dumpsys','activity','activities')
                evidence = fresh_profile_evidence(before, after, active.group(1))
                if evidence is not None:
                    return evidence
                self._navigation_sleep(.5, until, stop_requested)
            return RoomEvidence()
        except Exception as exc:
            logger.warning('当前房间取证未完成，将尝试纠偏: %s', exc)
            return RoomEvidence()
        finally:
            cleanup_deadline = min(deadline, time.monotonic()+10.0)
            self._operation_deadline = cleanup_deadline
            try:
                if profile_open:
                    for _ in range(3):
                        if stop_requested() or time.monotonic() >= cleanup_deadline:
                            break
                        act = self.get_current_focus_activity() or ''
                        if 'UserProfileActivity' in act or 'login' in act.lower():
                            self._adb('shell','input','keyevent','4')
                            self._navigation_sleep(.3, cleanup_deadline, stop_requested)
                        else:
                            break
                elif card_open and not stop_requested():
                    self._adb('shell','input','keyevent','4')
                if (not fast_identity and (profile_open or card_open) and live_task
                        and not stop_requested() and time.monotonic() < cleanup_deadline):
                    returned = self._adb('shell', 'dumpsys', 'activity', 'activities')
                    active = re.search(
                        r'(?:topResumedActivity|mResumedActivity|ResumedActivity).*?ActivityRecord\{.*?\s+u\d+\s+[^ ]*LivePlayActivity\s+t(\d+)', returned)
                    if active and active.group(1) == live_task:
                        self._observation_returned_to_live = True
                        self._profile_entry_clicked_generation = None
                # 未确认资料卡出现时保留点击标记，不连点、不盲目 Back。
            except Exception as exc:
                self._observation_returned_to_live = False
                logger.warning('资料页返回未确认，保留本轮身份观察：%s', exc)
            finally:
                self._operation_deadline = old

    def wait_live_picture(self, deadline, stop_requested, *, check_overlay=True,
                          return_from_profile=False):
        """等待当前页就绪：空观测属于未知，不等同已离开直播间。

        仅取证后的调用允许退出本流程打开的资料页；已经确认错误页面时仍返回失败。
        遮挡检查与图像采样分配预算，避免UI查询吞掉两次完整截图所需的时间。
        """
        import numpy as np
        old = self._operation_deadline
        self._operation_deadline = deadline
        previous = None
        last_problem = 'render_timeout'
        backs = 0
        overlay_checked = not check_overlay
        try:
            while time.monotonic() < deadline and not stop_requested():
                if deadline-time.monotonic() < 1.0:
                    break
                act = self.get_current_focus_activity() or ''
                if not act:
                    last_problem = 'page_probe_unavailable'
                    self._navigation_sleep(.5, deadline, stop_requested)
                    continue
                if 'LivePlayActivity' not in act:
                    if return_from_profile and backs < 3 and ('UserProfileActivity' in act or 'login' in act.lower()):
                        self._adb('shell', 'input', 'keyevent', '4')
                        backs += 1
                        self._navigation_sleep(.6, deadline, stop_requested)
                        continue
                    self.last_gate_reason = 'login_blocked' if 'login' in act.lower() else 'navigation_failed'
                    return False
                if not overlay_checked:
                    # 15秒检查/返回遮挡，至少留下10秒给两次截图；不以毫秒余额启动探针。
                    overlay_deadline = min(deadline-10, time.monotonic()+15)
                    if overlay_deadline-time.monotonic() < 2:
                        last_problem = 'page_probe_unavailable'
                        break
                    if not self.dismiss_live_overlay(overlay_deadline, stop_requested):
                        self.last_gate_reason = 'overlay_not_recovered'
                        return False
                    overlay_checked = True
                    # 返回操作后重新读取Activity再采样，不使用点击前的页面状态。
                    continue
                frame = self._capture_frame_array()
                if frame is None:
                    last_problem = 'picture_probe_unavailable'
                else:
                    last_problem = 'render_timeout'
                    h,w = frame.shape[:2]
                    roi = frame[int(h*.15):int(h*.75),int(w*.08):int(w*.85):2]
                    if previous is not None and previous.shape == roi.shape:
                        motion = float(np.mean(np.abs(roi.astype(np.int16)-previous.astype(np.int16)) > 15))
                        if motion > .003 and float(np.std(roi)) > 5:
                            self.last_gate_reason = ''
                            return not stop_requested()
                    previous = roi.copy()
                self._navigation_sleep(.4, deadline, stop_requested)
            self.last_gate_reason = 'cancelled' if stop_requested() else last_problem
            return False
        finally:
            self._operation_deadline = old

    def inspect_live_surface(self, timeout_sec=3.0):
        """后台只读探针：读取窗口焦点，不等待页面空闲；不点击，不修改主导航的截止时间。"""
        from src.worker.live_overlay import classify_live_surface
        deadline = time.monotonic() + timeout_sec
        texts = {}
        for target in ('displays', 'windows'):
            remaining = deadline - time.monotonic()
            texts[target] = ''
            if remaining < .2:
                break
            try:
                result = self.adb_client.run_adb('shell', 'dumpsys', 'window', target,
                                                 timeout=max(.001,remaining-.2), check=False)
                if result.exit_code == 0:
                    texts[target] = result.stdout or ''
            except Exception as exc:
                logger.debug('窗口状态不可读取: %s', exc)
        surface = classify_live_surface(texts.get('windows', ''), texts.get('displays', ''))
        return {'activity': surface.activity, 'overlay': surface.overlay,
                'markers': list(surface.markers), 'ui_available': surface.available,
                'observed_at': time.time()}

    def inspect_live_identity(self, deadline, stop_requested=lambda: False):
        """运行期只读观察：独立预算，无点击、缓存和主导航状态写入。"""
        from src.worker.runtime_identity import IdentityObservation
        started, wall = time.monotonic(), time.time()
        def read(target):
            remaining = deadline - time.monotonic()
            if stop_requested() or remaining <= 0:
                raise TimeoutError('identity_probe_deadline')
            result = self.adb_client.run_adb('shell', 'dumpsys', 'activity', target,
                                            timeout=max(.001,remaining-.2), check=False)
            if result.exit_code:
                raise RuntimeError('identity_probe_failed')
            return result.stdout or ''
        def context(text):
            match = re.search(r'(?:topResumedActivity|mResumedActivity|ResumedActivity).*?ActivityRecord\{(\S+)\s+u\d+\s+(\S+)\s+t(\d+)', text)
            return match.groups() if match else None
        evidence, reason = None, 'room_evidence_unavailable'
        try:
            before = context(read('activities'))
            if before and 'LivePlayActivity' in before[1]:
                # top 还转储后台桌面等任务；只请求实际前台组件，并限定任务及实例。
                top = read(before[1])
                after = context(read('activities'))
                if before == after:
                    blocks=re.split(r'(?=^TASK\s)',top,flags=re.MULTILINE)
                    blocks=[block for block in blocks if re.search(r'^TASK[^\n]*\bid='+re.escape(before[2])+r'\b',block)]
                    token_ids={before[0].lower()}
                    if before[0].isdigit():token_ids.add(format(int(before[0]),'x'))
                    if len(blocks)==1:
                        headers=re.findall(r'^\s*ACTIVITY\s+(\S+)\s+(\S+)',blocks[0],re.MULTILINE)
                        if len(headers)==1 and headers[0][0]==before[1] and headers[0][1].lower() in token_ids:
                            evidence = current_live_fragment_evidence(blocks[0], before[1])
                    reason = '' if evidence else 'room_evidence_unavailable'
                else:
                    reason = 'page_changed'
            else:
                reason = 'page_changed' if before else 'page_probe_unavailable'
        except Exception as exc:
            reason = 'identity_probe_timeout' if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) else 'identity_probe_failed'
        finished = time.monotonic()
        if finished > deadline or stop_requested():
            evidence, reason = None, 'identity_probe_timeout'
        return IdentityObservation(evidence, reason, started, finished, wall, time.time())

    def dismiss_live_overlay(self, deadline, stop_requested, *, require_observed_clear=False):
        old = getattr(self, '_operation_deadline', None)
        self._operation_deadline = min(old, deadline) if old is not None else deadline
        try:
            return self._dismiss_live_overlay(self._operation_deadline, stop_requested,
                                               require_observed_clear=require_observed_clear)
        finally:
            self._operation_deadline = old

    def _dismiss_live_overlay(self, deadline, stop_requested, *, require_observed_clear=False):
        """只在新鲜窗口证据确认面板持有焦点时发送 BACK；关闭后必须再次观察。

        每次 BACK 前都重新读取焦点：面板若已自行关闭，BACK 会退出直播间。
        最多两次 BACK，所有复检共享截止时间。
        require_observed_clear 用于运行期验收，必须读到面板已消失的窗口证据。
        """
        backs = 0
        for _ in range(6):
            if stop_requested() or time.monotonic() >= deadline:
                return False
            current = self.inspect_live_surface(
                timeout_sec=max(.001, min(3, deadline-time.monotonic())))
            activity = current.get('activity', '')
            if activity and 'LivePlayActivity' not in activity:
                return False
            if not current.get('overlay'):
                if current.get('ui_available'):
                    return True
                if not require_observed_clear and backs == 0:
                    return True  # 初次准入未知不盲点；后续画面与周期探针仍继续。
                self._navigation_sleep(.3, deadline, stop_requested)
                continue
            if backs >= 2:
                return False
            if stop_requested() or time.monotonic()>=deadline:return False
            logger.info('关闭直播面板窗口: %s，使用返回键', ' '.join(current.get('markers', [])))
            self._adb('shell', 'input', 'keyevent', '4',
                      timeout=min(2, max(.001, deadline-time.monotonic())))
            backs += 1
            if not self._navigation_sleep(.6, deadline, stop_requested):
                return False
        return False

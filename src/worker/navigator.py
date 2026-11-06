"""抖音直播间 Intent 导航与原地刷新控制器。"""
from pathlib import Path
import logging
import subprocess
import time
import re
import xml.etree.ElementTree as ET

from src.mvp.config import get_adb_exe
from src.worker.adb_client import AdbDeviceClient
from src.worker.room_locator import RoomEvidence, activity_records, fresh_profile_evidence

logger = logging.getLogger(__name__)

DOUYIN_PACKAGE = "com.ss.android.ugc.aweme"


class WorkerNavigator:
    """负责通过 ADB 执行抖音直播间的秒级唤起、原地自愈刷新及权限放行。"""

    def __init__(self, serial: str, adb_exe: Path | None = None):
        self.serial = serial
        self.adb_exe = adb_exe or get_adb_exe()
        if not self.adb_exe or not self.adb_exe.exists():
            raise FileNotFoundError(f"未找到 adb.exe: {self.adb_exe}")
        self.adb_client = AdbDeviceClient(serial=self.serial, adb_exe=self.adb_exe)
        self.target_kind = "auto"
        self.target_web_rid = None
        self.last_gate_reason = "render_timeout"
        self._operation_deadline = None
        self._last_adb_failed = False

    def _adb(self, *args: str, timeout: float = 5.0) -> str:
        """执行 ADB 命令，遇到断连自动有界重试。"""
        try:
            self._last_adb_failed = False
            if self._operation_deadline is not None:
                timeout = min(timeout, self._operation_deadline - time.monotonic())
                if timeout < 1.0:
                    raise TimeoutError("导航流程预算耗尽")
            res = self.adb_client.run_adb(*args, timeout=timeout)
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
                res2 = self.adb_client.run_adb(*args, timeout=retry_budget)
                self._last_adb_failed = res2.exit_code != 0
                output = res2.stdout.strip()
            return output
        except Exception as e:
            self._last_adb_failed = True
            logger.warning(f"执行 ADB 指令失败: {args}, 错误: {e}")
            return ""

    def get_current_focus_package(self) -> str | None:
        """获取当前前台聚焦的应用包名。"""
        out = self._adb("shell", "dumpsys", "window", "windows")
        m = re.search(r"mCurrentFocus=.*?([a-zA-Z0-9._]+)/", out)
        if m:
            return m.group(1)
        out2 = self._adb("shell", "dumpsys", "activity", "activities")
        m2 = re.search(r"ResumedActivity: ActivityRecord\{.*?\s+([a-zA-Z0-9._]+)/", out2)
        if m2:
            return m2.group(1)
        return None

    def get_current_focus_activity(self) -> str | None:
        """获取当前前台聚焦的完整 Activity 类名。"""
        out = self._adb("shell", "dumpsys", "window", "windows")
        m = re.search(r"mCurrentFocus=.*?([a-zA-Z0-9._]+/[a-zA-Z0-9._]+)", out)
        if m:
            return m.group(1).replace("/", ".")
        out2 = self._adb("shell", "dumpsys", "activity", "activities")
        m2 = re.search(r"ResumedActivity: ActivityRecord\{.*?\s+([a-zA-Z0-9._]+/[a-zA-Z0-9._]+)", out2)
        if m2:
            return m2.group(1).replace("/", ".")
        return None

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

    def launch_room(self, room_id: str, web_rid: str | None = None,
                    wait_sec: int = 3, warmup_first: bool = False) -> bool:
        """离线目标路由；只有 App 未在前台时预热，不查询公网状态。"""
        if warmup_first and self.get_current_focus_package() != DOUYIN_PACKAGE:
            self._adb("shell", "am", "start", "-n", f"{DOUYIN_PACKAGE}/.main.MainActivity")
            time.sleep(3.5)
        if not self._send_room_intent(room_id, web_rid):
            return False
        if wait_sec > 0:
            time.sleep(wait_sec)
        self.dismiss_login_prompt()
        return self.get_current_focus_package() == DOUYIN_PACKAGE

    def refresh_current_room(self, room_id: str) -> bool:
        """复用首次唤起路由，短号不得被当作内部场次 ID。"""
        return self._send_room_intent(room_id)

    def dismiss_system_dialogs(self):
        """自动放行 Android 系统投屏/录音授权弹窗及抖音登录阻断遮罩。"""
        try:
            # 1. 尝试放行系统授权弹窗
            self._adb("shell", "input", "keyevent", "66")  # KEYCODE_ENTER
            # 2. 检测并关闭可能遮挡直播间的登录引导弹窗
            self.dismiss_login_prompt()
        except Exception as e:
            logger.warning(f"放行弹窗异常: {e}")

    def dismiss_login_prompt(self):
        """若前台弹出抖音免登录阻断遮罩（DYLoginActivity），自动点击跳过/关闭放行。"""
        out = self._adb("shell", "dumpsys", "window", "windows")
        if "DYLoginActivity" in out or "login" in out.lower():
            logger.info("检测到抖音登录遮罩弹窗，正在自动点击跳过/关闭放行...")
            # 1. 尝试点击全屏登录页左上角的“跳过”按钮（bounds=[18,72][150,204] 中心点 84, 138）
            self._adb("shell", "input", "tap", "84", "138")
            time.sleep(0.3)
            # 2. 尝试点击半屏弹窗右上角关闭按钮（bounds=[960,656][1032,728] 中心点 996, 692）
            self._adb("shell", "input", "tap", "996", "692")
            time.sleep(0.5)

    def dismiss_center_dialog(self):
        """尝试点击居中卡片弹窗（如“发现抖音朋友”）左侧的【拒绝】按钮，严禁点击右侧【继续】。"""
        logger.info("检测到居中卡片弹窗，正在点击左侧【拒绝】按钮放行...")
        # 居中弹窗拒绝按钮 bounds=[120,1321][539,1464]，中心点约 330, 1392
        self._adb("shell", "input", "tap", "330", "1392")
        time.sleep(0.4)

    def capture_screenshot(self, save_path: Path) -> bool:
        """截屏用于心跳差分或证据归档。"""
        save_path.parent.mkdir(parents=True, exist_ok=True)
        remote_path = "/sdcard/wp_probe.png"
        self._adb("shell", "screencap", "-p", remote_path)
        subprocess.run(
            [str(self.adb_exe), "-s", self.serial, "pull", remote_path, str(save_path)],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        self._adb("shell", "rm", "-f", remote_path)
        return save_path.exists()

    def _capture_frame_array(self):
        """通过 ADB 直接从内存截帧解码为 RGB 图像矩阵。"""
        try:
            import io
            import numpy as np
            from PIL import Image
            cmd = [str(self.adb_exe), "-s", self.serial, "exec-out", "screencap", "-p"]
            budget = 5.0
            if self._operation_deadline is not None:
                budget = min(budget, self._operation_deadline - time.monotonic())
                if budget < 1.0:
                    return None
            res = subprocess.run(cmd, capture_output=True, timeout=budget)
            if res.returncode == 0 and len(res.stdout) > 0:
                img = Image.open(io.BytesIO(res.stdout)).convert("RGB")
                return np.array(img)
        except Exception as e:
            logger.warning(f"内存截帧解码异常: {e}")
        return None

    def verify_preflight_content_gate(self, timeout_sec: float = 8.0) -> bool:
        self._operation_deadline = time.monotonic() + timeout_sec
        try:
            return self._verify_preflight_content_gate(timeout_sec)
        finally:
            self._operation_deadline = None

    def _verify_preflight_content_gate(self, timeout_sec: float) -> bool:
        """开录前置双重门禁：验证前台已脱离登录页且直播流真正开始动态渲染。"""
        import numpy as np
        from src.worker.popup_bypasser import PopupBypasser
        bypasser = PopupBypasser(adb_client=self.adb_client)
        bypasser.deadline = self._operation_deadline
        start_t = time.monotonic()
        self.last_gate_reason = "render_timeout"
        logger.info(f"正在执行开录前置内容门禁核验 (阈值 {timeout_sec}s)...")

        while time.monotonic() - start_t < timeout_sec:
            # 优先穿透白名单弹窗 (如未成年人模式/青少年模式/隐私协议)
            if bypasser.detect_and_dismiss_popups():
                time.sleep(0.5)
                continue

            act = self.get_current_focus_activity() or ""
            if "DYLoginActivity" in act or "login" in act.lower():
                self.last_gate_reason = "login_blocked"
                self.dismiss_login_prompt()
                time.sleep(0.3)
                continue

            # 硬卡口：若前台处于短视频推荐流 MainActivity，坚决拦截，拒绝截帧浪费
            if "MainActivity" in act:
                self.last_gate_reason = "navigation_failed"
                logger.warning(f"门禁拦截：前台当前处于首页短视频推荐流 ({act})，坚决拦截出流！")
                time.sleep(0.3)
                continue

            if not act:
                self.last_gate_reason = "device_unavailable"
            elif "login" not in act.lower():
                self.last_gate_reason = "render_timeout"
            frame1 = self._capture_frame_array()
            if frame1 is None:
                time.sleep(0.3)
                continue

            # 1. 白底占比检测
            # a) 全屏白底检测 (全屏登录页 RGB > 235 占比 > 60%)
            white_ratio = float(np.mean((frame1[:, :, 0] > 235) & (frame1[:, :, 1] > 235) & (frame1[:, :, 2] > 235)))
            if white_ratio > 0.60:
                logger.warning(f"门禁检测到全屏高白底异常 (白底占比 {white_ratio:.1%})，尝试穿透遮罩...")
                self.dismiss_login_prompt()
                time.sleep(0.3)
                continue

            # b) 居中卡片弹窗白底检测 (如“发现抖音朋友”居中 bounds=[120,528][960,1464])
            h, w, _ = frame1.shape
            center_crop = frame1[int(h * 0.27):int(h * 0.76), int(w * 0.11):int(w * 0.89)]
            if center_crop.size > 0:
                center_white = float(np.mean((center_crop[:, :, 0] > 235) & (center_crop[:, :, 1] > 235) & (center_crop[:, :, 2] > 235)))
                if center_white > 0.65:
                    logger.warning(f"门禁检测到居中白色卡片弹窗 (居中白底占比 {center_white:.1%})，尝试点击【拒绝】穿透...")
                    self.dismiss_center_dialog()
                    time.sleep(0.3)
                    continue

            # 2. 帧差分动态推流校验
            time.sleep(0.3)
            frame2 = self._capture_frame_array()
            if frame2 is None or frame2.shape != frame1.shape:
                continue

            diff = np.abs(frame2.astype(np.int16) - frame1.astype(np.int16))
            dynamic_ratio = float(np.mean(diff > 15))

            if dynamic_ratio > 0.005:  # 大于 0.5% 像素有动态变化
                # 终极硬门禁校验：严禁在 MainActivity (首页短视频信息流) 放行出流
                is_live_act = ("LivePlayActivity" in act or "live" in act.lower()) and ("MainActivity" not in act)
                if not is_live_act:
                    self.last_gate_reason = "navigation_failed"
                    logger.warning(f"门禁拦截：检测到画面动态变化 (差分率 {dynamic_ratio:.2%})，但前台并非直播间 ({act})，坚决拦截！")
                    time.sleep(0.3)
                    continue

                self.last_gate_reason = ""
                logger.info(f"开录前置门禁顺利通过！Activity={act}, 白底率={white_ratio:.1%}, 动态帧差率={dynamic_ratio:.2%}")
                return True

            time.sleep(0.3)

        logger.error(f"开录前置门禁超时 ({timeout_sec}s)：画面仍为静止遮罩或未开始推流。")
        return False

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
                    if time.monotonic()-main_seen_since >= 10:
                        return False
                elif not act:
                    main_seen_since = None
                    self.last_gate_reason = 'page_probe_unavailable'
                else:
                    bypasser = PopupBypasser(self.adb_client)
                    bypasser.deadline = min(deadline, time.monotonic()+4)
                    bypasser.detect_and_dismiss_popups()
                self._navigation_sleep(.5, deadline, stop_requested)
            return False
        finally:
            self._operation_deadline = old

    @staticmethod
    def _anchor_entry_point(tree, width, height):
        """仅选择直播顶部主播名字，不点击聊天用户名或关注按钮。"""
        choices = []
        for node in tree.iter('node'):
            key = node.get('resource-id','').rsplit('/',1)[-1]
            if node.get('package') != DOUYIN_PACKAGE or key not in {'user_name','author_name'}:
                continue
            match = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]',node.get('bounds',''))
            if not match:
                continue
            x1,y1,x2,y2 = map(int,match.groups())
            if 0 <= x1 < x2 < width*.65 and height*.03 <= y1 < y2 < height*.18:
                choices.append(((x1+x2)//2,(y1+y2)//2))
        return choices[0] if len(choices)==1 else None

    def observe_live_room(self, deadline, stop_requested):
        """由当前主播资料卡主动取得独立 UID/场次证据，再返回直播画面。

        不查询公网，不读取账号凭证，不点击关注、登录或发送分享。
        优先按新 UI 树中的直播顶部名字和资料头像 bounds 定位入口；
        只有已实测竖屏布局允许左上头像坐标兜底，不点击中部活动区域。
        资料卡点击必须匹配实际头像节点，不猜测关注/发送等按钮。
        """
        from src.worker.popup_bypasser import PopupBypasser
        old = self._operation_deadline
        self._operation_deadline = deadline
        cleanup_deadline = deadline
        operation_deadline = deadline - min(10.0, max(2.0, (deadline-time.monotonic())*.2))
        self._operation_deadline = operation_deadline
        card_open = False
        profile_open = False
        frame = None
        try:
            if stop_requested() or 'LivePlayActivity' not in (self.get_current_focus_activity() or ''):
                return RoomEvidence()
            frame = self._capture_frame_array()
            if frame is None:
                return RoomEvidence()
            h,w = frame.shape[:2]
            if not 1.6 < h/w < 2.3:
                return RoomEvidence(source='unsupported_live_layout')
            bypasser = PopupBypasser(self.adb_client)
            # 先找实际显示的主播入口；刚进直播时顶栏可能还未加载或被清屏隐藏。
            # 已有资料卡则直接读取，不能再次点击背景把它关掉。
            avatars = []
            bypasser.dump_timeout_sec = 8.0
            entry_clicked = False
            # 三次点击后仍须读取一次结果，不能把刚打开的资料卡误当失败。
            for entry_attempt in range(4):
                if stop_requested() or operation_deadline-time.monotonic() < 3:
                    return RoomEvidence(source='cancelled_or_deadline')
                bypasser.deadline = min(operation_deadline, time.monotonic()+11)
                xml = bypasser.get_ui_dump()
                tree = ET.fromstring(xml) if xml else None
                if tree is not None:
                    avatars = [n for n in tree.iter('node') if n.get('package') == DOUYIN_PACKAGE
                               and n.get('content-desc', '').endswith('的头像')]
                    if len(avatars) == 1:
                        break
                    point = self._anchor_entry_point(tree, w, h)
                else:
                    point = None
                if not xml and entry_clicked:
                    # 点击后读取超时：只等结果，不再次盲点把刚打开的卡片关掉。
                    self._navigation_sleep(1.0, operation_deadline, stop_requested)
                    continue
                if entry_attempt == 3:
                    break  # 最后一轮只确认第三次点击的结果，不产生第四次点击。
                if point:
                    self._adb('shell','input','tap',str(point[0]),str(point[1]))
                else:
                    # 只在已适配竖屏直播页尝试左上主播头像；绝不点击中部活动/礼物区。
                    self._adb('shell','input','tap',str(int(w*.0833)),str(int(h*.0755)))
                entry_clicked = True
                self._navigation_sleep(.8, operation_deadline, stop_requested)
            if len(avatars) != 1:
                logger.info('直播页已到达，但主播资料入口尚未就绪，将同页复检')
                return RoomEvidence(source='anchor_card_not_ready')
            card_open = True
            avatar = avatars[0]
            nickname = avatar.get('content-desc', '')[:-3]
            points = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', avatar.get('bounds',''))
            if not points:
                return RoomEvidence(nickname=nickname)
            x1,y1,x2,y2 = map(int,points.groups())
            if not (0 <= x1 < x2 <= w and 0 <= y1 < y2 <= h):
                return RoomEvidence(nickname=nickname)
            before = self._adb('shell', 'dumpsys', 'activity', 'activities')
            active = re.search(r'(?:topResumedActivity|mResumedActivity|ResumedActivity).*?ActivityRecord\{\S+\s+u\d+\s+[^ ]*LivePlayActivity\s+t(\d+)', before)
            if not active:
                return RoomEvidence(nickname=nickname)
            self._adb('shell', 'input', 'tap', str((x1+x2)//2), str((y1+y2)//2))
            profile_open = True
            until = min(operation_deadline, time.monotonic()+12)
            while time.monotonic() < until and not stop_requested():
                after = self._adb('shell','dumpsys','activity','activities')
                evidence = fresh_profile_evidence(before, after, active.group(1), nickname)
                if evidence is not None:
                    return evidence
                self._navigation_sleep(.3, until, stop_requested)
            return RoomEvidence(nickname=nickname)
        except Exception as exc:
            logger.warning('当前房间取证未完成，将尝试纠偏: %s', exc)
            return RoomEvidence()
        finally:
            # 清理最多10秒，取消时也不能把整个剩余取证时限都用于清理。
            cleanup_deadline = min(cleanup_deadline, time.monotonic()+10.0)
            self._operation_deadline = cleanup_deadline
            if profile_open:
                for _ in range(3):
                    act = self.get_current_focus_activity() or ''
                    if 'UserProfileActivity' in act or 'login' in act.lower():
                        self._adb('shell','input','keyevent','4')
                        self._navigation_sleep(.3, cleanup_deadline, lambda: False)
                    else:
                        break
            elif card_open:
                self._adb('shell','input','keyevent','4')
            if 'LivePlayActivity' in (self.get_current_focus_activity() or ''):
                # 返回直播后先识别，只有确定仍有资料卡/礼物展馆才按返回关闭。
                # 禁止“预计空白位置”点击：该位置可能正是展馆/活动入口。
                self.dismiss_live_overlay(cleanup_deadline, lambda: False)
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

    def inspect_live_surface(self, timeout_sec=6.0):
        """后台只读探针；不触发点击，不修改主导航的截止时间。"""
        from src.worker.live_overlay import classify_live_overlay
        from src.worker.popup_bypasser import PopupBypasser
        activity = self.get_current_focus_activity() or ''
        if 'LivePlayActivity' not in activity:
            return {'activity': activity, 'overlay': None, 'ui_available': False, 'observed_at': time.time()}
        reader = PopupBypasser(self.adb_client)
        reader.deadline = time.monotonic() + timeout_sec
        xml = reader.get_ui_dump()
        found = classify_live_overlay(xml)
        return {'activity': activity, 'overlay': found.kind if found else None,
                'markers': list(found.markers) if found else [], 'ui_available': bool(xml),
                'observed_at': time.time()}

    def dismiss_live_overlay(self, deadline, stop_requested, *, initial_observation=None,
                             require_observed_clear=False):
        """仅关闭确定的面板；复用新鲜识别结果，关闭后未知UI不伪报已消失。

        最多两次BACK，所有复检共享截止时间；不重复耗费第一轮UI dump。
        require_observed_clear用于运行期验收，必须看到面板消失的实际UI证据。
        """
        observation = initial_observation
        if observation is not None:
            age = time.time() - observation.get('observed_at', 0)
            if not (0 <= age <= 5 and observation.get('ui_available')
                    and 'LivePlayActivity' in observation.get('activity', '')
                    and 'LivePlayActivity' in (self.get_current_focus_activity() or '')):
                observation = None
        backs = 0
        for _ in range(5):
            if stop_requested() or time.monotonic() >= deadline:
                return False
            current = observation or self.inspect_live_surface(
                timeout_sec=max(.1, min(5, deadline-time.monotonic())))
            observation = None
            if 'LivePlayActivity' not in current['activity']:
                return False
            if not current.get('overlay'):
                if current.get('ui_available'):
                    return True
                if not require_observed_clear and backs == 0:
                    return True  # 初次准入未知不盲点；后续画面与周期探针仍继续。
                self._navigation_sleep(.2, deadline, stop_requested)
                continue
            if backs >= 2:
                return False
            logger.info('关闭已识别直播遮挡: %s，使用返回键', current['overlay'])
            self._adb('shell', 'input', 'keyevent', '4',
                      timeout=min(2, max(.1, deadline-time.monotonic())))
            backs += 1
            if not self._navigation_sleep(.6, deadline, stop_requested):
                return False
        return False

"""抖音直播间 Intent 导航与原地刷新控制器。"""
from pathlib import Path
import logging
import subprocess
import time
import re

from src.mvp.config import get_adb_exe
from src.worker.adb_client import AdbDeviceClient

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

    def _adb(self, *args: str, timeout: float = 5.0) -> str:
        """执行 ADB 命令，遇到断连自动有界重试。"""
        try:
            res = self.adb_client.run_adb(*args, timeout=timeout)
            output = res.stdout.strip() or res.stderr.strip()
            if "not found" in output or "offline" in output:
                self.adb_client.ensure_connected(timeout=3.0)
                res2 = self.adb_client.run_adb(*args, timeout=timeout)
                output = res2.stdout.strip()
            return output
        except Exception as e:
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

    def launch_room(
        self,
        room_id: str,
        web_rid: str | None = None,
        wait_sec: int = 3,
        warmup_first: bool = False,
    ) -> bool:
        """通过 Intent 首次唤起进入指定直播间。

        自适应离线唤起策略：
        1. 当 room_id 为 19 位纯数字（真实在播内部 ID）时，优先通过 snssdk1128://live?room_id={room_id} 秒级直达；
        2. 当 room_id 为展示短号或非 19 位 ID 时，离线自适应通过带应用包名的 Web Intent:
           am start -a android.intent.action.VIEW -d https://live.douyin.com/{target_id} -p com.ss.android.ugc.aweme
           直达应用内 Web Router 解析进入直播间，彻底避免短视频误入；
        3. 若指定了 web_rid 且内部 ID 唤起报错，同样降级到应用内 Web Intent。
        """
        logger.info(f"Worker 唤起直播间: room_id={room_id}, web_rid={web_rid}")
        if warmup_first:
            self._adb("shell", "am", "start", "-n", f"{DOUYIN_PACKAGE}/.main.MainActivity")
            time.sleep(3.5)

        target_web_rid = None
        is_internal_19_id = bool(re.fullmatch(r"\d{19}", str(room_id).strip()))
        if is_internal_19_id:
            intent_uri = f"snssdk1128://live?room_id={room_id}"
            out = self._adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", intent_uri)
            if "Error" in out and web_rid:
                target_web_rid = web_rid
        else:
            target_web_rid = web_rid or room_id

        if target_web_rid:
            if str(target_web_rid).startswith("http://") or str(target_web_rid).startswith("https://"):
                web_uri = str(target_web_rid)
            else:
                web_uri = f"https://live.douyin.com/{target_web_rid}"
            self._adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", web_uri, "-p", DOUYIN_PACKAGE)

        if wait_sec > 0:
            time.sleep(wait_sec)
        self.dismiss_login_prompt()
        pkg = self.get_current_focus_package()
        return pkg == DOUYIN_PACKAGE

    def refresh_current_room(self, room_id: str) -> bool:
        """原地 Intent 刷新当前直播流（严禁手势拖拽，避免切房或清屏）。"""
        logger.info(f"看门狗执行原地 Intent 刷新: room_id={room_id}")
        intent_uri = f"snssdk1128://live?room_id={room_id}"
        out = self._adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", intent_uri)
        return "Error" not in out

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
            text=True
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
            res = subprocess.run(cmd, capture_output=True)
            if res.returncode == 0 and len(res.stdout) > 0:
                img = Image.open(io.BytesIO(res.stdout)).convert("RGB")
                return np.array(img)
        except Exception as e:
            logger.warning(f"内存截帧解码异常: {e}")
        return None

    def verify_preflight_content_gate(self, timeout_sec: float = 8.0) -> bool:
        """开录前置双重门禁：验证前台已脱离登录页且直播流真正开始动态渲染。"""
        import numpy as np
        from src.worker.popup_bypasser import PopupBypasser
        bypasser = PopupBypasser(adb_client=self.adb_client)
        start_t = time.time()
        logger.info(f"正在执行开录前置内容门禁核验 (阈值 {timeout_sec}s)...")

        while time.time() - start_t < timeout_sec:
            # 优先穿透白名单弹窗 (如未成年人模式/青少年模式/隐私协议)
            if bypasser.detect_and_dismiss_popups():
                time.sleep(0.5)
                continue

            act = self.get_current_focus_activity() or ""
            if "DYLoginActivity" in act or "login" in act.lower():
                self.dismiss_login_prompt()
                time.sleep(0.3)
                continue

            # 硬卡口：若前台处于短视频推荐流 MainActivity，坚决拦截，拒绝截帧浪费
            if "MainActivity" in act:
                logger.warning(f"门禁拦截：前台当前处于首页短视频推荐流 ({act})，坚决拦截出流！")
                time.sleep(0.3)
                continue

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
                    logger.warning(f"门禁拦截：检测到画面动态变化 (差分率 {dynamic_ratio:.2%})，但前台并非直播间 ({act})，坚决拦截！")
                    time.sleep(0.3)
                    continue

                logger.info(f"开录前置门禁顺利通过！Activity={act}, 白底率={white_ratio:.1%}, 动态帧差率={dynamic_ratio:.2%}")
                return True

            time.sleep(0.3)

        logger.error(f"开录前置门禁超时 ({timeout_sec}s)：画面仍为静止遮罩或未开始推流。")
        return False

    def detect_current_room_identity(self) -> tuple[str | None, str | None]:
        """从模拟器当前前台页面提取独立的主播身份证据 (UID 与 昵称)。

        绝不拿目标待唤起的 ID 冒充观测结果！
        若无法可靠取得稳定数字 UID，则 UID 返回 None，进入 UNCONFIRMED 判定流程。
        """
        try:
            # 尝试通过 UI Automator dump 获取当前前台 UI 结构
            res = self.adb_client.run_adb("exec-out", "uiautomator", "dump", "/dev/tty", timeout=4.0)
            xml_content = res.stdout if (res.exit_code == 0 and "<node" in res.stdout) else ""
            if not xml_content:
                # 尝试标准 dump
                self.adb_client.run_adb("shell", "uiautomator", "dump", "/sdcard/wp_ident.xml", timeout=5.0)
                pull_res = self.adb_client.run_adb("shell", "cat", "/sdcard/wp_ident.xml", timeout=3.0)
                xml_content = pull_res.stdout if pull_res.exit_code == 0 else ""

            if xml_content:
                # 寻找主播昵称节点
                nick_match = re.search(r'<node[^>]*?resource-id="[^"]*?(?:user_name|author_name|name)[^"]*"[^>]*?text="([^"]+)"', xml_content)
                detected_nick = nick_match.group(1).strip() if nick_match else None

                # 寻找数字 UID（若有展示数字 ID）
                uid_match = re.search(r'<node[^>]*?resource-id="[^"]*?(?:display_id|unique_id|author_id)[^"]*"[^>]*?text="(\d+)"', xml_content)
                detected_uid = uid_match.group(1).strip() if uid_match else None

                return detected_uid, detected_nick
        except Exception as e:
            logger.warning(f"探测前台主播身份证据异常: {e}")

        # 未能取得或无法可靠识别时诚实返回 None，进入 UNCONFIRMED 分支
        return None, None

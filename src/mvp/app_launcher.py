"""抖音 App Intent 深度链接直达与前台页面控制器。"""
from pathlib import Path
import logging
import subprocess
import time
import re

from src.mvp.config import get_adb_exe

logger = logging.getLogger(__name__)

DOUYIN_PACKAGE = "com.ss.android.ugc.aweme"


class AppLauncher:
    """负责通过 ADB 调起抖音直播间及处理系统权限弹窗。"""

    def __init__(self, serial: str, adb_exe: Path | None = None):
        self.serial = serial
        self.adb_exe = adb_exe or get_adb_exe()
        if not self.adb_exe or not self.adb_exe.exists():
            raise FileNotFoundError(f"未找到 adb.exe: {self.adb_exe}")

    def _adb(self, *args: str) -> str:
        """执行 ADB 命令，若遇到设备离线或断开则自动重连。"""
        cmd = [str(self.adb_exe), "-s", self.serial, *args]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        output = res.stdout.strip() or res.stderr.strip()
        if "not found" in output or "offline" in output:
            # 自动执行 connect 并重试一次
            subprocess.run([str(self.adb_exe), "connect", self.serial], capture_output=True, text=True)
            res2 = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            output = res2.stdout.strip()
        return output

    def get_current_focus_package(self) -> str | None:
        """获取当前前台聚焦的应用包名。"""
        out = self._adb("shell", "dumpsys", "window", "windows")
        m = re.search(r"mCurrentFocus=.*?([a-zA-Z0-9._]+)/", out)
        if m:
            return m.group(1)
        # 兼容备用命令
        out2 = self._adb("shell", "dumpsys", "activity", "activities")
        m2 = re.search(r"ResumedActivity: ActivityRecord\{.*?\s+([a-zA-Z0-9._]+)/", out2)
        if m2:
            return m2.group(1)
        return None

    def launch_live_room(self, room_id: str, web_rid: str | None = None, wait_sec: int = 3) -> bool:
        """通过 Intent 唤起进入指定直播间。"""
        logger.info(f"正在唤起直播间: room_id={room_id}, web_rid={web_rid}")

        # 方式 1: snssdk1128://live?room_id=...
        intent_uri = f"snssdk1128://live?room_id={room_id}"
        out = self._adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", intent_uri)
        logger.info(f"Intent 唤起结果: {out}")

        # 如果失败且有 web_rid，尝试 web 深度链接
        if "Error" in out and web_rid:
            web_uri = f"https://live.douyin.com/{web_rid}"
            out2 = self._adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", web_uri)
            logger.info(f"Web Intent 唤起备用结果: {out2}")

        # 等待页面加载
        if wait_sec > 0:
            time.sleep(wait_sec)
        pkg = self.get_current_focus_package()
        logger.info(f"当前前台包名: {pkg}")
        return pkg == DOUYIN_PACKAGE

    def auto_dismiss_capture_dialog(self) -> bool:
        """检测并自动点击放行 Android 系统投屏/音频录制授权弹窗。"""
        # 很多 Android 12/15 会弹出 "是否允许录制/投屏"，包含 "立即开始" 按钮 (android:id/button1)
        # 直接发送 Enter 键或模拟点击右下角按钮
        try:
            self._adb("shell", "input", "keyevent", "66")  # KEYCODE_ENTER
            return True
        except Exception as e:
            logger.warning(f"自动放行授权弹窗失败: {e}")
            return False

    def capture_screenshot(self, save_path: Path) -> bool:
        """截取当前模拟器屏幕画面并拉取到本地保存（用于状态验证与证据归档）。"""
        save_path.parent.mkdir(parents=True, exist_ok=True)
        remote_path = "/sdcard/screen_probe.png"
        self._adb("shell", "screencap", "-p", remote_path)
        res = subprocess.run(
            [str(self.adb_exe), "-s", self.serial, "pull", remote_path, str(save_path)],
            capture_output=True,
            text=True
        )
        self._adb("shell", "rm", "-f", remote_path)
        return save_path.exists()

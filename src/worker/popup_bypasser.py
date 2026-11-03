"""开录前常见干扰弹窗白名单自动穿透器 (Ticket 08)。"""
import logging
import re
from typing import Optional

from src.worker.adb_client import AdbDeviceClient

logger = logging.getLogger("PopupBypasser")


class PopupBypasser:
    """自动检测并穿透开录前常见全屏或居中系统干扰弹窗。"""

    # 白名单关键字与对应关闭按钮文案
    WHITELIST_PATTERNS = [
        # (弹窗内容关键字正则, 关闭按钮文案正则)
        (r"青少年模式", r"我知道了|开启|进入青少年模式"),
        (r"更新|发现新版本", r"以后再说|暂不更新|取消"),
        (r"服务协议|隐私政策", r"同意并继续|同意|我知道了"),
        (r"通讯录|朋友推荐", r"拒绝|暂不|取消"),
    ]

    def __init__(self, adb_client: AdbDeviceClient):
        self.adb = adb_client

    def get_ui_dump(self) -> str:
        """获取当前屏幕 UI Automator XML 结构。"""
        try:
            # 优先使用 exec-out 或 uiautomator dump
            res = self.adb.run_adb("exec-out", "uiautomator", "dump", "/dev/tty", timeout=4.0)
            if res.exit_code == 0 and "<node" in res.stdout:
                return res.stdout
        except Exception:
            pass

        # 降级尝试标准 dump
        try:
            self.adb.run_adb("shell", "uiautomator", "dump", "/sdcard/window_dump.xml", timeout=5.0)
            pull_res = self.adb.run_adb("shell", "cat", "/sdcard/window_dump.xml", timeout=3.0)
            if pull_res.exit_code == 0:
                return pull_res.stdout
        except Exception as e:
            logger.warning(f"获取 UI Dump 失败: {e}")

        return ""

    def detect_and_dismiss_popups(self) -> bool:
        """检测并尝试穿透当前屏幕上的白名单干扰弹窗。

        若发现并成功点击穿透返回 True，无弹窗或无法穿透返回 False。
        """
        ui_xml = self.get_ui_dump()
        if not ui_xml:
            return False

        for popup_re, btn_re in self.WHITELIST_PATTERNS:
            if re.search(popup_re, ui_xml):
                logger.info(f"检测到已知白名单弹窗: pattern='{popup_re}'")
                # 寻找对应的确认/关闭按钮 bounds
                # 匹配格式: <node ... text="..." ... bounds="[x1,y1][x2,y2]"
                node_matches = re.finditer(
                    r'<node[^>]*?text="([^"]*)"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    ui_xml
                )
                for m in node_matches:
                    text, x1, y1, x2, y2 = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
                    if re.search(btn_re, text):
                        cx = (x1 + x2) // 2
                        cy = (y1 + y2) // 2
                        logger.info(f"命中白名单关闭按钮: text='{text}', 坐标=({cx}, {cy})，执行穿透点击...")
                        self.adb.run_adb("shell", "input", "tap", str(cx), str(cy), timeout=3.0)
                        return True

        return False

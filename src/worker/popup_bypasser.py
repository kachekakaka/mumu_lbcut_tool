"""开录前常见干扰弹窗白名单自动穿透器 (Ticket 08)。"""
import logging
import re
import time
import uuid
import xml.etree.ElementTree as ET
from typing import Optional

from src.worker.adb_client import AdbDeviceClient

logger = logging.getLogger("PopupBypasser")


class PopupBypasser:
    """自动检测并穿透开录前常见全屏或居中系统干扰弹窗。"""

    # 白名单关键字与对应关闭按钮文案
    WHITELIST_PATTERNS = [
        # (弹窗内容关键字正则, 关闭按钮文案正则)
        (r"青少年模式|未成年人模式", r"不再提醒|我知道了|暂不开启|取消"),
        (r"更新|发现新版本", r"以后再说|暂不更新|取消"),
        (r"服务协议|隐私政策", r"同意并继续|同意|我知道了"),
        (r"通讯录|朋友推荐", r"拒绝|暂不|取消"),
    ]

    def __init__(self, adb_client: AdbDeviceClient):
        self.adb = adb_client
        self.deadline = None
        self.dump_timeout_sec = 5.0

    def _budget(self, maximum: float) -> float:
        if self.deadline is None:
            return maximum
        remaining = self.deadline - time.monotonic()
        if remaining < 1.0:
            raise TimeoutError("弹窗探针总预算耗尽")
        return min(maximum, remaining)

    def get_ui_dump(self) -> str:
        """直接执行 UIAutomator，再独立读取其输出文件。

        MuMu Android 15 上 UIAutomator 可能在 XML 已落盘后以 139 退出；
        不能把进程退出码直接当成 dump 失败。也不能把 uiautomator 放进
        adb shell sh -c：该启动方式在部分 MuMu 镜像中无法正确初始化。
        """
        remote = '/sdcard/wp_ui_' + uuid.uuid4().hex + '.xml'
        try:
            # UUID 路径确保不会误读历史文件；直接调用 wrapper 保留 Android 环境。
            result = self.adb.run_adb(
                'shell', 'uiautomator', 'dump', remote,
                timeout=self._budget(self.dump_timeout_sec + 2.0),
                check=False,
            )
            if result.exit_code != 0:
                logger.debug(
                    'UIAutomator returned %s; checking whether a complete XML was written',
                    result.exit_code,
                )

            read_result = self.adb.run_adb(
                'shell', 'cat', remote,
                timeout=self._budget(3.0),
                check=False,
            )
            if read_result.exit_code != 0:
                return ''
            output = read_result.stdout or ''
            begin = output.find('<hierarchy')
            end = output.rfind('</hierarchy>')
            if begin < 0 or end < begin:
                return ''
            xml = output[begin:end + len('</hierarchy>')]
            root = ET.fromstring(xml)
            if root.tag != 'hierarchy' or not any(node.tag == 'node' for node in root.iter()):
                return ''
            return xml
        except Exception as exc:
            logger.debug('本次页面结构不可读取: %s', exc)
            return ''
        finally:
            # 每次使用唯一文件名，清理失败也不会污染下一轮读取。
            try:
                self.adb.run_adb('shell', 'rm', '-f', remote, timeout=1.5)
            except Exception:
                pass

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
                        self.adb.run_adb("shell", "input", "tap", str(cx), str(cy), timeout=self._budget(3.0))
                        return True

        return False

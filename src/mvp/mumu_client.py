"""MuMu 模拟器管理与命令行客户端。"""
from pathlib import Path
import json
import logging
import os
import subprocess
import time
import re
import threading

from src.mvp.config import get_mumu_manager_exe, get_adb_exe

logger = logging.getLogger(__name__)

LAUNCH_RETRY_WINDOW_SEC = 45
LAUNCH_RETRY_INTERVAL_SEC = 3
MANAGER_TIMEOUT_SEC = 10


class MuMuClient:
    """封装 MuMuManager.exe 的生命周期与控制接口。"""

    def __init__(self, manager_exe: Path | None = None, adb_exe: Path | None = None):
        manager = manager_exe or get_mumu_manager_exe()
        adb = adb_exe or get_adb_exe()
        if not manager or not Path(manager).exists():
            raise FileNotFoundError(f"未找到 MuMuManager.exe: {manager}")
        if not adb or not Path(adb).exists():
            raise FileNotFoundError(f"未找到 adb.exe: {adb}")
        self.manager_exe = Path(manager).resolve()
        self.adb_exe = Path(adb).resolve()

    def _run_manager(self, *args: str, timeout: float = MANAGER_TIMEOUT_SEC, check: bool = False) -> str:
        """执行 MuMuManager 命令并返回 stdout。"""
        cmd = [str(self.manager_exe), *args]
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=self.manager_exe.parent,
        )
        if res.returncode != 0:
            logger.warning(f"MuMuManager 执行返回码 {res.returncode}: {res.stderr.strip() or res.stdout.strip()}")
            if check:
                raise RuntimeError(f"MuMuManager 查询失败 ({res.returncode}): {res.stderr.strip() or res.stdout.strip()}")
        return res.stdout.strip()

    def get_all_players(self) -> dict[str, dict]:
        """获取所有实例信息字典。"""
        out = self._run_manager("info", "-v", "all", check=True)
        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:
            raise ValueError(f"解析 MuMuManager info 失败: {out}") from exc
        if not isinstance(data, dict) or any(not isinstance(v, dict) for v in data.values()):
            raise ValueError(f"MuMuManager info 不是实例集合: {out}")
        return data

    def list_available_instances(self) -> list[dict]:
        """获取所有可用模拟器实例列表（供调度平台初始化实例池使用）。

        返回格式规范：
        [
            {
                "vmindex": 2,
                "name": "lb1",
                "is_running": False,
                "android_version": "15.0",
                "adb_host": "127.0.0.1",
                "adb_port": 16448
            }, ...
        ]
        """
        all_info = self.get_all_players()
        instances = []
        for key, val in all_info.items():
            try:
                idx = int(val.get("index", key))
            except (ValueError, TypeError) as exc:
                raise ValueError(f"MuMuManager 实例编号无效: {key}") from exc
            if idx < 0:
                raise ValueError(f"MuMuManager 实例编号不能为负数: {idx}")
            is_running = bool(val.get("is_android_started", False) or val.get("is_process_started", False))
            instances.append({
                "vmindex": idx,
                "name": val.get("name", f"player_{idx}"),
                "is_running": is_running,
                "android_started": bool(val.get("is_android_started", False)),
                "android_version": val.get("android_version", "12.0"),
                "adb_host": val.get("adb_host_ip", "127.0.0.1"),
                "adb_port": val.get("adb_port"),
                "raw_info": val,
            })
        instances.sort(key=lambda x: x["vmindex"])
        return instances

    def get_player_info(self, vmindex: int, *, timeout: float = MANAGER_TIMEOUT_SEC) -> dict:
        """获取单个实例信息字典。"""
        out = self._run_manager("info", "-v", str(vmindex), timeout=timeout, check=True)
        try:
            data = json.loads(out)
            # 有时返回以 str(vmindex) 为 key，有时直接是对象
            if str(vmindex) in data:
                return data[str(vmindex)]
            return data
        except json.JSONDecodeError:
            logger.error(f"解析实例 {vmindex} 信息失败: {out}")
            return {}

    def is_android_started(self, vmindex: int, *, timeout: float = MANAGER_TIMEOUT_SEC) -> bool:
        """检查 Android 系统是否已完全启动。"""
        info = self.get_player_info(vmindex, timeout=timeout)
        return bool(info.get("is_android_started", False))

    def ensure_main_process_running(self, *, stop_event=None, timeout_sec: float = 15) -> bool:
        """确保 MuMu 主程序 (MuMuNxMain.exe) 处于运行状态。"""
        # 检查进程
        stop_event = stop_event or threading.Event()
        if stop_event.is_set():
            return False
        deadline = time.monotonic() + timeout_sec
        main_exe = (self.manager_exe.parent / "MuMuNxMain.exe").resolve()
        if not main_exe.exists():
            return True

        res = subprocess.run(
            ["tasklist", "/fi", "IMAGENAME eq MuMuNxMain.exe"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=min(MANAGER_TIMEOUT_SEC, timeout_sec),
        )
        stdout_text = res.stdout or ""
        if "MuMuNxMain.exe" not in stdout_text:
            if stop_event.is_set() or time.monotonic() >= deadline:
                return False
            logger.info("MuMu 主程序未启动，正在独立拉起主程序...")
            creationflags = 0
            if os.name == "nt":
                creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            subprocess.Popen(
                [str(main_exe), "--from-shortcut"],
                cwd=str(self.manager_exe.parent),
                creationflags=creationflags,
                close_fds=True,
            )
            if stop_event.wait(min(5, max(0, deadline - time.monotonic()))):
                return False
        return not stop_event.is_set()

    def launch_player(self, vmindex: int, timeout_sec: int = 60, *, stop_event=None) -> bool:
        """有界启动：主程序准备 15 秒、受理重试 45 秒、Android 就绪默认 60 秒；停服不关机。"""
        stop_event = stop_event or threading.Event()
        deadline = time.monotonic() + 15 + LAUNCH_RETRY_WINDOW_SEC + timeout_sec

        def remaining(until=deadline):
            if stop_event.is_set():
                raise InterruptedError("开机被停服中断")
            budget = min(deadline, until) - time.monotonic()
            if budget <= 0:
                raise TimeoutError("开机超过截止时间")
            return min(MANAGER_TIMEOUT_SEC, budget)

        def pause(seconds, until=deadline):
            remaining(until)
            if stop_event.wait(min(seconds, max(0, min(deadline, until) - time.monotonic()))):
                raise InterruptedError("开机被停服中断")

        try:
            remaining()
            if not self.ensure_main_process_running(stop_event=stop_event, timeout_sec=min(15, deadline-time.monotonic())):
                return False
            if self.is_android_started(vmindex, timeout=remaining()):
                return True
            logger.info("正在启动 MuMu 实例 %s...", vmindex)
            retry_until = min(deadline, time.monotonic() + LAUNCH_RETRY_WINDOW_SEC)
            while True:
                output = self._run_manager("control", "-v", str(vmindex), "launch", timeout=remaining(retry_until))
                if self._manager_errcode(output) == 0:
                    break
                pause(LAUNCH_RETRY_INTERVAL_SEC, retry_until)
                if self.is_android_started(vmindex, timeout=remaining(retry_until)):
                    return True
            ready_until = min(deadline, time.monotonic() + timeout_sec)
            while True:
                pause(3, ready_until)
                if self.is_android_started(vmindex, timeout=remaining(ready_until)):
                    return True
        except (InterruptedError, TimeoutError, subprocess.TimeoutExpired, OSError, ValueError, RuntimeError) as exc:
            logger.warning("实例 %s 开机未完成: %s", vmindex, exc)
            return False

    @staticmethod
    def _manager_errcode(output: str) -> int:
        """读取 MuMuManager JSON 输出中的 errcode；无此字段视为成功。"""
        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            return 0
        try:
            return int(data.get("errcode", 0)) if isinstance(data, dict) else 0
        except (TypeError, ValueError):
            return 0

    def shutdown_player(self, vmindex: int) -> bool:
        """关闭指定实例。"""
        logger.info(f"正在关闭 MuMu 实例 {vmindex}...")
        self._run_manager("control", "-v", str(vmindex), "shutdown")
        return True

    def mute_player(self, vmindex: int, mute: bool = True) -> bool:
        """设置模拟器自身声卡外放静音（拦截外放声音，不影响 Android 内部音频流捕获）。"""
        val = "true" if mute else "false"
        logger.info(f"设置实例 {vmindex} 系统外放静音: {val}")
        res = self._run_manager("setting", "-v", str(vmindex), "-k", "system_volume_close", "-val", val)
        return '"system_volume_close"' in res or "system_volume_close" in res

    def show_player_window(self, vmindex: int) -> bool:
        """显示模拟器前台窗口，确保 DWM 渲染交换链维持满帧率。"""
        self._run_manager("control", "-v", str(vmindex), "show_window")
        return True

    def hide_player_window(self, vmindex: int) -> bool:
        """隐藏模拟器窗口，实现完全后台静默运行。"""
        self._run_manager("control", "-v", str(vmindex), "hide_window")
        return True

    def get_player_hwnd(self, vmindex: int) -> int | None:
        """获取指定模拟器实例的主窗口句柄 (HWND)。"""
        out = self._run_manager("info", "-v", str(vmindex))
        try:
            data = json.loads(out)
            if str(vmindex) in data:
                data = data[str(vmindex)]
            main_wnd_str = data.get("main_wnd")
            if main_wnd_str:
                hwnd = int(main_wnd_str, 16)
                if os.name == "nt":
                    import ctypes
                    if ctypes.windll.user32.IsWindow(hwnd):
                        return hwnd
                return hwnd
        except Exception as e:
            logger.debug(f"获取实例 {vmindex} 窗口句柄失败: {e}")
        return None

    def layout_player_window(
        self,
        vmindex: int,
        pos_x: int,
        pos_y: int,
        size_w: int | None = None,
        size_h: int | None = None,
    ) -> bool:
        """设置模拟器窗口的位置与尺寸。"""
        cmd = ["control", "-v", str(vmindex), "layout_window", "--pos_x", str(pos_x), "--pos_y", str(pos_y)]
        if size_w is not None:
            cmd.extend(["--size_w", str(size_w)])
        if size_h is not None:
            cmd.extend(["--size_h", str(size_h)])
        self._run_manager(*cmd)

        if os.name == "nt":
            hwnd = self.get_player_hwnd(vmindex)
            if hwnd:
                import ctypes
                SWP_NOZORDER = 0x0004
                SWP_NOACTIVATE = 0x0010
                SWP_ASYNCWINDOWPOS = 0x4000
                flags = SWP_NOZORDER | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS
                if size_w is None or size_h is None:
                    flags |= 0x0001  # SWP_NOSIZE
                w = size_w or 0
                h = size_h or 0
                ctypes.windll.user32.SetWindowPos(hwnd, 0, pos_x, pos_y, w, h, flags)
        return True

    def send_player_to_bottom(
        self,
        vmindex: int,
        pos_x: int | None = None,
        pos_y: int | None = None,
    ) -> bool:
        """将模拟器窗口放置在所有其他应用窗口的最底层 (HWND_BOTTOM)，且不抢占前台焦点。

        参数:
        - vmindex: 实例索引
        - pos_x: 可选指定 X 坐标（若指定则同步平移窗口，若为 None 则保持当前坐标）
        - pos_y: 可选指定 Y 坐标

        效果：
        1. 保持窗口显示 (Show) 状态，确保 Windows DWM 分配满帧率渲染视口；
        2. 沉入所有普通窗口下方，不遮挡用户的日常办公/网页操作，也不抢占键盘鼠标焦点；
        3. 可根据调度平台需要指定桌面位置坐标（如安全复位到可视区域）。
        """
        self.show_player_window(vmindex)
        if pos_x is not None or pos_y is not None:
            tx = pos_x if pos_x is not None else 0
            ty = pos_y if pos_y is not None else 0
            self.layout_player_window(vmindex, pos_x=tx, pos_y=ty)

        if os.name == "nt":
            hwnd = self.get_player_hwnd(vmindex)
            if hwnd:
                import ctypes
                HWND_BOTTOM = 1
                SWP_NOSIZE = 0x0001
                SWP_NOACTIVATE = 0x0010
                SWP_ASYNCWINDOWPOS = 0x4000
                flags = SWP_NOSIZE | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS
                if pos_x is None and pos_y is None:
                    flags |= 0x0002  # SWP_NOMOVE
                tx = pos_x or 0
                ty = pos_y or 0
                ctypes.windll.user32.SetWindowPos(
                    hwnd,
                    HWND_BOTTOM,
                    tx,
                    ty,
                    0,
                    0,
                    flags,
                )
                logger.info(f"已将实例 {vmindex} 窗口 (HWND: {hex(hwnd)}) 下沉置于其他窗口底层。")
                return True
        logger.info(f"已显示实例 {vmindex} 窗口（当前系统不支持或未获取到底层 HWND）。")
        return True

    def move_player_offscreen(
        self,
        vmindex: int,
        pos_x: int | None = None,
        pos_y: int | None = None,
    ) -> bool:
        """将模拟器窗口移出主屏幕工作区边缘（屏幕外位置），保持 DWM 视口激活但不在屏幕上可见。

        默认坐标为 (-3200, 0)，确保移出绝大多数常见主副屏幕可视区域。
        """
        self.show_player_window(vmindex)
        target_x = pos_x if pos_x is not None else -3200
        target_y = pos_y if pos_y is not None else 0
        logger.info(f"正在将实例 {vmindex} 窗口移出主工作区边缘至坐标 ({target_x}, {target_y})...")
        return self.layout_player_window(vmindex, pos_x=target_x, pos_y=target_y)

    def apply_window_display_mode(
        self,
        vmindex: int,
        window_mode: str = "foreground",
        pos_x: int | None = None,
        pos_y: int | None = None,
    ) -> bool:
        """统一应用窗口显示模式：
        - 'foreground': 正常前台展示；
        - 'bottom': 放置在其他应用窗口底层且不抢焦点；
        - 'offscreen': 移出主工作区边缘（支持自定义 pos_x, pos_y）；
        - 'hidden' / 'background': 完全后台隐藏静默（帧率受限）。
        """
        mode = window_mode.lower().strip()
        if mode in ("hidden", "background"):
            return self.hide_player_window(vmindex)
        elif mode == "bottom":
            return self.send_player_to_bottom(vmindex, pos_x=pos_x, pos_y=pos_y)
        elif mode in ("offscreen", "edge"):
            return self.move_player_offscreen(vmindex, pos_x=pos_x, pos_y=pos_y)
        else:
            return self.show_player_window(vmindex)

    def optimize_performance_for_recording(
        self,
        vmindex: int,
        mute: bool = True,
        disable_dynamic_fps: bool = True,
        show_window: bool | None = None,
        window_mode: str = "foreground",
        pos_x: int | None = None,
        pos_y: int | None = None,
    ) -> bool:
        """为录播环境执行模拟器底层调优：
        1. 拦截 Windows 声卡外放静音，避免杂音干扰电脑；
        2. 禁用动态省电降频 (dynamic_adjust_frame_rate: false)，杜绝掉帧卡顿；
        3. 视口控制：支持 foreground / bottom / offscreen / hidden 多种模式。
        """
        if mute:
            self.mute_player(vmindex, mute=True)
        if disable_dynamic_fps:
            logger.info(f"设置实例 {vmindex} 禁用省电降频: dynamic_adjust_frame_rate = false, max_frame_rate = 60")
            self._run_manager("setting", "-v", str(vmindex), "-k", "dynamic_adjust_frame_rate", "-val", "false")
            self._run_manager("setting", "-v", str(vmindex), "-k", "dynamic_low_frame_rate_limit", "-val", "30")
            self._run_manager("setting", "-v", str(vmindex), "-k", "max_frame_rate", "-val", "60")
            self._run_manager("setting", "-v", str(vmindex), "-k", "renderer_strategy", "-val", "perf")
        # MuMu 把 dx 存为“稳定”渲染（OpenGL）；Vulkan 下两路录制曾多次显卡驱动崩溃。下次开机生效。
        self._run_manager("setting", "-v", str(vmindex), "-k", "renderer_mode", "-val", "dx")

        # 兼容旧代码显式传入 show_window 的情况
        effective_mode = window_mode
        if show_window is False:
            effective_mode = "hidden"
        elif show_window is True and effective_mode == "hidden":
            effective_mode = "foreground"

        self.apply_window_display_mode(vmindex, window_mode=effective_mode, pos_x=pos_x, pos_y=pos_y)
        return True

    def get_adb_serial(self, vmindex: int) -> str:
        """获取实例对应的 ADB 地址与端口（如 127.0.0.1:16448）。"""
        info = self.get_player_info(vmindex)
        host = info.get("adb_host_ip") or "127.0.0.1"
        port = info.get("adb_port")
        if port:
            return f"{host}:{port}"

        # 尝试从 vm_config.json 读取
        config_path = self.manager_exe.parent.parent / "vms" / f"MuMuPlayer-15.0-{vmindex}" / "configs" / "vm_config.json"
        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    cfg_port = cfg.get("vm", {}).get("nat", {}).get("port_forward", {}).get("adb", {}).get("host_port")
                    if cfg_port:
                        return f"{host}:{cfg_port}"
            except Exception:
                pass

        # 官方规则默认计算
        calc_port = 16384 + 32 * int(vmindex)
        return f"{host}:{calc_port}"

    def connect_adb(self, vmindex: int, retries: int = 5, retry_interval_sec: float = 1.5) -> str | None:
        """建立 ADB 连接并返回设备 serial。"""
        serial = self.get_adb_serial(vmindex)
        logger.info(f"正在连接实例 {vmindex} 的 ADB: {serial}")

        for attempt in range(1, retries + 1):
            # 先通过 manager 触发连接
            self._run_manager("adb", "-v", str(vmindex), "-c", "connect")

            # 显式执行 adb connect
            subprocess.run([str(self.adb_exe), "connect", serial], capture_output=True, text=True, cwd=self.adb_exe.parent)

            # 检查 devices
            res = subprocess.run([str(self.adb_exe), "devices"], capture_output=True, text=True, cwd=self.adb_exe.parent)
            lines = [line.strip() for line in res.stdout.strip().splitlines() if line.strip()]

            for line in lines[1:]:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == serial and parts[1] == "device":
                    return serial

            if attempt < retries:
                time.sleep(retry_interval_sec)

        # 严禁模糊匹配或串绑其他在线设备
        logger.warning(f"实例 {vmindex} 的目标设备 {serial} 未处于 device 状态")
        return None

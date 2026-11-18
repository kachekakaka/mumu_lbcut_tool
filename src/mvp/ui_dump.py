"""MuMu Android 的 UI 导出命令；32 位入口避开实测的 64 位退出崩溃。"""
import re


UI_DUMP_COMMAND = (
    'CLASSPATH=/system/framework/android.test.runner.jar:/system/framework/uiautomator.jar '
    '/system/bin/app_process32 /system/bin com.android.commands.uiautomator.Launcher dump '
)


def ui_dump_args(remote: str) -> tuple[str, str]:
    """只接受本工具使用的绝对 XML 路径，不把调用方文字变成 shell 语法。"""
    if not re.fullmatch(r'/(?:data/local/tmp|sdcard)/[A-Za-z0-9_-]+\.xml', remote):
        raise ValueError('UI dump 临时路径无效')
    # ADB shell 接收完整命令，不再套一层 sh -c，也不修改设备全局环境。
    return 'shell', UI_DUMP_COMMAND + remote

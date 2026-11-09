"""直播内遮挡面板的只读识别：读取窗口管理器状态，不等待页面空闲。

MuMu Android 15 的直播页持续刷新，UIAutomator 等不到空闲状态，几乎取不到页面结构。
实机中抖音直播面板（如主播资料卡）是挂在 LivePlayActivity 上的独立应用窗口，并持有输入焦点；
只有这种情况下 BACK 才只送达面板，不会退出直播间。
"""
from dataclasses import dataclass
import re

PACKAGE = 'com.ss.android.ugc.aweme'
LIVE_ACTIVITY = 'LivePlayActivity'
PANEL_WINDOW = 'live_panel_window'

_HEADER = re.compile(r'^  Window #\d+ Window\{(\w+) u\d+ ([^}]*)\}:[ \t]*$', re.M)


@dataclass(frozen=True)
class AppWindow:
    window_id: str
    title: str
    package: str
    window_type: str
    activity_record: str
    visible: bool
    frame: str


@dataclass(frozen=True)
class LiveSurface:
    activity: str
    overlay: str | None
    markers: tuple[str, ...]
    available: bool


def parse_windows(text: str) -> list[AppWindow]:
    """解析 `dumpsys window windows` 的窗口列表（自顶向下）。"""
    text = (text or '').replace('\r', '')
    heads = list(_HEADER.finditer(text))
    windows = []
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
        body = text[head.end():end]

        def field(pattern):
            match = re.search(pattern, body)
            return match.group(1) if match else ''
        windows.append(AppWindow(
            window_id=head.group(1), title=head.group(2),
            package=field(r'\bpackage=(\S+)'), window_type=field(r'\bty=(\w+)'),
            activity_record=field(r'mActivityRecord=ActivityRecord\{(\w+)'),
            visible=bool(re.search(r'^\s*isVisible=true\s*$', body, re.M)),
            frame=field(r'\bframe=(\[-?\d+,-?\d+\]\[-?\d+,-?\d+\])')))
    return windows


def parse_focus(text: str) -> tuple[str | None, str, str]:
    """从 `dumpsys window displays` 读取焦点窗口 ID、焦点 Activity 记录 ID 与类名。"""
    text = (text or '').replace('\r', '')
    window = re.search(r'mCurrentFocus=Window\{(\w+) u\d+ [^}]*\}', text)
    app = re.search(r'mFocusedApp=ActivityRecord\{(\w+) u\d+ ([\w.]+)/([\w.$]+)', text)
    if not app:
        return (window.group(1) if window else None), '', ''
    package, name = app.group(2), app.group(3)
    activity = package + name if name.startswith('.') else name
    return (window.group(1) if window else None), app.group(1), activity


def classify_live_surface(windows_text: str, displays_text: str) -> LiveSurface:
    """焦点在直播 Activity 的非基础窗口上才算遮挡面板；证据不一致一律视为未知。"""
    focus_id, app_record, activity = parse_focus(displays_text)
    windows = parse_windows(windows_text)
    if not activity or not windows:
        return LiveSurface(activity, None, (), False)
    if LIVE_ACTIVITY not in activity:
        return LiveSurface(activity, None, (), True)
    focused = next((w for w in windows if w.window_id == focus_id), None)
    if focused is None or focused.package != PACKAGE:
        # 焦点为空（切换中）或在系统窗口上：BACK 去向不确定，不能当作干净页面。
        return LiveSurface(activity, None, ('focus=' + (focused.title if focused else 'none'),), False)
    if focused.activity_record != app_record:
        return LiveSurface(activity, None, ('focus_record_mismatch',), False)
    if focused.window_type == 'BASE_APPLICATION':
        return LiveSurface(activity, None, (), LIVE_ACTIVITY in focused.title)
    if not focused.visible:
        return LiveSurface(activity, None, ('focus_not_visible',), False)
    return LiveSurface(activity, PANEL_WINDOW,
                       ('type=' + focused.window_type, 'frame=' + focused.frame), True)

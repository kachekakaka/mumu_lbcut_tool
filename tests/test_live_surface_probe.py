"""直播面板窗口探针：真实 MuMu Android 15 窗口转储，确认 BACK 只在面板持焦时发送。"""
from pathlib import Path
import time
from types import SimpleNamespace
import pytest

from src.worker.live_overlay import PANEL_WINDOW, classify_live_surface, parse_windows
from src.worker.navigator import WorkerNavigator

FIXTURES = Path(__file__).parent / 'fixtures'
OPEN = (FIXTURES / 'live_windows_panel_open.txt').read_text(encoding='utf8')
CLEAN = (FIXTURES / 'live_windows_clean.txt').read_text(encoding='utf8')
LIVE_APP = 'mFocusedApp=ActivityRecord{233513554 u0 com.ss.android.ugc.aweme/.live.LivePlayActivity t83}'


def displays(window_id, app=LIVE_APP, title='com.ss.android.ugc.aweme/com.ss.android.ugc.aweme.live.LivePlayActivity'):
    focus = f'  mCurrentFocus=Window{{{window_id} u0 {title}}}' if window_id else '  mCurrentFocus=null'
    return f'{focus}\r\n  {app}\r\n'


def test_parse_windows_reads_real_dump_fields():
    rows = {w.window_id: w for w in parse_windows(OPEN.replace('\n', '\r\n'))}
    panel, base = rows['121eb68'], rows['515af50']
    assert (panel.window_type, panel.activity_record, panel.visible, panel.frame) == \
        ('APPLICATION', '233513554', True, '[0,72][1080,1920]')
    assert base.window_type == 'BASE_APPLICATION' and base.package == 'com.ss.android.ugc.aweme'
    assert rows['9bb5284'].visible is False


def test_focused_panel_window_over_live_room_is_overlay():
    surface = classify_live_surface(OPEN, displays('121eb68'))
    assert surface.overlay == PANEL_WINDOW and surface.available
    assert surface.activity == 'com.ss.android.ugc.aweme.live.LivePlayActivity'
    assert 'type=APPLICATION' in surface.markers


def test_focus_on_live_base_window_is_clean_even_if_panel_listed():
    assert classify_live_surface(CLEAN, displays('515af50')) == \
        classify_live_surface(OPEN, displays('515af50'))
    surface = classify_live_surface(CLEAN, displays('515af50'))
    assert surface.overlay is None and surface.available


def test_unknown_focus_is_not_clean_and_not_overlay():
    for text in (displays(None), displays('c3454d3', title='StatusBar'), displays('deadbee')):
        surface = classify_live_surface(OPEN, text)
        assert surface.overlay is None and not surface.available
    # 焦点窗口属于另一个 Activity 记录时，BACK 去向不确定。
    other = displays('121eb68', app=LIVE_APP.replace('233513554', '111'))
    assert not classify_live_surface(OPEN, other).available
    assert not classify_live_surface('', displays('121eb68')).available


def test_non_live_focused_activity_is_reported():
    login = 'mFocusedApp=ActivityRecord{1 u0 com.ss.android.ugc.aweme/.account.business.login.DYLoginActivity t83}'
    surface = classify_live_surface(CLEAN, displays('x', app=login))
    assert 'DYLoginActivity' in surface.activity and surface.overlay is None


class FakeAdb:
    def __init__(self, states):
        self.states, self.calls = list(states), []

    def run_adb(self, *args, timeout=5.0, check=False):
        self.calls.append(args)
        assert args[:3] == ('shell', 'dumpsys', 'window'), args
        displays_text, windows_text = self.states[0]
        if args[3] == 'windows':
            self.states.pop(0) if len(self.states) > 1 else None
            return SimpleNamespace(exit_code=0, stdout=windows_text)
        return SimpleNamespace(exit_code=0, stdout=displays_text)


def navigator(states):
    nav = WorkerNavigator.__new__(WorkerNavigator)
    nav._operation_deadline = None
    nav.adb_client = FakeAdb(states)
    nav._navigation_sleep = lambda *args: True
    nav.keys = []
    nav._adb = lambda *args, **kw: nav.keys.append(args) or ''
    return nav


def test_inspect_uses_only_window_manager_dumps():
    nav = navigator([(displays('121eb68'), OPEN)])
    result = nav.inspect_live_surface()
    assert result['overlay'] == PANEL_WINDOW and result['ui_available']
    assert all('uiautomator' not in ' '.join(c) for c in nav.adb_client.calls)
    assert len(nav.adb_client.calls) == 2


def test_dismiss_sends_back_only_after_fresh_panel_focus_and_sees_it_gone():
    nav = navigator([(displays('121eb68'), OPEN), (displays('515af50'), CLEAN)])
    assert nav.dismiss_live_overlay(time.monotonic() + 10, lambda: False, require_observed_clear=True)
    assert nav.keys == [('shell', 'input', 'keyevent', '4')]


def test_panel_closed_by_itself_gets_no_back():
    nav = navigator([(displays('515af50'), CLEAN)])
    assert nav.dismiss_live_overlay(time.monotonic() + 10, lambda: False, require_observed_clear=True)
    assert nav.keys == []


def test_unreadable_focus_never_sends_back_at_runtime():
    nav = navigator([(displays(None), OPEN)])
    assert not nav.dismiss_live_overlay(time.monotonic() + 3, lambda: False, require_observed_clear=True)
    assert nav.keys == []


def test_panel_that_survives_two_backs_is_failure():
    nav = navigator([(displays('121eb68'), OPEN)])
    assert not nav.dismiss_live_overlay(time.monotonic() + 10, lambda: False, require_observed_clear=True)
    assert len(nav.keys) == 2


@pytest.mark.parametrize('observations, reason', [
    ('??L', None), ('???L', 'surface_probe_unavailable'), ('OOL', 'runtime_left_live_page'),
    ('?O?L', None),
])
def test_actual_surface_classification_controls_tail_and_recovery(monkeypatch, tmp_path, valid_media,
                                                                observations, reason):
    from src.worker.state_machine import WorkerState
    from worker_harness import build_worker, assert_paired
    login = 'mFocusedApp=ActivityRecord{1 u0 com.ss.android.ugc.aweme/.account.business.login.DYLoginActivity t83}'
    states = {'?': ('', ''), 'L': (displays('515af50'), CLEAN), 'O': (displays('x', app=login), CLEAN)}
    probe = navigator([states[name] for name in observations])
    harness = build_worker(monkeypatch, tmp_path, valid_media, total=44, segment=60)
    normal = harness.navigator_type.inspect_live_surface
    def inspect(nav, **kwargs):
        if harness.controller.state_machine.state == WorkerState.RECORDING:
            return probe.inspect_live_surface()
        return normal(nav, **kwargs)
    monkeypatch.setattr(harness.navigator_type, 'inspect_live_surface', inspect)
    assert harness.controller.run() == 0
    emitted = harness.events()
    assert [e['reason'] for e in emitted if e['event'] == 'stall_detected'] == ([reason] if reason else [])
    assert len(harness.pipelines) == (2 if reason else 1)
    terminals = assert_paired(emitted)
    assert terminals[-1]['event'] == 'segment_finished' and terminals[-1]['is_final']
    if reason is None:
        assert len(terminals) == 1

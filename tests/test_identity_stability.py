"""K15 连续核验、清理冲突与调度缺项恢复的行为验收（设备/网络为替身）。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.worker.room_locator import RoomEvidence, compare_evidence, locate_target
from src.worker.identity_verifier import IdentityStatus
from src.mvp.room_resolver import ResolvedRoom, RoomStatus
from test_navigation_budget import Clock
from test_room_locator import NavigationFixture
from test_scheduler_integration import system, observe, pump, row, start_one
from console_helpers import observe_room
from test_console_controls import command, result


GOOD = RoomEvidence('900', '123')
BAD = RoomEvidence('901', '456')


@pytest.mark.parametrize('value', ['0', '000', '123.0', '123abc', '１２３', '-123'])
def test_fragment_and_profile_reject_invalid_identifier_without_numeric_prefix(value):
    from test_room_locator import live_top, dump
    from src.worker.room_locator import current_live_fragment_evidence, fresh_profile_evidence
    assert current_live_fragment_evidence(live_top(uid=value), 'LivePlayActivity') is None
    assert fresh_profile_evidence('', dump(uid=value), '41') is None


@pytest.fixture
def clock(monkeypatch):
    import src.worker.room_locator as locator
    value = Clock()
    monkeypatch.setattr(locator, 'time', value)
    return value


@pytest.mark.parametrize('expected,evidence', [
    ((None, '123'), GOOD), (('900', None), GOOD),
    (('900', '123'), replace(GOOD, room_id=None)),
    (('900', '123'), replace(GOOD, anchor_uid=None)),
    (('900', '123'), replace(GOOD, anchor_uid='0')),
    (('900', '123'), replace(GOOD, returned_to_live=False)),
])
def test_incomplete_or_unreturned_is_not_a_match(expected, evidence):
    assert compare_evidence(evidence, *expected) == IdentityStatus.UNCONFIRMED


@pytest.mark.parametrize('samples,reads', [([BAD, GOOD, GOOD], 3),
    ([GOOD, RoomEvidence(), GOOD, GOOD], 4), ([GOOD, BAD, GOOD, GOOD], 4)])
def test_settling_and_interruption_need_two_new_matches(clock, samples, reads):
    nav = NavigationFixture(samples)
    landed = locate_target(nav, '900', target_kind='room_id', anchor_uid='123')
    assert landed.success and landed.status == IdentityStatus.VERIFIED
    assert nav.routes == ['primary'] and nav.reads == reads
    assert clock.now >= 1000 + reads - 1


@pytest.mark.parametrize('conflict', [BAD, replace(BAD, returned_to_live=False)])
def test_new_conflict_cannot_disappear_through_missing_evidence_or_override(clock, conflict):
    nav = NavigationFixture([conflict, RoomEvidence()])
    landed = locate_target(nav, '900', target_kind='room_id', anchor_uid='123', allow_unconfirmed=True)
    assert not landed.success and landed.reason == 'identity_conflict_unresolved'
    assert nav.ready_checks == 0


def test_explicit_missing_identity_stays_unconfirmed(clock):
    nav = NavigationFixture([RoomEvidence(room_id='900')])
    landed = locate_target(nav, '900', target_kind='room_id', allow_unconfirmed=True)
    assert landed.success and landed.status == IdentityStatus.UNCONFIRMED
    assert clock.now >= 1055  # 显式放行仍经过有界复核。


def test_second_observation_cannot_overrun_budget_and_still_verify(clock):
    nav = NavigationFixture([GOOD])
    def observe_slow(deadline, **kw):
        nav.reads += 1
        clock.sleep(deadline - clock.now + .1)
        return GOOD
    nav.observe_live_room = observe_slow
    landed = locate_target(nav, '900', target_kind='room_id', anchor_uid='123', max_attempts=1)
    assert not landed.success and not nav.ready_checks


def test_device_probe_also_respects_identity_stage_budget(clock):
    nav = NavigationFixture([RoomEvidence()])
    def observe_slow(deadline, **kw):
        clock.sleep(deadline-clock.now)
        return RoomEvidence()
    def device_online():
        assert clock.now <= 1052
        clock.sleep(8)
        return True
    nav.observe_live_room = observe_slow
    nav.device_online = device_online
    landed = locate_target(nav, '900', target_kind='room_id', anchor_uid='123', max_attempts=1)
    assert not landed.success and clock.now <= 1060


@pytest.mark.parametrize('cleanup_fails', [False, True])
def test_real_navigator_independent_profile_cycles_keep_conflicts_on_cleanup_failure(monkeypatch, cleanup_fails):
    import numpy as np
    import src.worker.navigator as module
    from src.worker.navigator import WorkerNavigator
    clock = Clock()
    monkeypatch.setattr(module, 'time', clock)
    monkeypatch.setattr(module, 'FAST_IDENTITY_TRIES', 1)
    nav = WorkerNavigator.__new__(WorkerNavigator)
    nav._operation_deadline = None
    nav._navigation_generation = 1
    nav._profile_entry_clicked_generation = None
    nav.adb_client = object()
    state = dict(page='live', cycle=0, taps=0)
    inputs = []
    nav.get_current_focus_activity = lambda: 'UserProfileActivity' if state['page'] == 'profile' else 'LivePlayActivity'
    frame = np.zeros((1920, 1080, 3), dtype=np.uint8)
    nav._capture_frame_array = lambda: frame
    nav._profile_sheet_avatar_point = lambda frame: (151, 643)
    nav._navigation_sleep = lambda sec, deadline, stop: clock.sleep(min(sec, deadline-clock.now)) or not stop()
    monkeypatch.setattr('src.worker.popup_bypasser.PopupBypasser',
        lambda adb: SimpleNamespace(get_ui_dump=lambda: '<hierarchy/>'))
    live = 'topResumedActivity=ActivityRecord{live u0 com.ss.android.ugc.aweme/.live.LivePlayActivity t41}'
    def adb(*args, **kw):
        if args[:2] == ('shell', 'input'):
            inputs.append(args[2:])
        if args[:3] == ('shell', 'input', 'tap'):
            state['taps'] += 1
            if args[3:] == ('151', '643'):
                state['page'] = 'profile'; state['cycle'] += 1
        elif args[:3] == ('shell', 'input', 'keyevent'):
            if cleanup_fails:
                raise OSError('注入返回失败')
            state['page'] = 'live'
        elif args[:4] == ('shell', 'dumpsys', 'activity', 'activities'):
            if state['page'] == 'live':
                return live
            return (f'* Hist #2: ActivityRecord{{profile{state["cycle"]} u0 com.ss.android.ugc.aweme/.profile.ui.UserProfileActivity t41}} '
                    'Intent { dat=aweme://user/profile?uid=456&room_id=901&anchor_id=456&previous_page=live }')
        return ''
    nav._adb = adb
    first = nav.observe_live_room(clock.now + 30, lambda: False)
    assert (first.room_id, first.anchor_uid) == ('901', '456')
    assert first.returned_to_live is not cleanup_fails
    assert nav._operation_deadline is None
    assert state['taps'] == 2
    cleanup = inputs[inputs.index(('keyevent', '4')):]
    def verify_cleanup(actions):
        assert actions and all(action == ('keyevent', '4') for action in actions)
    verify_cleanup(cleanup)
    with pytest.raises(AssertionError):
        verify_cleanup([('tap', '300', '430'), *cleanup])
    if cleanup_fails:
        assert compare_evidence(first, '900', '123') == IdentityStatus.MISMATCH
    else:
        second = nav.observe_live_room(clock.now + 30, lambda: False)
        assert second.returned_to_live and first.activity_token != second.activity_token
        assert state['cycle'] == 2 and state['taps'] == 4
        assert nav._profile_entry_clicked_generation is None


def test_live_missing_uid_changes_session_and_stops_old_worker(system):
    e = system
    target, job = start_one(e)
    e.network.rooms['A'] = ResolvedRoom('A', RoomStatus.LIVE, room_id='2065')
    observe_room(e.store, target, e.network.rooms['A'], e.clock())
    e.scheduler.tick()
    t = row(e, 'SELECT * FROM targets WHERE id=?', target)
    assert t['observed_status'] == 'live' and t['session_key'] == 'room:2065'
    assert t['anchor_uid'] is None
    assert row(e, 'SELECT stop_reason FROM jobs WHERE id=?', job['id'])['stop_reason'] == 'live_session_changed'
    assert len(e.children) == 1


@pytest.mark.parametrize('restored,changed', [('1065', False), ('2065', True)])
def test_missing_room_keeps_last_session_but_never_supplies_admission(system, restored, changed):
    e = system
    target, job = start_one(e)
    observe_room(e.store, target, ResolvedRoom('A', RoomStatus.LIVE, anchor_uid='900001'), e.clock())
    e.scheduler.tick()
    t = row(e, 'SELECT * FROM targets WHERE id=?', target)
    assert t['room_id'] is None and t['session_key'] == job['session_key']
    assert e.scheduler._identity_reason(t) == 'target_identity_incomplete'
    assert e.children[0].alive
    observe_room(e.store, target, ResolvedRoom('A', RoomStatus.LIVE, room_id=restored, anchor_uid='900001'), e.clock())
    e.scheduler.tick()
    assert (row(e, 'SELECT session_key FROM targets WHERE id=?', target)['session_key'] != job['session_key']) == changed


def test_first_incomplete_live_has_no_invented_session_or_intent(system):
    e = system
    target = e.store.add_target('A', auto_record=False, allow_unconfirmed=True)
    e.network.rooms['A'] = ResolvedRoom('A', RoomStatus.LIVE, anchor_uid='900001')
    key = command(e, 'record', target)
    pump(e, lambda: result(e, key)['status'] == 'failed')
    t = row(e, 'SELECT * FROM targets WHERE id=?', target)
    assert t['observed_status'] == 'live' and t['session_key'] is None and t['record_session_key'] is None
    assert result(e, key)['error_code'] == 'target_identity_incomplete'
    assert not e.children and not e.store.active_job(target)


@pytest.mark.parametrize('incomplete', [False, True])
def test_pending_manual_refresh_reuses_task_for_missing_or_stale_identity(system, incomplete):
    e = system
    target = e.store.add_target('A', auto_record=False)
    observe(e, {'A': RoomStatus.LIVE})
    job_id = e.store.create_pending(target, e.clock())
    if incomplete:
        observe_room(e.store, target, ResolvedRoom('A', RoomStatus.LIVE, room_id='1065'), e.clock())
    else:
        e.clock.advance(1000)
    e.scheduler._dispatch()
    assert not e.children and not e.scheduler._verifying
    key = command(e, 'record', target)
    pump(e, lambda: len(e.children) == 1)
    assert result(e, key)['status'] == 'succeeded'
    assert e.children[0].spec.job_id == job_id
    assert len(e.store.rows('SELECT * FROM jobs')) == 1
    # 手动刷新与启动复核都执行，标题走独立查询槽位。
    assert [source for source, _, _ in e.scheduler.room_queries.calls] == ['manual', 'verify', 'title']


def test_prelaunch_missing_releases_slot_and_scan_completion_resumes(system):
    e = system
    target = e.store.add_target('A')
    observe(e, {'A': RoomStatus.LIVE})
    e.network.rooms['A'] = ResolvedRoom('A', RoomStatus.LIVE, room_id='1065')
    e.scheduler.tick(); e.scheduler.tick()
    job = e.store.active_job(target)
    assert job['status'] == 'pending' and job['vmindex'] is None
    assert job['error_reason'] == 'target_identity_incomplete' and not e.children
    count = len(e.network.resolved)
    e.clock.advance(20)
    e.scheduler.tick()
    assert len(e.network.resolved) == count
    e.scheduler.auto_scan = True
    e.network.rooms['A'] = ResolvedRoom('A', RoomStatus.LIVE, room_id='1065', anchor_uid='900001')
    pump(e, lambda: len(e.children) == 1)
    assert e.children[0].spec.job_id == job['id']


@pytest.mark.parametrize('extra', [[], ['--expected-anchor-uid', '123'],
    ['--target-kind', 'room_id'], ['--target-kind', 'room_id', '--expected-anchor-uid', '0'],
    ['--allow-unconfirmed-identity', '--expected-anchor-uid', 'bad']])
def test_cli_bad_expected_identity_fails_before_creating_output(tmp_path, extra):
    from src.worker.cli import main
    output = tmp_path / 'never-created'
    with pytest.raises(SystemExit) as exc:
        main(['--vmindex', '2', '--target', '900', '--output-dir', str(output), *extra])
    assert exc.value.code == 2 and not output.exists()


def test_programmatic_configuration_has_same_early_identity_validation(tmp_path):
    from src.worker.controller import WorkerConfig, WorkerController
    output = tmp_path / 'never-created'
    with pytest.raises(ValueError, match='UID|uid'):
        WorkerController(WorkerConfig(2, '900', output, target_kind='room_id'))
    assert not output.exists()


def test_relocation_invalidates_heartbeat_and_evidence_before_callbacks(monkeypatch, tmp_path, valid_media):
    import src.worker.controller as module
    from src.worker.room_locator import LandingResult
    from worker_harness import build_worker
    h = build_worker(monkeypatch, tmp_path, valid_media)
    c = h.controller
    c.identity_status = IdentityStatus.VERIFIED
    c.emitter.set_context(room_identity_status='verified', room_evidence_source='old', room_evidence_at=123)
    def locate(nav, target, **kw):
        assert c.identity_status == IdentityStatus.UNCONFIRMED
        kw['on_step'](action='navigate')
        kw['stop_requested']()
        return LandingResult(False, 'identity_unstable')
    monkeypatch.setattr(module, 'locate_target', locate)
    c._locate_target_room()
    events = h.events()
    assert events and all(e['room_identity_status'] == 'unconfirmed' for e in events)
    assert all(e['room_evidence_at'] == 0 for e in events)


@pytest.mark.parametrize('reason,code', [('identity_unstable', 18), ('identity_conflict_unresolved', 18),
    ('login_blocked', 15), ('device_unavailable', 20)])
def test_runtime_relocation_uses_dedicated_exit_and_preserves_old_segment(monkeypatch, tmp_path, valid_media, reason, code):
    from worker_harness import build_worker, assert_paired
    from src.worker.room_locator import LandingResult
    from src.worker.state_machine import ActionType, PipelineAction
    import src.worker.controller as module
    h = build_worker(monkeypatch, tmp_path, valid_media, total=5, segment=30)
    real = module.locate_target
    calls = []
    def locate(*args, **kw):
        calls.append(1)
        if len(calls) == 1:
            return real(*args, **kw)
        assert not any(p.alive for p in h.pipelines)
        return LandingResult(False, reason)
    monkeypatch.setattr(module, 'locate_target', locate)
    posted = []
    def drift(now):
        if any(p.alive for p in h.pipelines) and not posted:
            posted.append(1)
            h.controller.action_dispatcher.post_action(PipelineAction(ActionType.IN_PLACE_HEAL,
                h.controller.current_generation_id, 'runtime_left_live_page'))
    h.clock.on_sleep = drift
    assert h.controller.run() == code
    assert len(calls) == 2 and h.launched == [1]
    assert all(e['room_identity_status'] == 'verified' for e in assert_paired(h.events()))
    assert [e['exit_code'] for e in h.events() if e['event'] in ('worker_failed', 'worker_stopped')] == [code, code]


def test_login_query_preserves_live_with_missing_identity(system):
    from src.orchestrator.room_query_process import QueryResult
    e = system
    target, job = start_one(e)
    token = 'check'
    e.scheduler._login_checks[job['id']] = (0, e.clock()+120, token)
    meta = e.store.begin_observation(target, e.clock())
    e.scheduler._login_result(meta, job['id'], token,
        QueryResult(ResolvedRoom('A', RoomStatus.LIVE, room_id='1065')))
    t = row(e, 'SELECT * FROM targets WHERE id=?', target)
    assert t['observed_status'] == 'live' and t['anchor_uid'] is None
    event = row(e, "SELECT payload FROM events WHERE job_id=? ORDER BY rowid DESC LIMIT 1", job['id'])
    import json
    assert json.loads(event['payload'])['reason'] == 'target_identity_incomplete'

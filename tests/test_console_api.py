"""接口契约、快照分页、时间统计、日志边界、CLI 与静态托管。"""
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import uuid

from fastapi.testclient import TestClient
import pytest
import requests

from console_helpers import TestRuntime, observe_room
from src.mvp.room_resolver import ResolvedRoom, RoomStatus
from src.orchestrator.server import create_app, ServerConfig
from src.orchestrator.storage import Store
from src.orchestrator.queries import Queries
from src.orchestrator import cli


@pytest.fixture
def api(tmp_path):
    store = Store(tmp_path / 'state' / 'orchestrator.sqlite3')
    runtime = TestRuntime(store)
    config = ServerConfig(tmp_path / 'state', tmp_path / 'out', [1001], static_root=tmp_path / 'web')
    with TestClient(create_app(config, runtime)) as client:
        yield client, store, runtime, config
    store.close()


def submit(client, method, path, body=None, key=None):
    return client.request(method, '/api/' + path, json=body, headers={'Idempotency-Key': key or str(uuid.uuid4())})


def apply(store, key):
    with store.transaction() as db:
        cmd = store.row('SELECT * FROM commands WHERE id=?', (key,))
        db.execute("UPDATE commands SET status='running',started_at=10 WHERE id=?", (key,))
        store.complete_command(key, store.apply_control(cmd, 10), now=10)


def test_all_empty_reads_and_errors(api):
    client, store, runtime, config = api
    for path in ('targets', 'jobs', 'segments', 'instances'):
        response = client.get('/api/' + path)
        assert response.status_code == 200, response.text
        assert response.json()['items'] == [] and response.json()['total'] == 0
        assert set(response.json()) == {'items', 'total', 'limit', 'offset', 'generated_at'}
    assert client.get('/api/overview').json()['today_finished_segments'] == 0
    for path in ('missing', 'commands/no', 'jobs/no', 'jobs/no/log'):
        assert client.get('/api/' + path).status_code == 404
    for path in ('targets?unknown=x', 'jobs?from_time=3&to_time=2', 'jobs?active=1', 'jobs?from_time=nan',
                 'targets?limit=201', 'targets?offset=-1', 'targets?limit=1&limit=2', 'instances?status=bad', 'segments?status=bad'):
        response = client.get('/api/' + path)
        assert response.status_code == 422, response.text
        assert set(response.json()) == {'code', 'message', 'details'}
    assert client.post('/api/targets', content='{bad', headers={'Content-Type': 'application/json'}).status_code == 400
    for body in ({'targets': []}, {'targets': ['A'], 'other': 1}, {'targets': ['A'], 'manual_only': 'false'}, {'targets': ['x' * 2049]}):
        assert submit(client, 'POST', 'targets', body).status_code == 422
    assert client.post('/api/targets', json={'targets': ['A']}).status_code == 422
    assert submit(client, 'POST', 'targets', {'targets': ['A']}, 'bad').status_code == 422
    runtime.state = 'closing'
    assert submit(client, 'POST', 'targets', {'targets': ['A']}).status_code == 503


def test_command_receipt_idempotency_and_removed_target(api):
    client, store, runtime, config = api
    rid = str(uuid.uuid4())
    response = submit(client, 'POST', 'targets', {'targets': ['  A  ']}, rid)
    assert response.status_code == 202
    key = response.json()['command_id']
    apply(store, key)
    again = submit(client, 'POST', 'targets', {'targets': ['A']}, rid)
    assert again.status_code == 200 and again.json()['command_id'] == key
    assert submit(client, 'POST', 'targets', {'targets': ['B']}, rid).status_code == 409
    command = client.get('/api/commands/' + key).json()
    assert command['status'] == 'succeeded' and isinstance(command['result'], dict)
    target = command['result']['items'][0]['target_id']
    rid = str(uuid.uuid4())
    key = submit(client, 'DELETE', 'targets/' + target, key=rid).json()['command_id']
    apply(store, key)
    assert submit(client, 'DELETE', 'targets/' + target, key=rid).status_code == 200
    assert submit(client, 'DELETE', 'targets/' + target).status_code == 404


def seed(store):
    target = store.add_target('A', now=1)
    observe_room(store, target, ResolvedRoom('A', RoomStatus.LIVE, room_id='session', nickname='主播甲'), 2)
    job = store.create_pending(target, 3)
    return target, job


def test_recovery_causes_survive_cancel_and_recent_event_limit(api):
    client, store, runtime, config = api
    target, job = seed(store)
    store.record_event(job, dict(event='stall_detected', reason='login_prompt_detected'), 10)
    store.record_event(job, dict(event='room_navigation', action='retry_picture', reason='render_timeout'), 11)
    for n in range(55):
        store.record_event(job, dict(event='room_navigation', event_id=f'wait-{n}', action='wait_picture', reason='return_to_live'), 12+n)
    store.record_event(job, dict(event='room_navigation', action='navigation_exhausted', reason='cancelled'), 80)
    store.record_event(job, dict(event='worker_stopped', reason='cancelled', exit_code=30), 81)
    with store.transaction() as db:
        db.execute("UPDATE jobs SET status='failed',ended_at=81,exit_code=30,stop_reason='anchor_offline',error_reason='cancelled' WHERE id=?", (job,))
    expected = dict(trigger={'code':'login_prompt_detected','detail':None},
                    last_failure={'code':'render_timeout','detail':None}, outcome='cancelled')
    detail = client.get('/api/jobs/' + job).json()
    assert detail['recovery_context'] == expected
    assert not any(e['event'] == 'stall_detected' for e in detail['recent_events'])
    assert detail['stop_reason']['code'] == 'anchor_offline'
    assert detail['error_reason']['code'] == 'cancelled' and detail['status'] == 'failed'
    assert client.get('/api/jobs?active=false').json()['items'][0]['recovery_context'] == expected
    # 后续新一次中断必须清空上一次恢复原因，成功恢复不因最后正常退出而变为取消。
    store.record_event(job, dict(event='stall_detected', reason='video_frozen'), 90)
    store.record_event(job, dict(event='stall_healed'), 91)
    store.record_event(job, dict(event='worker_stopped', reason='stop_requested', exit_code=0), 92)
    assert client.get('/api/jobs/' + job).json()['recovery_context'] == dict(
        trigger={'code':'video_frozen','detail':None}, last_failure=None, outcome='recovered')


def test_filters_history_projection_and_snapshot(api):
    client, store, runtime, config = api
    target, job = seed(store)
    second = store.add_target('B', auto_record=False, now=1)
    with store.transaction() as db:
        db.execute("UPDATE targets SET note='needs_fix: 地址错误' WHERE id=?", (second,))
        db.execute("UPDATE jobs SET status='finished',ended_at=10 WHERE id=?", (job,))
    response = client.get('/api/targets?q=主播&auto_record=true&trusted_status=live&has_notice=false&limit=1').json()
    assert response['total'] == 1 and response['items'][0]['id'] == target
    assert response['items'][0]['recording_intent'] is True
    assert client.get('/api/targets?has_notice=true').json()['items'][0]['needs_fix_reason'] == {'code': 'target_invalid', 'detail': '地址错误'}
    assert client.get('/api/jobs').json()['total'] == 0
    assert client.get('/api/jobs?active=false&from_time=3&to_time=4').json()['items'][0]['id'] == job
    assert client.get('/api/jobs?active=false&to_time=3').json()['total'] == 0
    assert client.get('/api/jobs?active=false&target_id=none').json()['total'] == 0
    for n in range(55):
        store.record_event(job, dict(event='navigation_step', event_id=str(n), timestamp=n, action='a', phase='p', attempt=1), n)
    store.record_event(job, dict(event='heartbeat', event_id='heart', timestamp=20, fps=23, elapsed_sec=5, room_identity_status='confirmed'), 21)
    store.record_event(job, dict(event='heartbeat', event_id='old-heart', timestamp=19, fps=1, elapsed_sec=4), 22)
    store.record_event(job, dict(event='heartbeat', event_id='invalid-heart', timestamp=25, fps='bad'), 25)
    detail = client.get('/api/jobs/' + job).json()
    assert len(detail['recent_events']) == 50 and detail['recent_events'][0]['event_id'] == '54'
    assert detail['heartbeat_at'] == 20 and detail['elapsed_sec'] == 5 and detail['fps'] == 23
    assert 'heartbeat_json' not in detail and 'pid' not in detail
    with store.read_snapshot() as snapshot:
        assert snapshot.execute('SELECT count(*) FROM targets WHERE enabled=1').fetchone()[0] == 2
        with store.transaction() as db:
            db.execute('UPDATE targets SET enabled=0 WHERE id=?', (target,))
        assert snapshot.execute('SELECT count(*) FROM targets WHERE enabled=1').fetchone()[0] == 2
    assert client.get('/api/jobs/' + job).json()['nickname'] == '主播甲'
    assert client.get('/api/targets').json()['total'] == 1


def test_segment_times_quality_alerts_and_beijing_day(api):
    client, store, runtime, config = api
    target, job = seed(store)
    midnight = datetime(2026, 11, 11, tzinfo=timezone(timedelta(hours=8))).timestamp()
    for index, ended, kind in ((1, midnight - .1, 'segment_finished'), (2, midnight, 'segment_finished'), (3, midnight + 1, 'segment_aborted')):
        store.record_event(job, dict(event='segment_started', event_id=f's{index}', segment_index=index, timestamp=midnight - 10, file_path='raw.ts'), midnight + 30)
        store.record_event(job, dict(event=kind, event_id=f'e{index}', segment_index=index, timestamp=ended, file_path=f'{index}.mp4', is_final=False), midnight + 40)
    store.record_event(job, dict(event='segment_quality_warning', event_id='quality', segment_index=3, timestamp=midnight, flag='future_flag', suspected_start_time=midnight-1), midnight+50)
    store.record_event(job, dict(event='segment_started', event_id='active', segment_index=4, timestamp=midnight+5, file_path='active.ts'), midnight+50)
    query = Queries(store, clock=lambda: midnight + 60)
    overview = query.overview(limit=1)
    assert overview['today_finished_segments'] == 1
    assert overview['alerts']['total'] == 2 and len(overview['alerts']['items']) == 1
    assert {a['type'] for a in query.overview()['alerts']['items']} == {'segment_aborted', 'segment_quality'}
    segments = client.get('/api/segments?has_quality_flags=true&status=aborted&job_id=' + job).json()
    assert segments['total'] == 1
    s = segments['items'][0]
    assert s['ended_at'] == midnight + 1 and s['created_at'] == midnight - 10
    assert s['quality_flags'] == [{'flag': 'future_flag', 'suspected_start_time': midnight-1}]
    assert s['duration_sec'] is None and s['file_size_mb'] is None
    started = client.get('/api/segments?status=started').json()['items'][0]
    assert started['file_path'] == 'active.ts'
    assert client.get(f'/api/segments?from_time={midnight}&to_time={midnight+10}').json()['total'] == 1


def test_log_tail_byte_limits_and_path_escape(api, tmp_path):
    client, store, runtime, config = api
    target, job = seed(store)
    url = '/api/jobs/' + job + '/log'
    assert client.get(url).json()['available'] is False
    root = store.path.parent / 'events'
    root.mkdir()
    path = root / (job + '.log')
    path.write_bytes(b'')
    assert client.get(url).json()['available'] is True
    path.write_bytes('一\n二\n尾'.encode('utf-8') + b'\xe4')
    data = client.get(url + '?lines=2').json()
    assert data['text'] == '二\n尾�' and data['truncated'] and data['returned_lines'] == 2
    path.write_bytes(b'x' * (2 * 1024 * 1024))
    data = client.get(url).json()
    assert len(data['text']) == 1024 * 1024 and data['truncated']
    assert client.get(url + '?lines=2001').status_code == 422
    with store.transaction() as db:
        db.execute("INSERT INTO jobs(id,target_id,session_key,status,created_at) VALUES('../escape',?,'s','cancelled',1)", (target,))
    with pytest.raises(Exception, match='越界'):
        Queries(store).log('../escape')


def test_static_missing_spa_unknown_api_and_traversal(api, tmp_path):
    client, store, runtime, config = api
    assert client.get('/').status_code == 503
    config.static_root.mkdir()
    (config.static_root / 'index.html').write_text('<html>控制台</html>', encoding='utf-8')
    (config.static_root / 'app.js').write_text('console.log(1)', encoding='utf-8')
    assert client.get('/targets').text == '<html>控制台</html>'
    assert client.get('/app.js').status_code == 200
    for path in ('/api/no', '/missing.js', '/%2e%2e/secret.txt'):
        response = client.get(path)
        assert response.status_code == 404 and response.headers['content-type'].startswith('application/json')


def test_cli_only_http_and_retains_timeout_request_id(monkeypatch, capsys):
    calls = []
    rid = str(uuid.uuid4())
    def failed(*args, **kw):
        calls.append((args, kw))
        raise requests.Timeout()
    monkeypatch.setattr(cli.requests, 'request', failed)
    assert cli.main(['stop', '--target-id', 'target', '--session-key', 'room:A', '--request-id', rid]) == 1
    assert len(calls) == 1 and calls[0][1]['headers']['Idempotency-Key'] == rid
    assert json.loads(capsys.readouterr().out)['request_id'] == rid
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(['--data-dir', 'old', 'status'])


def test_first_start_is_not_retrying_and_fix_alert_time_does_not_move(api):
    client, store, runtime, config = api
    target, job = seed(store)
    with store.transaction() as db:
        db.execute("UPDATE jobs SET status='starting' WHERE id=?", (job,))
        db.execute('UPDATE targets SET attempts=1 WHERE id=?', (target,))
    assert client.get('/api/targets').json()['items'][0]['is_retrying'] is False
    store.finish_job(job, 9, 50, True, 'target_invalid')
    observe_room(store, target, ResolvedRoom('A', RoomStatus.LIVE, room_id='new'), 100)
    alert = Queries(store).overview()['alerts']['items'][0]
    assert alert['type'] == 'needs_fix' and alert['timestamp'] == 50
    observe_room(store, target, ResolvedRoom('A', RoomStatus.LIVE, room_id='new'), 200)
    assert Queries(store).overview()['alerts']['items'][0]['timestamp'] == 50


def test_alerts_are_self_contained_beyond_first_target_page(api):
    client, store, runtime, config = api
    target, job = seed(store)
    with store.transaction() as db:
        db.execute("UPDATE jobs SET status='failed',ended_at=10,error_reason=? WHERE id=?",
                   ('worker_exception: 错误: 设备失联', job))
        db.execute('UPDATE targets SET attempts=3,retry_at=80 WHERE id=?', (target,))
    for index in range(201):
        store.add_target(f'new-{index}', now=20 + index)
    assert target not in {t['id'] for t in client.get('/api/targets?limit=200').json()['items']}
    alert = client.get('/api/overview').json()['alerts']['items'][0]
    assert alert['target_id'] == target
    assert (alert['nickname'], alert['target_raw'], alert['attempts'], alert['retry_at']) == ('主播甲', 'A', 3, 80)
    assert alert['reason'] == {'code': 'worker_exception', 'detail': '错误: 设备失联'}
    assert 'message' not in alert
    assert client.get('/api/jobs?active=false').json()['items'][0]['error_reason'] == alert['reason']


def test_recording_time_uses_first_segment_and_event_reason_is_structured(api):
    client, store, runtime, config = api
    target, job = seed(store)
    with store.transaction() as db:
        db.execute("UPDATE jobs SET status='running',started_at=5 WHERE id=?", (job,))
    assert client.get('/api/jobs/' + job).json()['recording_started_at'] is None
    for index, timestamp in ((2, 30), (1, 20)):
        store.record_event(job, dict(event='segment_started', event_id=f's-{index}', segment_index=index,
                                    timestamp=timestamp, file_path=f'{index}.ts'), 40)
    store.record_event(job, dict(event='navigation_step', event_id='why', timestamp=42,
                                reason='capture_restart_failed: 设备: timeout'), 42)
    detail = client.get('/api/jobs/' + job).json()
    assert (detail['started_at'], detail['recording_started_at']) == (5, 20)
    assert detail['recent_events'][0]['reason'] == {'code': 'capture_restart_failed', 'detail': '设备: timeout'}
    assert client.get('/api/jobs').json()['items'][0]['recording_started_at'] == 20


@pytest.mark.parametrize('raw,expected', [
    (None, None), ('', None), ('future_reason', {'code': 'future_reason', 'detail': None}),
    ('event_collection_incomplete: a: b', {'code': 'event_collection_incomplete', 'detail': 'a: b'}),
    ('旧版异常说明', {'code': 'unspecified', 'detail': '旧版异常说明'}),
])
def test_reason_projection_preserves_details(raw, expected):
    from src.orchestrator.reasons import reason_object
    assert reason_object(raw) == expected


def test_optional_instances_cli_and_overview_counts_only_enabled(api, tmp_path):
    from src.orchestrator.server import ServerConfig
    parsed = cli.build_parser().parse_args(['run', '--output-dir', str(tmp_path / 'out')])
    assert parsed.instances is None
    assert ServerConfig(tmp_path / 'data', tmp_path / 'out').instances is None

    client, store, runtime, config = api
    with store.transaction() as db:
        db.execute("INSERT INTO instances(vmindex,name,status,enabled,updated_at) VALUES(2,'VM2','idle',0,1)")
        db.execute("INSERT INTO instances(vmindex,name,status,enabled,updated_at) VALUES(3,'VM3','idle',1,1)")
        db.execute("INSERT INTO instances(vmindex,name,status,enabled,updated_at) VALUES(4,'VM4','reconcile',0,1)")
    counts = client.get('/api/overview').json()['instance_counts']
    assert counts['idle'] == 1 and counts['reconcile'] == 1


def test_instance_enable_filter_and_patch_contract(api):
    client, store, runtime, config = api
    with store.transaction() as db:
        db.execute("INSERT INTO instances(vmindex,name,status,enabled,updated_at) VALUES(2,'VM2','idle',0,1)")
        db.execute("INSERT INTO instances(vmindex,name,status,enabled,reason,updated_at) VALUES(3,'VM3','offline',1,'not_discovered',1)")

    assert client.get('/api/instances?enabled=bad').status_code == 422
    assert [i['vmindex'] for i in client.get('/api/instances?enabled=false').json()['items']] == [2]
    assert [i['vmindex'] for i in client.get('/api/instances?enabled=true').json()['items']] == [3]

    assert submit(client, 'PATCH', 'instances/999', {'enabled': True}).status_code == 404
    assert submit(client, 'PATCH', 'instances/-1', {'enabled': True}).status_code == 422
    assert submit(client, 'PATCH', 'instances/2', {'enabled': 'true'}).status_code == 422

    rid = str(uuid.uuid4())
    resp = submit(client, 'PATCH', 'instances/2', {'enabled': True}, rid)
    assert resp.status_code == 202
    key = resp.json()['command_id']
    apply(store, key)
    assert submit(client, 'PATCH', 'instances/2', {'enabled': True}, rid).status_code == 200
    assert submit(client, 'PATCH', 'instances/2', {'enabled': False}, rid).status_code == 409
    assert client.get('/api/instances?enabled=true').json()['total'] == 2

    # 占用、开机、待对账时禁止停用
    target, job = seed(store)
    with store.transaction() as db:
        db.execute("UPDATE instances SET status='busy',job_id=? WHERE vmindex=2", (job,))
    r_busy = submit(client, 'PATCH', 'instances/2', {'enabled': False})
    assert r_busy.status_code == 409 and r_busy.json()['code'] == 'instance_busy'

    assert client.get('/api/instances?status=resetting').status_code == 422
    assert 'resetting' not in client.get('/api/overview').json()['instance_counts']

    with store.transaction() as db:
        db.execute("UPDATE instances SET status='booting',job_id=NULL,reason='booting',boot_attempts=2,boot_retry_at=999 WHERE vmindex=2")
    r_boot = submit(client, 'PATCH', 'instances/2', {'enabled': False})
    assert r_boot.status_code == 409 and r_boot.json()['code'] == 'instance_busy'
    assert [i['vmindex'] for i in client.get('/api/instances?status=booting').json()['items']] == [2]
    assert client.get('/api/overview').json()['instance_counts']['booting'] == 1

    # 离线且带冷却记录的实例在启用时应清零开机冷却
    with store.transaction() as db:
        db.execute("UPDATE instances SET status='offline',enabled=0,reason='launch_failed',boot_attempts=3,boot_retry_at=999 WHERE vmindex=2")
    re_enable = submit(client, 'PATCH', 'instances/2', {'enabled': True}).json()['command_id']
    apply(store, re_enable)
    row2 = store.row('SELECT * FROM instances WHERE vmindex=2')
    assert (row2['enabled'], row2['boot_attempts'], row2['boot_retry_at'], row2['reason']) == (1, 0, 0, 'android_not_started')

    with store.transaction() as db:
        db.execute("UPDATE jobs SET status='needs_reconcile',vmindex=2 WHERE id=?", (job,))
        db.execute("UPDATE instances SET status='reconcile',job_id=? WHERE vmindex=2", (job,))
    r_rec = submit(client, 'PATCH', 'instances/2', {'enabled': False})
    assert r_rec.status_code == 409 and r_rec.json()['code'] == 'reconcile_required'

    # 停用已失联旧实例会直接清理该未发现旧记录
    clean_key = submit(client, 'PATCH', 'instances/3', {'enabled': False}).json()['command_id']
    apply(store, clean_key)
    assert [i['vmindex'] for i in client.get('/api/instances').json()['items']] == [2]


def test_open_segment_folder_contract(api, monkeypatch):
    client, store, runtime, config = api
    target, job = seed(store)

    launched = []
    from src.orchestrator import desktop
    monkeypatch.setattr(desktop.platform, 'system', lambda: 'Windows')
    monkeypatch.setattr(desktop, 'run_file_manager', lambda cmd: launched.append(cmd))

    # 1. 切片不存在
    assert client.post(f'/api/segments/{job}/0/open-folder').status_code == 404

    # 2. 切片存在但未落盘 (file_path 为 None)
    with store.transaction() as db:
        db.execute("INSERT INTO segments(job_id, segment_index, created_at, updated_at) VALUES(?, 0, 10, 10)", (job,))
    r_not_ready = client.post(f'/api/segments/{job}/0/open-folder')
    assert r_not_ready.status_code == 404
    assert r_not_ready.json()['code'] == 'segment_file_not_ready'

    # 3. 正常高亮定位 (文件存在且在 output_dir 内)
    job_folder = config.output_dir / job
    job_folder.mkdir(parents=True, exist_ok=True)
    seg_file = job_folder / 'seg_000.mp4'
    seg_file.write_bytes(b'video-data')
    with store.transaction() as db:
        db.execute("UPDATE segments SET file_path=? WHERE job_id=? AND segment_index=0", (str(seg_file), job))

    r_ok = client.post(f'/api/segments/{job}/0/open-folder')
    assert r_ok.status_code == 200
    data = r_ok.json()
    assert data['status'] == 'succeeded' and data['revealed'] is True
    assert data['file_path'] == str(seg_file.resolve())
    assert data['folder_path'] == str(job_folder.resolve())
    assert len(launched) == 1
    assert launched[0] == ['explorer.exe', f'/select,{seg_file.resolve()}']

    # 4. 文件不存在但父目录存在 (优雅降级)
    seg_file.unlink()
    launched.clear()
    r_degrade = client.post(f'/api/segments/{job}/0/open-folder')
    assert r_degrade.status_code == 200
    assert r_degrade.json()['revealed'] is False
    assert len(launched) == 1
    assert launched[0] == ['explorer.exe', str(job_folder.resolve())]

    # 5. 父目录也不存在
    job_folder.rmdir()
    r_no_dir = client.post(f'/api/segments/{job}/0/open-folder')
    assert r_no_dir.status_code == 404
    assert r_no_dir.json()['code'] == 'folder_not_found'

    # 6. 路径越界 (不在 output_dir 白名单)
    outside_dir = config.data_dir / 'outside'
    outside_dir.mkdir(parents=True, exist_ok=True)
    outside_file = outside_dir / 'escape.mp4'
    outside_file.write_bytes(b'bad')
    with store.transaction() as db:
        db.execute("UPDATE segments SET file_path=? WHERE job_id=? AND segment_index=0", (str(outside_file), job))
    r_forbidden = client.post(f'/api/segments/{job}/0/open-folder')
    assert r_forbidden.status_code == 403
    assert r_forbidden.json()['code'] == 'path_forbidden'


@pytest.mark.parametrize('file_exists', [True, False])
def test_open_segment_folder_launch_failure(api, monkeypatch, caplog, file_exists):
    client, store, runtime, config = api
    _, job = seed(store)
    folder = config.output_dir / job
    folder.mkdir(parents=True)
    segment = folder / '切片 001.mp4'
    if file_exists:
        segment.write_bytes(b'video-data')
    with store.transaction() as db:
        db.execute('INSERT INTO segments(job_id, segment_index, file_path, created_at, updated_at) VALUES(?, 0, ?, 10, 10)',
                   (job, str(segment)))

    def fail_launch(*args, **kwargs):
        raise OSError('模拟资源管理器启动失败')

    # 在实际子进程边界注入失败，防止桌面辅助层再次吞掉异常。
    from src.orchestrator import desktop
    monkeypatch.setattr(desktop.subprocess, 'Popen', fail_launch)
    response = client.post(f'/api/segments/{job}/0/open-folder')
    assert response.status_code == 500
    assert response.json()['code'] == 'desktop_open_failed'
    assert 'status' not in response.json() and 'revealed' not in response.json()
    assert '模拟资源管理器启动失败' in caplog.text

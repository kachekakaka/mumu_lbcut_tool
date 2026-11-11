"""开播状态只取签名接口的房间结构；游客 ttwid 失效时换新后重试一次。"""
import json
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from src.mvp import room_resolver
from src.mvp.room_resolver import RoomResolver, RoomStatus


def api(payload):
    body = json.dumps(payload).encode('utf-8') if payload is not None else b''
    resp = Mock(status_code=200, content=body)
    resp.json = Mock(side_effect=(lambda: json.loads(body)) if body else ValueError('empty'))
    return resp


def room_payload(status, owner=True, user=None):
    room = {'status': status, 'id_str': '7123456789', 'title': '测试'}
    if owner:
        room['owner'] = {'id_str': '998877', 'nickname': '主播小A'}
    data = {'data': [room]}
    if user:
        data['user'] = user
    return {'status_code': 0, 'data': data}


@pytest.fixture
def ttwids(monkeypatch):
    issued = iter(['guest-1', 'guest-2', 'guest-3'])
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        resp = Mock(status_code=200)
        resp.cookies = {'ttwid': next(issued)}
        return resp
    monkeypatch.setattr(room_resolver.requests, 'post', post)
    return calls


def resolver_with(*responses):
    resolver = RoomResolver()
    resolver.session.get = Mock(side_effect=list(responses))
    return resolver


def test_extract_web_rid_from_url():
    resolver = RoomResolver()
    assert resolver.extract_web_rid('https://live.douyin.com/123456') == '123456'
    assert resolver.extract_web_rid('123456') == '123456'
    assert resolver.extract_web_rid('来看直播 https://live.douyin.com/rourou.004 点击') == 'rourou.004'


def test_live_room_and_anchor_uid_are_distinct(ttwids):
    resolver = resolver_with(api(room_payload(2)))
    result = resolver.resolve('https://live.douyin.com/123456')
    assert (result.status, result.room_id, result.anchor_uid, result.web_rid) == (
        RoomStatus.LIVE, '7123456789', '998877', '123456')
    assert result.nickname == '主播小A'


def test_request_is_signed_and_uses_guest_ttwid_only(ttwids):
    resolver = resolver_with(api(room_payload(2)))
    resolver.resolve('123456')
    url = resolver.session.get.call_args.args[0]
    query = parse_qs(urlsplit(url).query)
    assert urlsplit(url).path == '/webcast/room/web/enter/'
    assert query['web_rid'] == ['123456'] and query['aid'] == ['6383'] and query['a_bogus'][0]
    assert resolver.session.get.call_args.kwargs['headers'] == {'Cookie': 'ttwid=guest-1'}


def test_owner_falls_back_to_outer_user(ttwids):
    payload = room_payload(4, owner=False, user={'id_str': '998877', 'nickname': '主播小A'})
    result = resolver_with(api(payload)).resolve('123456')
    assert (result.status, result.anchor_uid) == (RoomStatus.ENDED, '998877')


def test_finished_business_code_is_ended(ttwids):
    result = resolver_with(api({'status_code': 30003, 'data': {'prompts': '直播已结束'}})).resolve('123456')
    assert result.status == RoomStatus.ENDED and result.room_id is None


@pytest.mark.parametrize('payload', [
    room_payload(3),
    {'status_code': 10011, 'status_msg': 'invalid', 'data': {}},
    {'status_code': 0, 'data': {'data': []}},
])
def test_paused_or_abnormal_response_is_unknown(ttwids, payload):
    assert resolver_with(api(payload)).resolve('123456').status == RoomStatus.UNKNOWN


def test_http_error_is_unknown(ttwids):
    resp = api(room_payload(2))
    resp.raise_for_status.side_effect = requests.HTTPError('500')
    assert resolver_with(resp).resolve('123456').status == RoomStatus.UNKNOWN


def test_empty_body_refreshes_ttwid_once(ttwids):
    resolver = resolver_with(api(None), api(room_payload(2)))
    assert resolver.resolve('123456').status == RoomStatus.LIVE
    cookies = [c.kwargs['headers']['Cookie'] for c in resolver.session.get.call_args_list]
    assert cookies == ['ttwid=guest-1', 'ttwid=guest-2'] and len(ttwids) == 2
    # 换到的新 ttwid 继续复用，不再每次注册。
    resolver.session.get = Mock(side_effect=[api(room_payload(2))])
    resolver.resolve('123456')
    assert resolver.session.get.call_args.kwargs['headers'] == {'Cookie': 'ttwid=guest-2'}
    assert len(ttwids) == 2


def test_repeated_empty_body_logs_and_is_unknown(ttwids, caplog):
    resolver = resolver_with(api(None), api(None))
    with caplog.at_level('WARNING', logger='src.mvp.room_resolver'):
        assert resolver.resolve('123456').status == RoomStatus.UNKNOWN
    assert 'HTTP 200' in caplog.text and '0 字节' in caplog.text


def test_ttwid_registration_failure_is_unknown(monkeypatch):
    resp = Mock(status_code=200, cookies={})
    monkeypatch.setattr(room_resolver.requests, 'post', Mock(return_value=resp))
    resolver = RoomResolver()
    resolver.session.get = Mock()
    assert resolver.resolve('123456').status == RoomStatus.UNKNOWN
    resolver.session.get.assert_not_called()

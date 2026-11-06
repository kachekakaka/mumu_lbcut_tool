"""查询结果必须来自房间结构，不能使用无关 HTML 的 status 字段。"""
import json
from unittest.mock import Mock
import requests
from src.mvp.room_resolver import RoomResolver, RoomStatus


def page(room):
    payload = {'roomInfo': {'room': room}}
    return '<html><script>self.__pace_f.push(' + json.dumps([1, '0:' + json.dumps(payload)]) + ')</script></html>'


def test_extract_web_rid_from_url():
    resolver = RoomResolver()
    assert resolver.extract_web_rid('https://live.douyin.com/123456') == '123456'
    assert resolver.extract_web_rid('123456') == '123456'
    assert resolver.extract_web_rid('来看直播 https://live.douyin.com/rourou.004 点击') == 'rourou.004'


def test_live_room_and_anchor_uid_are_distinct():
    resolver = RoomResolver()
    response = Mock()
    response.text = page({'status': 2, 'id_str': '7123456789', 'title': '测试',
                          'owner': {'id_str': '998877', 'nickname': '主播小A'}})
    resolver.session.get = Mock(return_value=response)
    result = resolver.resolve('123456')
    assert result.status == RoomStatus.LIVE
    assert result.room_id == '7123456789'
    assert result.anchor_uid == '998877'
    assert result.web_rid == '123456'


def test_unrelated_status_in_html_cannot_end_recording():
    resolver = RoomResolver()
    response = Mock()
    response.text = '<html><script>{"status": 0, "id_str": "1234"}</script></html>'
    resolver.session.get = Mock(return_value=response)
    assert resolver.resolve('123456').status == RoomStatus.UNKNOWN


def test_http_error_is_unknown_even_if_body_looks_live():
    resolver = RoomResolver()
    response = Mock()
    response.text = page({'status': 2, 'id_str': '7123456789'})
    response.raise_for_status.side_effect = requests.HTTPError('500')
    resolver.session.get = Mock(return_value=response)
    assert resolver.resolve('123456').status == RoomStatus.UNKNOWN

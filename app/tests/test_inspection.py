"""Read-only inspection: verdicts, levels, and "never sends anything"."""

import datetime as _dt

from replica.application import inspection as I
from replica.domain import safety

NOW = int(_dt.datetime(2026, 11, 4, 12, 0, tzinfo=safety.BEIJING)
          .timestamp() * 1000)


def healthy_session():
    return {'token': 'abc', 'keepalive': {'last_ok_at': NOW - 3_600_000}}


def test_shape_and_order():
    res = I.run(session=healthy_session(), now_ms=NOW)
    assert set(res) == {'ok', 'items', 'at'}
    assert res['at'] == safety.fmt_beijing(NOW)
    assert [i['id'] for i in res['items']] == [
        'token', 'freeze', 'daily', 'record', 'clock', 'receipt', 'audit']
    for item in res['items']:
        assert set(('id', 'label', 'level', 'text', 'ok')) <= set(item)
        assert item['level'] in I.LEVELS
        assert item['ok'] is (item['level'] != 'error')


def test_no_session_is_an_error():
    res = I.run(session=None, now_ms=NOW)
    assert res['ok'] is False
    assert res['items'][0]['level'] == 'error'


def test_disabled_account_and_expired_token():
    disabled = I.token_item({'token': 'x', 'disabled': True})
    assert disabled['level'] == 'error' and disabled['state'] == 'disabled'
    expired = I.token_item({'token': 'x', 'keepalive': {'token_expired': True,
                                                        'last_ok_at': NOW}})
    assert expired['level'] == 'error' and expired['state'] == 'expired'


def test_token_never_echoes_the_value():
    item = I.token_item({'token': 'super-secret-token-123'})
    assert 'super-secret' not in str(item)


def test_unverified_token_is_a_warning_not_an_error():
    item = I.token_item({'token': 'x', 'keepalive': {}})
    assert item['level'] == 'warn' and item['ok'] is True


def test_freeze_remaining_seconds():
    assert I.freeze_item(0, now_ms=NOW)['level'] == 'ok'
    assert I.freeze_item(120, now_ms=NOW)['level'] == 'warn'
    until = I.freeze_item(0, freeze_until_ms=NOW + 180_000, now_ms=NOW)
    assert until['remaining_s'] == 180
    assert I.freeze_item(0, freeze_until_ms=NOW - 1000, now_ms=NOW)['ok']


def test_daily_item_states():
    unknown = I.daily_item(None)
    assert unknown['level'] == 'warn' and unknown['runs'] is None
    ok = I.daily_item({'runs': 1}, max_per_day=2)
    assert ok['level'] == 'ok'
    full = I.daily_item({'runs': 2}, max_per_day=2)
    assert full['level'] == 'warn' and '上限' in full['text']


def test_record_item():
    assert I.record_item([], '')['level'] == 'info'
    assert I.record_item(None, '超时')['text'].startswith('记录读取失败')
    good = I.record_item([{'start_ms': NOW - 3600_000, 'distance_km': 2.5,
                           'state': 'ok'}])
    assert good['level'] == 'ok' and '2.50 km' in good['text']
    bad = I.record_item([{'start_ms': NOW, 'distance_km': 2.5,
                          'state': 'issue',
                          'reasons': [{'reason': '运动数据有问题',
                                       'complete': False}]}])
    assert bad['level'] == 'error' and '运动数据有问题' in bad['text']


def test_clock_item():
    assert I.clock_item(None)['level'] == 'info'
    small = I.clock_item({'offset_ms': 120, 'applied': True,
                          'source': 'gmttime'})
    assert small['level'] == 'ok'
    big = I.clock_item({'offset_ms': 5 * 60_000, 'applied': True,
                        'source': 'gmttime'})
    assert big['level'] == 'warn'
    junk = I.clock_item({'offset_ms': 'n/a', 'applied': False,
                         'source': 'local'})
    assert junk['level'] == 'ok'


def test_receipt_item_flags_unresolved():
    pending = I.last_receipt_item([
        {'state': 'accepted', 'start': NOW - 10 * 86_400_000},
        {'state': 'unknown', 'start': NOW - 86_400_000}])
    assert pending['level'] == 'warn' and '只读核对' in pending['text']
    assert I.last_receipt_item([])['state'] == 'none'


def test_audit_item():
    assert I.audit_item(None)['level'] == 'info'
    assert I.audit_item({'summary': {'failed': 2}, 'at': 'x'})['level'] == 'warn'
    assert I.audit_item({'summary': {'failed': 0, 'warned': 1},
                         'at': 'x'})['level'] == 'ok'


def test_overall_ok_needs_no_errors():
    res = I.run(session=healthy_session(), daily={'runs': 0}, records=[],
                clock_state={'offset_ms': 0, 'applied': True,
                             'source': 'gmttime'},
                receipts=[], now_ms=NOW)
    assert res['ok'] is True


def test_run_is_pure(monkeypatch):
    """No clock read and no I/O beyond what the caller hands in."""
    from replica.domain import clock

    def boom(*a, **k):
        raise AssertionError('inspection must not read the clock')
    monkeypatch.setattr(clock, 'now_ms', boom)
    res = I.run(session=None, now_ms=NOW)
    assert res['at'] == safety.fmt_beijing(NOW)


def test_self_test_passes():
    assert all(ok for _, ok, _ in I.self_test())

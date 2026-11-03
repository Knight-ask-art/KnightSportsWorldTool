"""Server-aligned clock: no system settings, no "please calibrate" advice."""

import json
from datetime import UTC, datetime

from replica.application import readonly as RO
from replica.application import riskcheck as RK
from replica.domain import clock as C
from replica.transport import amap
from replica.transport import realclient as RC

# --------------------------------------------------------------------------
# clock core
# --------------------------------------------------------------------------

def test_apply_server_time_uses_the_rtt_midpoint():
    sent, received = 1_800_000_000_000, 1_800_000_000_400
    server = 1_800_000_060_000
    out = C.apply_server_time(server, sent, received)
    assert out['applied'] is True
    assert out['source'] == 'gmttime'
    assert out['offset_ms'] == server - (sent + received) // 2
    assert out['rtt_ms'] == 400
    # anchored at server + rtt/2, so now is within a couple of seconds
    assert abs(C.now_ms() - (server + 200)) < 3_000


def test_implausible_server_time_is_rejected():
    before = C.state()
    out = C.apply_server_time(1, C.local_now_ms(), C.local_now_ms())
    assert out['applied'] is False
    assert '合理范围' in out['reason']
    assert C.source() == before['source']


def test_needs_sync_and_reset():
    C.reset()
    assert C.needs_sync() is True
    now = C.local_now_ms()
    C.apply_server_time(now, now, now)
    assert C.needs_sync(max_age_s=600) is False
    C.reset()
    assert C.source() == 'local'


def test_offset_survives_a_wall_clock_change(monkeypatch):
    sent = C.local_now_ms()
    C.apply_server_time(sent + 120_000, sent, sent)
    first = C.now_ms()
    # Simulate Windows time sync moving the wall clock one hour back.  The
    # monotonic anchor must keep our timestamps stable.
    monkeypatch.setattr(C, 'local_now_ms', lambda: sent - 3_600_000)
    second = C.now_ms()
    assert abs(second - first) < 2_000


def test_persistence_round_trip_and_ttl(tmp_path):
    path = str(tmp_path / 'clock.json')
    now = C.local_now_ms()
    C.apply_server_time(now + 95_000, now, now)
    assert C.save(path) is True
    C.reset()
    assert C.load(path) is True
    assert abs(C.offset_ms() - 95_000) < 1_000

    # Age the saved measurement past the TTL: a machine restart may have
    # fixed its clock, so a stale offset must not be applied again.
    with open(path, encoding='utf-8') as f:
        doc = json.load(f)
    doc['measured_local_ms'] -= C.SAVED_TTL_MS + 60_000
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(doc, f)
    C.reset()
    assert C.load(path) is False
    assert C.source() == 'local'


def test_fmt_skew_is_readable():
    assert C.fmt_skew(125_000) == '125 秒'
    assert C.fmt_skew(3 * 60_000) == '3 分钟'
    assert C.fmt_skew(2 * 3600_000) == '2.0 小时'


# --------------------------------------------------------------------------
# RealClient.server_time
# --------------------------------------------------------------------------

def test_server_time_applies_the_measured_offset(monkeypatch):
    class Fake(RC.RealClient):
        def __init__(self):
            RC.RealClient.__init__(self, backend='netsec')

        def request(self, *a, **k):
            return {'json': {'error': 10000,
                             'data': 1_800_000_060_123}}

    sent = 1_800_000_000_000
    received = sent + 500
    values = [sent, received]
    monkeypatch.setattr(
        C, 'local_now_ms',
        lambda: values.pop(0) if values else received)
    out = Fake().server_time(RC.Account(name='t', uid='1', token='x'))
    assert out['applied'] is True
    assert out['offset_ms'] == 1_800_000_060_123 - (sent + received) // 2
    assert C.source() == 'gmttime'


def test_server_time_falls_back_to_the_http_date_header(monkeypatch):
    class Fake(RC.RealClient):
        def __init__(self):
            RC.RealClient.__init__(self, backend='netsec')

        def request(self, *a, **k):
            return {'json': None,
                    'date_header': 'Wed, 04 Nov 2026 03:14:17 GMT'}

    expected = int(datetime(2026, 11, 4, 3, 14, 17,
                            tzinfo=UTC).timestamp() * 1000)
    values = [expected - 200, expected - 100]
    monkeypatch.setattr(
        C, 'local_now_ms',
        lambda: values.pop(0) if values else expected - 100)
    out = Fake().server_time(RC.Account(name='t', uid='1', token='x'))
    assert out['source'] == 'http-date'
    assert out['server_ms'] == expected
    assert out['applied'] is True
    assert C.source() == 'http-date'


def test_server_time_can_measure_without_applying(monkeypatch):
    class Fake(RC.RealClient):
        def __init__(self):
            RC.RealClient.__init__(self, backend='netsec')

        def request(self, *a, **k):
            return {'json': {'error': 10000, 'data': 1_800_000_000_000}}

    values = [1_800_000_000_000, 1_800_000_000_100]
    monkeypatch.setattr(
        C, 'local_now_ms',
        lambda: values.pop(0) if values else 1_800_000_000_100)
    out = Fake().server_time(RC.Account(name='t', uid='1', token='x'),
                             apply=False)
    assert out['offset_ms'] == -50
    assert C.source() == 'local'


# --------------------------------------------------------------------------
# AMap reverse geocoding (official `MapLogKt.getCity`)
# --------------------------------------------------------------------------

def test_regeo_city_reads_city(monkeypatch):
    monkeypatch.setenv('JHP_AMAP_KEY', 'test-key')
    seen = {}

    def fake_get(url, params, **kwargs):
        seen['url'] = url
        seen['params'] = params
        seen['kwargs'] = kwargs
        return {'regeocode': {'addressComponent': {
            'city': '江门市', 'province': '广东省'}}}

    monkeypatch.setattr(amap, '_get', fake_get)
    assert amap.regeo_city(22.5, 113.0) == '江门市'
    assert '/geocode/regeo' in seen['url']
    assert seen['params']['location'] == '113.000000,22.500000'


def test_regeo_city_falls_back_to_province_for_municipalities(monkeypatch):
    monkeypatch.setenv('JHP_AMAP_KEY', 'test-key')
    monkeypatch.setattr(amap, '_get', lambda *a, **k: {
        'regeocode': {'addressComponent': {'city': [], 'province': '北京市'}}})
    assert amap.regeo_city(39.9, 116.4) == '北京市'


def test_regeo_city_returns_empty_when_unknown(monkeypatch):
    monkeypatch.setenv('JHP_AMAP_KEY', 'test-key')
    monkeypatch.setattr(amap, '_get', lambda *a, **k: {
        'regeocode': {'addressComponent': {'city': '', 'province': ''}}})
    assert amap.regeo_city(0.0, 0.0) == ''


def test_address_for_start_prefers_reverse_geocode(monkeypatch):
    monkeypatch.setattr(amap, 'regeo_city', lambda *a, **k: '广州市')
    assert RK.address_for_start(23.0, 113.4, '广东工业大学', '') == '广州市'


def test_address_for_start_falls_back_to_the_campus_city(monkeypatch):
    monkeypatch.setattr(amap, 'regeo_city', lambda *a, **k: '')
    assert RK.address_for_start(22.5, 113.0, '广州软件学院 江门校区',
                                '广州软件学院') == '江门市'


def test_address_for_start_never_sends_the_school_name(monkeypatch):
    monkeypatch.setattr(amap, 'regeo_city', lambda *a, **k: '')
    assert RK.address_for_start(23.0, 113.4, '广东工业大学', '') == ''


# --------------------------------------------------------------------------
# preflight wording and tri-state city
# --------------------------------------------------------------------------

def _noon_ms():
    return RK.safety.now_ms()


def test_preflight_reports_automatic_clock_correction():
    out = RK.preflight('江门校区', '', ip_info={'city': '江门'},
                       start_ms=_noon_ms(), clock_offset_ms=125_000,
                       explicit_start=True)
    assert out['info']['clock_applied'] is True
    assert out['info']['ip_city_matches'] is True
    text = '\n'.join(out['warnings'])
    assert '已自动改用服务器时间' in text
    assert '不需要修改系统设置' in text
    assert '校准系统时间' not in text


def test_preflight_clock_failure_is_honest_but_not_a_blocker():
    out = RK.preflight('江门校区', '', ip_info={'city': '江门'},
                       start_ms=_noon_ms(), clock_error='timeout',
                       explicit_start=True)
    assert out['info']['clock_applied'] is False
    text = '\n'.join(out['warnings'])
    assert '未能读取服务器时间' in text
    assert '不会修改系统设置' in text


def test_preflight_city_match_is_tri_state():
    common = {'start_ms': _noon_ms(), 'explicit_start': True}
    same = RK.preflight('江门校区', '', ip_info={'city': '江门市'}, **common)
    diff = RK.preflight('江门校区', '', ip_info={'city': '北京市'}, **common)
    unknown = RK.preflight('江门校区', '', ip_info={'city': ''}, **common)
    assert same['info']['ip_city_matches'] is True
    assert diff['info']['ip_city_matches'] is False
    assert unknown['info']['ip_city_matches'] is None


# --------------------------------------------------------------------------
# detail endpoint fallback (Inverx 7.3.92)
# --------------------------------------------------------------------------

def test_detail_fallback_prefers_v70320():
    # 7.4.0 dex string pool: v70320 is the detail route (PROTO_DIFF_7.4.0).
    assert RO.DETAIL_PATHS[0] == 'api/v70320/runnings/get_one_record'
    assert 'api/v70260/runnings/get_one_record' in RO.DETAIL_PATHS
    assert 'api/v61200/runnings/get_one_record' in RO.DETAIL_PATHS

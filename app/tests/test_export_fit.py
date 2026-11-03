"""FIT export: framing, CRC, message set, semicircle coordinates, GCJ->WGS."""

import base64
import datetime as _dt
import struct

import pytest

from replica.application import export_fit as E
from replica.domain import coords

START = 1_700_000_000_000


def track(n=20, start_lat=22.5432, start_lng=113.9345):
    return [{'lat': start_lat + i * 1e-5, 'lng': start_lng + i * 2e-5,
             'altitude': 20.0 + (i % 5), 'distance_m': i * 12.5,
             'duration_s': i * 10} for i in range(n)]


def test_header_and_crc_are_valid():
    blob = E.build(track(), START)
    assert blob[:1] == b'\x0e'                    # 14-byte header
    assert blob[8:12] == b'.FIT'
    assert E.crc16(blob[:12]) == struct.unpack('<H', blob[12:14])[0]
    data_size = struct.unpack('<I', blob[4:8])[0]
    end = 14 + data_size
    assert E.crc16(blob[14:end]) == struct.unpack('<H', blob[end:end + 2])[0]
    assert len(blob) == end + 2


def test_known_crc16_vector():
    # The FIT SDK documents "123456789" -> 0xBB3D for this CRC.
    assert E.crc16(b'123456789') == 0xBB3D


def test_messages_present():
    info = E.inspect(E.build(track(7), START))
    assert info['ok'] and info['header_crc_ok'] and info['file_crc_ok']
    assert info['messages'] == {'file_id': 1, 'record': 7, 'lap': 1,
                                'session': 1, 'activity': 1}


def test_inspect_detects_truncation_and_corruption():
    blob = E.build(track(), START)
    assert E.inspect(blob[:-3])['ok'] is False
    broken = bytearray(blob)
    broken[40] ^= 0xFF
    assert E.inspect(bytes(broken))['ok'] is False
    assert E.inspect(b'not a fit file at all')['ok'] is False


def test_input_order_is_sorted_by_time():
    pts = list(reversed(track(5)))
    assert E.inspect(E.build(pts, START))['messages']['record'] == 5


def test_no_track_raises():
    with pytest.raises(ValueError):
        E.build([], START)
    with pytest.raises(ValueError):
        E.build([{'lat': 0, 'lng': 0}, {'lat': 0, 'lng': 0}], START)


def test_semicircles_round_trip():
    lat, lng = 22.5432, 113.9345
    s_lat, s_lng = E.semicircles(lat, lng)
    assert abs(s_lat / 2 ** 31 * 180 - lat) < 1e-6
    assert abs(s_lng / 2 ** 31 * 180 - lng) < 1e-6


def test_positions_are_converted_from_gcj_to_wgs():
    gcj_lat, gcj_lng = 22.5432, 113.9345
    wgs = coords.gcj02_to_wgs84(gcj_lat, gcj_lng)
    assert wgs != (gcj_lat, gcj_lng)
    got = E._wgs84(gcj_lat, gcj_lng)
    assert abs(got[0] - wgs[0]) < 1e-9 and abs(got[1] - wgs[1]) < 1e-9


def test_fit_time_epoch():
    epoch = int(_dt.datetime(1989, 12, 31, tzinfo=_dt.timezone.utc)
                .timestamp() * 1000)
    assert E.fit_time(epoch) == 0
    assert E.fit_time(epoch + 3_600_000) == 3600
    assert E.fit_time(-5) == 0
    assert E.fit_time(0) == 0


def test_export_envelope_and_filename():
    res = E.export(track(), START)
    assert res['mime'] == 'application/vnd.garmin.fit'
    assert res['filename'].startswith('haoxiangpao-20231114') \
        and res['filename'].endswith('.fit')
    raw = base64.b64decode(res['data_b64'])
    assert raw[:1] == b'\x0e' and raw[8:12] == b'.FIT'
    assert res['points'] == 20 and res['bytes'] == len(raw)


def test_export_without_start_time_still_works():
    res = E.export(track(), 0, filename='../../自定义 名称')
    # CJK is kept; separators that could escape a folder are stripped.
    assert res['filename'] == '自定义 名称.fit'
    for bad in ('/', '\\', ':'):
        assert bad not in res['filename']
    raw = base64.b64decode(res['data_b64'])
    assert E.inspect(raw)['ok']


def test_export_uses_gcj_when_lat_placeholder_is_minus_one():
    blob = E.build([
        {'lat': -1.0, 'lng': -1.0, 'gLat': 22.4100, 'gLng': 112.9400,
         'totalTime': 0, 'totalDis': 0},
        {'lat': -1.0, 'lng': -1.0, 'gLat': 22.4110, 'gLng': 112.9410,
         'totalTime': 60, 'totalDis': 100},
    ], 1_792_584_000_000)
    info = E.inspect(blob)
    assert info['ok'] is True and info['messages']['record'] == 2


def test_export_accepts_preview_style_points():
    pts = [{'lat': 22.5, 'lng': 113.9, 'alt': 30, 'totalDis': i * 10,
            'totalTime': i * 5} for i in range(6)]
    info = E.inspect(base64.b64decode(E.export(pts, START)['data_b64']))
    assert info['ok'] and info['messages']['record'] == 6


def test_self_test_passes():
    assert all(ok for _, ok, _ in E.self_test())

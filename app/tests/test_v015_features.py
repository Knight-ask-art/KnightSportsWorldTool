"""v0.1.5 backend features: chart series, login cover, minimum version.

Covers the pieces that landed with 0.1.5 and had no tests yet:

* ``rundata.preview_series`` / ``rundata.track_series`` -- the 10 s chart
  series behind the pre-submit preview and the record detail page;
* ``bridge_api._image_info`` -- header-only image probes (no Pillow in the
  frozen build);
* the login-cover bridge methods, which must stay landscape-only and keep
  every artefact inside ``store_dir``;
* ``updater.check`` carrying ``min_version`` / ``required`` from the release
  manifest.

Everything runs offline and inside ``tmp_path``.
"""

from __future__ import annotations

import base64
import io
import json
import math
import os
import random
import struct
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from replica.adapters.bridge_api import ReplicaApi, _image_info  # noqa: E402
from replica.adapters.http_server import login_cover_css  # noqa: E402
from replica.application import readonly, updater  # noqa: E402
from replica.domain.rundata import (  # noqa: E402
    generate_run_data,
    preview_series,
    track_series,
)

WAYPOINTS = [(22.4108, 112.9408), (22.4118, 112.9418), (22.4100, 112.9410)]
PACE = 6.0


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _run(km: float, seed: int = 7) -> dict:
    return generate_run_data(km, PACE, 150, waypoints=WAYPOINTS, closed=True,
                             rng=random.Random(seed))


def _displayed_paces(run: dict) -> list[int]:
    """Whole seconds per kilometre, the number the split table shows.

    ``_distinct_paces`` works in this unit and rounds half up, matching the
    browser's ``Math.round``; raw split seconds are not the same thing.
    """
    return [int(math.floor(s['seconds'] / (s['km'] or 1e-9) + 0.5))
            for s in run['km_splits']]


def _png(width: int, height: int) -> bytes:
    """Minimal PNG whose IHDR is all ``_image_info`` needs to read."""
    ihdr = (struct.pack('>I', 13) + b'IHDR'
            + struct.pack('>II', width, height)
            + b'\x08\x02\x00\x00\x00')
    raw = (b'\x89PNG\r\n\x1a\n' + ihdr + b'\x00' * 4        # fake CRC
           + b'\x00\x00\x00\x00IDAT' + b'\x00' * 4
           + b'\x00\x00\x00\x00IEND' + b'\x00' * 4)
    assert len(raw) >= 32
    return raw


def _jpeg(width: int, height: int) -> bytes:
    """SOI + APP0 + SOF0 + EOI: enough header for a size probe."""
    app0 = b'\xff\xe0' + struct.pack('>H', 16) + b'JFIF\x00\x01\x01\x00\x00'
    app0 += b'\x00\x01\x00\x01\x00\x00'
    sof0 = (b'\xff\xc0' + struct.pack('>H', 11) + b'\x08'
            + struct.pack('>HH', height, width) + b'\x01\x01\x11\x00')
    return b'\xff\xd8' + app0 + sof0 + b'\xff\xd9'


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode('ascii')


# --------------------------------------------------------------------------
# preview_series
# --------------------------------------------------------------------------
@pytest.mark.parametrize('km', [2.5, 4.0, 6.0])
def test_preview_series_matches_the_record(km):
    run = _run(km)
    preview = preview_series(run)

    assert preview['series']
    for point in preview['series']:
        assert {'t', 'pace', 'cadence', 'stride', 'alt'} <= set(point)
    times = [p['t'] for p in preview['series']]
    assert all(b > a for a, b in zip(times, times[1:]))

    splits = run['km_splits']
    assert sum(s['seconds'] for s in splits) == run['sportTime']
    assert run['sportTime'] == round(km * PACE * 60)
    assert [s['seconds'] for s in preview['splits']] == [
        s['seconds'] for s in splits]
    # splits[].pace is seconds per kilometre (the UI divides by 60 before
    # fmtPace); minutes/km here would display every lap 60x too fast.
    for s in preview['splits']:
        assert s['pace'] == pytest.approx(
            s['seconds'] / (s['km'] or 1e-9), abs=0.2)


@pytest.mark.parametrize('km,seed', [
    (2.5, 7), (4.0, 7), (6.0, 7),
    (10.0, 7),
    (10.0, 30),   # used to leave three full laps at 370 s (6'10"/km)
])
def test_every_kilometre_has_its_own_displayed_pace(km, seed):
    run = _run(km, seed)
    paces = _displayed_paces(run)

    assert len(set(paces)) == len(paces), (km, seed, paces)
    assert sum(s['seconds'] for s in run['km_splits']) == run['sportTime']
    assert run['sportTime'] == round(km * PACE * 60)


@pytest.mark.parametrize('km', [5.0, 10.0])
def test_equal_manual_lap_times_become_distinct(km):
    """The split editor sends whole seconds back; equal rows must not stay equal."""
    n = math.ceil(km)
    run = generate_run_data(km, PACE, 150, waypoints=WAYPOINTS, closed=True,
                            km_times=[360] * n, rng=random.Random(3))
    paces = _displayed_paces(run)

    assert len(set(paces)) == len(paces), paces
    assert sum(s['seconds'] for s in run['km_splits']) == run['sportTime']
    assert run['sportTime'] == 360 * n


def test_preview_series_tolerates_an_empty_run():
    out = preview_series({})
    assert out['series'] == []
    assert out['splits'] == []
    assert out['duration_s'] == 0


def test_preview_series_tolerates_missing_fields():
    run = {
        'allLocJson': None,
        'speed_json': [{'beginTime': 1000, 'endTime': 11000,
                        'distance': 0}],
        'step_freq_json': None,
        'km_splits': [{'km': 1.0}, {'seconds': 0}],
        'sportTime': 10,
        'juli': 0,
    }
    out = preview_series(run)
    assert len(out['series']) == 1
    # No movement -> no pace/cadence/stride rather than a divide-by-zero.
    assert out['series'][0]['pace'] is None
    assert out['series'][0]['cadence'] is None
    assert out['series'][0]['stride'] is None
    assert out['series'][0]['alt'] is None
    assert out['splits'][0]['seconds'] == 0


# --------------------------------------------------------------------------
# track_series
# --------------------------------------------------------------------------
@pytest.mark.parametrize('track', [
    [],
    [{'duration_s': 0, 'distance_m': 0}],
    [None, 'x'],
    [{'duration_s': 0, 'distance_m': 0},
     {'duration_s': 0, 'distance_m': 0}],
])
def test_track_series_needs_two_usable_points(track):
    assert track_series(track) is None


def test_track_series_from_generated_loc_points():
    run = _run(4.0)
    track = readonly.parse_track(run['locPoints'])
    assert len(track) >= 2

    out = track_series(track, step_windows=run['step_freq_json'],
                       start_ms=run['locPoints'][0]['flag'])
    assert out is not None
    assert out['duration_s'] == run['sportTime']
    assert out['distance_m'] == pytest.approx(run['juli'], abs=0.1)
    assert out['series']
    for point in out['series']:
        assert {'t', 'pace', 'cadence', 'stride', 'alt'} <= set(point)
    # 7.4.0 points have no `steps`; cadence must come from step_freq_json.
    assert out['avg_cadence'] is not None, out
    assert 120.0 <= out['avg_cadence'] <= 180.0, out

    assert out['splits']
    assert sum(s['seconds'] for s in out['splits']) <= out['duration_s']
    assert all(s['cumulative'] <= out['duration_s'] for s in out['splits'])
    first = out['splits'][0]
    assert first['km'] == 1.0
    # Seconds per kilometre, not minutes: 6'00"/km is ~360, not 6.
    assert first['pace'] == pytest.approx(first['seconds'], abs=1.0)


# --------------------------------------------------------------------------
# _image_info
# --------------------------------------------------------------------------
@pytest.mark.parametrize('width,height', [(1200, 600), (600, 1200)])
def test_image_info_reads_png_size(width, height):
    assert _image_info(_png(width, height)) == ('image/png', width, height)


@pytest.mark.parametrize('width,height', [(1920, 1080), (1080, 1920)])
def test_image_info_reads_jpeg_size(width, height):
    assert _image_info(_jpeg(width, height)) == ('image/jpeg', width, height)


def test_image_info_rejects_non_images_and_short_buffers():
    assert _image_info(b'not an image, definitely not' * 2) is None
    assert _image_info(b'\x00' * 64) is None
    assert _image_info(_png(1200, 600)[:20]) is None


def test_image_info_reads_a_real_pillow_image():
    image_module = pytest.importorskip('PIL.Image')
    raw = io.BytesIO()
    image_module.new('RGB', (1600, 900), (12, 34, 56)).save(raw, 'PNG')
    assert _image_info(raw.getvalue()) == ('image/png', 1600, 900)


def test_login_cover_css_is_empty_without_a_stored_cover(tmp_path):
    css = login_cover_css(str(tmp_path))
    assert 'background-image' not in css
    assert '#loginGate' not in css


def test_login_cover_css_carries_the_stored_stamp(tmp_path):
    (tmp_path / 'login-cover.img').write_bytes(b'fake-png')
    (tmp_path / 'login-cover.json').write_text(
        '{"updated_at": 123456}', encoding='utf-8')
    css = login_cover_css(str(tmp_path))
    assert '#loginGate' in css
    assert '/login-cover?t=123456' in css


# --------------------------------------------------------------------------
# login cover (settings -> 登录页封面)
# --------------------------------------------------------------------------
def _api(tmp_path, monkeypatch):
    # Keep JHP_DATA_DIR pointed at the test store; ReplicaApi.__init__ sets it.
    monkeypatch.setenv('JHP_DATA_DIR', str(tmp_path))
    return ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                      str(tmp_path))


def test_login_cover_rejects_a_portrait_image(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch)
    result = api.real_set_login_cover(data=_b64(_png(600, 1200)))
    assert result['code'] != 200
    assert '横图' in result['message']
    assert not (tmp_path / 'login-cover.img').exists()


def test_login_cover_rejects_a_tiny_image(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch)
    result = api.real_set_login_cover(data=_b64(_jpeg(400, 200)))
    assert result['code'] != 200
    assert '太小' in result['message']
    assert not (tmp_path / 'login-cover.img').exists()


@pytest.mark.parametrize('width,height', [
    (3200, 600),   # 16:3 banner the old 1920x360 re-encode used to reject
    (1920, 360),   # exactly what that re-encode produced
    (1200, 300),   # wide, short, but still above the area floor
])
def test_login_cover_accepts_wide_banners(tmp_path, monkeypatch, width, height):
    api = _api(tmp_path, monkeypatch)
    result = api.real_set_login_cover(data=_b64(_png(width, height)))
    assert result['code'] == 200, result
    assert (result['data']['width'], result['data']['height']) == (width, height)
    assert (tmp_path / 'login-cover.img').is_file()


def test_login_cover_rejects_too_few_pixels(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch)
    result = api.real_set_login_cover(data=_b64(_png(900, 200)))
    assert result['code'] != 200
    assert '太小' in result['message']
    assert not (tmp_path / 'login-cover.img').exists()


def test_login_cover_roundtrip_stays_inside_the_store(tmp_path, monkeypatch):
    api = _api(tmp_path, monkeypatch)
    raw = _png(1200, 600)

    saved = api.real_set_login_cover(data='data:image/png;base64,' + _b64(raw))
    assert saved['code'] == 200, saved
    assert saved['data']['has_custom'] is True
    assert saved['data']['width'] == 1200
    assert saved['data']['height'] == 600

    cover = tmp_path / 'login-cover.img'
    meta = tmp_path / 'login-cover.json'
    assert cover.is_file() and cover.read_bytes() == raw
    assert meta.is_file()
    detail = json.loads(meta.read_text(encoding='utf-8'))
    assert detail['mime'] == 'image/png'
    assert (detail['width'], detail['height']) == (1200, 600)

    got = api.real_get_login_cover()
    assert got['code'] == 200
    assert got['data']['has_custom'] is True
    assert got['data']['url'].startswith('/login-cover?t=')
    assert got['data']['url'][len('/login-cover?t='):].isdigit()

    cleared = api.real_clear_login_cover()
    assert cleared['code'] == 200
    assert cleared['data']['has_custom'] is False
    assert api.real_get_login_cover()['data']['has_custom'] is False
    assert not cover.exists() and not meta.exists()


# --------------------------------------------------------------------------
# updater: min_version / required
# --------------------------------------------------------------------------
def _manifest(version='0.1.5', min_version='0.1.4') -> dict:
    return {
        'schema': 1,
        'app': 'haoxiangpao',
        'version': version,
        'min_version': min_version,
        'notes': ['修复若干问题'],
        'assets': {'windows-x64': {
            'file': 'haoxiangpao_v%s_win64.zip' % version,
            'url': 'pkg.zip',
            'size': 123,
            'sha256': 'a' * 64,
        }},
    }


def _check(monkeypatch, current: str, **manifest_kw) -> dict:
    manifest = _manifest(**manifest_kw)
    monkeypatch.setattr(updater, 'fetch_manifest',
                        lambda url: dict(manifest))
    return updater.check(current=current,
                         manifest_url='https://example.com/latest.json',
                         prefer='zip')


def test_check_requires_an_update_below_the_minimum(monkeypatch):
    result = _check(monkeypatch, '0.1.3', version='0.1.5',
                    min_version='0.1.4')
    assert result['state'] == 'available'
    assert result['min_version'] == '0.1.4'
    assert result['required'] is True


def test_check_at_the_latest_version_is_current(monkeypatch):
    result = _check(monkeypatch, '0.1.5', version='0.1.5',
                    min_version='0.1.4')
    assert result['state'] == 'current'
    assert result['required'] is False


def test_check_min_version_boundary(monkeypatch):
    """``required`` means "older than min_version", not "at" it.

    docs/update.md and the dialog copy ("当前 vX 低于最低支持版本 vY") both
    define the threshold strictly: a client already on ``min_version`` can
    upgrade directly, so no mandatory update is raised.
    """
    result = _check(monkeypatch, '0.1.4', version='0.1.5',
                    min_version='0.1.4')
    assert result['state'] == 'available'
    assert result['required'] is False


def test_equal_manual_splits_get_distinct_paces_without_moving_the_total():
    """Twelve identical 1 km laps used to defeat the old distinct-seconds helper."""
    from replica.domain.rundata import _distinct_paces

    out = _distinct_paces([1000.0] * 12, [360] * 12, 4320)
    assert sum(out) == 4320
    assert len(set(out)) == 12, out


def test_short_tail_is_not_turned_into_a_twelve_minute_pace():
    """One split second is 2 s/km on 0.5 km; the mover must work in pace units."""
    from replica.domain.rundata import _distinct_paces

    out = _distinct_paces([1000.0] * 7 + [500.0], [360] * 7 + [180], 2700)
    assert sum(out) == 2700
    tail_pace = out[-1] / 0.5
    assert len({int(round(out[i] / (1 if i < 7 else 0.5))) for i in range(8)}) == 8
    assert 300 <= tail_pace <= 420, tail_pace


def test_last_result_accepts_the_powershell_utf8_bom(tmp_path):
    """Out-File -Encoding utf8 on Windows PowerShell writes EF BB BF."""
    updates = tmp_path / 'updates'
    updates.mkdir()
    (updates / 'last-result.json').write_text(
        '\ufeff{"state":"ok","version":"0.1.5","message":"更新完成"}',
        encoding='utf-8')
    assert updater.last_result(str(tmp_path))['state'] == 'ok'

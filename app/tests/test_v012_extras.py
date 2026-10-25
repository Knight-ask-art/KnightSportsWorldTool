"""v0.1.2 extras: elevation modes, segment fence, raw detail, build identity."""
import argparse
import json
import random
from pathlib import Path

import pytest

from replica.adapters.bridge_api import APP_VERSION, ReplicaApi
from replica.adapters.cli import _altitude_kwargs, main
from replica.application import readonly as RO
from replica.build_info import VERSION, build_info, version_string
from replica.domain import routes as RT
from replica.domain.rundata import (ALTITUDE_MODES, generate_run_data,
                                    make_altitudes,
                                    parse_altitude_template, resample_profile)

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# elevation
# --------------------------------------------------------------------------

def test_altitude_modes_cover_every_public_mode():
    assert set(ALTITUDE_MODES) == {'auto', 'fixed', 'range', 'template'}


def test_fixed_altitude_has_no_climb():
    rng = random.Random(3)
    alts = make_altitudes(120, rng, base=88.0, mode='fixed')
    assert set(alts) == {88.0}
    run = generate_run_data(2.5, 6.0, 172, center_lat=22.4108,
                            center_lng=112.9408, altitude_mode='fixed',
                            altitude_base=88.0, run_uuid='fixed-test')
    assert run['totalAscent'] == 0
    assert {p['alt'] for p in run['allLocJson']} == {88.0}


def test_range_altitude_stays_inside_the_requested_band():
    rng = random.Random(4)
    alts = make_altitudes(200, rng, base=25.0, mode='range',
                          alt_range=(120, 240))
    assert min(alts) >= 120.0 and max(alts) <= 240.0
    assert max(alts) - min(alts) > 80.0          # a real profile, not a flat line


def test_template_altitude_keeps_the_profile_shape():
    profile = [10.0, 12.0, 20.0, 35.0, 20.0, 12.0, 10.0]
    rng = random.Random(5)
    alts = make_altitudes(70, rng, mode='template', profile=profile)
    assert min(alts) < 13.0 and max(alts) > 33.0
    assert alts[0] < alts[len(alts) // 2]      # starts low, climbs, comes back


def test_template_can_be_mapped_into_a_range():
    profile = [0.0, 5.0, 10.0]
    rng = random.Random(6)
    alts = make_altitudes(60, rng, mode='template', profile=profile,
                          alt_range=(300, 360))
    assert min(alts) >= 300.0 and max(alts) <= 360.0
    assert max(alts) - min(alts) > 40.0


@pytest.mark.parametrize('kwargs', [
    {'mode': 'range'},
    {'mode': 'template'},
    {'mode': 'nope'},
])
def test_bad_altitude_arguments_raise(kwargs):
    with pytest.raises(ValueError):
        make_altitudes(10, random.Random(1), **kwargs)


def test_parse_gpx_json_and_csv_templates():
    gpx = ('<gpx><trk><trkseg>'
           '<trkpt><ele>10</ele></trkpt><trkpt><ele>20</ele></trkpt>'
           '<trkpt><ele>30</ele></trkpt></trkseg></trk></gpx>')
    assert parse_altitude_template(gpx) == [10.0, 20.0, 30.0]

    rows = json.dumps([{'ele': 10}, {'ele': 16}, {'ele': 12}])
    assert parse_altitude_template(rows) == pytest.approx([10.0, 16.0, 12.0])

    assert parse_altitude_template('[5, 8, 11]') == [5.0, 8.0, 11.0]
    assert parse_altitude_template('22.41,112.94,12\n22.42,112.95,18') \
        == [12.0, 18.0]


def test_parse_template_rejects_empty_and_out_of_range():
    with pytest.raises(ValueError):
        parse_altitude_template('')
    with pytest.raises(ValueError):
        parse_altitude_template('<gpx></gpx>')
    with pytest.raises(ValueError):
        parse_altitude_template('[1, 99999]')


def test_template_smoothing_removes_a_one_point_spike():
    values = parse_altitude_template('[10, 10, 100, 10, 10]')
    assert max(values) < 100.0
    assert values[2] == pytest.approx(28.0)


def test_resample_keeps_endpoints():
    assert resample_profile([0.0, 10.0], 5) == [0.0, 2.5, 5.0, 7.5, 10.0]
    assert resample_profile([7.0], 4) == [7.0] * 4


def test_cli_altitude_kwargs_from_template_file(tmp_path):
    path = tmp_path / 'profile.gpx'
    path.write_text('<gpx><trkpt><ele>1</ele></trkpt>'
                    '<trkpt><ele>9</ele></trkpt></gpx>', encoding='utf-8')
    args = argparse.Namespace(altitude_mode=None, altitude_range=None,
                              altitude_template=str(path))
    out = _altitude_kwargs(args)
    assert out['altitude_mode'] == 'template'
    assert out['altitude_profile'] == [1.0, 9.0]

    args = argparse.Namespace(altitude_mode=None, altitude_range=[10, 20],
                              altitude_template=None)
    assert _altitude_kwargs(args)['altitude_mode'] == 'range'

    args = argparse.Namespace(altitude_mode='range', altitude_range=None,
                              altitude_template=None)
    with pytest.raises(ValueError):
        _altitude_kwargs(args)


# --------------------------------------------------------------------------
# whole-segment fence check
# --------------------------------------------------------------------------

def _u_shaped_fence():
    # A rectangle with a notch cut into it from the top.  Both test points are
    # inside; the straight line between them crosses the notch.
    return [(22.4100, 112.9400), (22.4100, 112.9440),
            (22.4120, 112.9440), (22.4120, 112.9430),
            (22.4110, 112.9430), (22.4110, 112.9410),
            (22.4120, 112.9410), (22.4120, 112.9400)]


def test_segment_check_catches_a_notch_between_two_inside_points():
    fence = _u_shaped_fence()
    a, b = (22.4115, 112.9405), (22.4115, 112.9435)
    assert RT.point_in_polygon(*a, fence)
    assert RT.point_in_polygon(*b, fence)

    point_only = RT.outside_fence([a, b], fence)
    assert point_only['count'] == 0

    segments = RT.outside_fence_segments([a, b], fence)
    assert segments['count'] == 1
    assert segments['total'] == 1
    seg = segments['segments'][0]
    assert seg['index'] == 0
    assert seg['distance_m'] > 0
    assert RT.point_in_polygon(seg['lat'], seg['lon'], fence) is False


def test_segment_check_accepts_a_fully_inside_line():
    fence = [(22.4100, 112.9400), (22.4100, 112.9420),
             (22.4120, 112.9420), (22.4120, 112.9400)]
    out = RT.outside_fence_segments(
        [(22.4105, 112.9405), (22.4115, 112.9415)], fence)
    assert out == {'count': 0, 'total': 1, 'segments': []}


def test_segment_check_handles_short_inputs():
    ring = [(22.41, 112.94), (22.41, 112.95), (22.42, 112.95)]
    assert RT.outside_fence_segments([], ring)['count'] == 0
    assert RT.outside_fence_segments([(22.41, 112.94)], ring)['count'] == 0
    assert RT.outside_fence_segments([(22.41, 112.94), (22.42, 112.95)],
                                     [])['count'] == 0


# --------------------------------------------------------------------------
# raw detail JSON
# --------------------------------------------------------------------------

def test_raw_record_detail_returns_the_server_object_untouched():
    payload = {'rrid': 7, 'uuid': 'u1', 'totalDis': 2500, 'jsonFromObs': 0,
              'allLocJson': [{'gLat': 22.41, 'gLng': 112.94, 'bdA': 25.0}]}
    fake = RO._FakeClient(by_path={
        'get_one_record': {'error': 10000, 'data': payload}})
    out = RO.raw_record_detail(fake, object(), 7)
    assert out['path'] == RO.DETAIL_PATHS[0]
    assert out['rrid'] == 7
    assert out['detail'] == payload
    assert out['obs'] is None
    assert out['obs_error'] == ''


def test_raw_record_detail_raises_on_business_error():
    fake = RO._FakeClient(by_path={
        'get_one_record': {'error': 400, 'message': 'bad'}})
    with pytest.raises(RO.QueryFailed):
        RO.raw_record_detail(fake, object(), 7)


# --------------------------------------------------------------------------
# build identity / CLI / bridge
# --------------------------------------------------------------------------

def test_version_has_a_single_source():
    assert APP_VERSION == VERSION == '0.1.2'
    info = build_info()
    assert info['version'] == VERSION
    assert set(info) >= {'version', 'commit', 'dirty', 'built_at', 'frozen'}
    assert version_string().startswith('v' + VERSION)


def test_cli_version_prints_the_build_label(capsys):
    with pytest.raises(SystemExit) as exc:
        main(['--version'])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith('v' + VERSION)


def test_bridge_parses_a_template_without_touching_the_network(tmp_path):
    api = ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                     str(tmp_path))
    res = api.real_altitude_template(
        '<gpx><trkpt><ele>4</ele></trkpt><trkpt><ele>14</ele></trkpt></gpx>')
    assert res['code'] == 200
    assert res['data']['min'] == 4.0 and res['data']['max'] == 14.0
    assert res['data']['profile'] == [4.0, 14.0]

    bad = api.real_altitude_template('')
    assert bad['code'] == 500


def test_bridge_reports_the_build_label(tmp_path):
    api = ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                     str(tmp_path))
    res = api.app_version()
    assert res['code'] == 200
    assert res['data']['version'] == VERSION
    assert res['data']['label'].startswith('v' + VERSION)


def test_raw_record_cli_keeps_stdout_valid_json(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv('JHP_DATA_DIR', str(tmp_path))

    class _Client:
        backend = 'fake'
        proxy = None

        def request(self, acct, path, payload=None, **kw):
            return {'status': 200, 'error': None,
                    'json': {'error': 10000, 'data': {
                        'rrid': (payload or {}).get('rrid'),
                        'totalDis': 1234}}}

    code = main(['--token', 't', '--uid', '1', '--record-json', '7'],
                client_factory=lambda **kw: _Client())
    captured = capsys.readouterr()
    assert code == 0
    data = json.loads(captured.out)
    assert data['detail']['rrid'] == 7
    assert data['build'].startswith('v' + VERSION)
    assert '[record]' in captured.err
    assert captured.out.strip().startswith('{')


def test_frontend_exposes_the_new_controls():
    html = (ROOT / 'frontend' / 'index.html').read_text(encoding='utf-8')
    for token in ('id="altMode"', 'id="altMin"', 'id="altMax"',
                  'id="altSummary"', 'id="inAltUse"',
                  'id="altTemplateFile"', 'id="onboard"',
                  'id="onboardOpen"', 'id="sideVersion"'):
        assert token in html, token
    js = (ROOT / 'frontend' / 'js' / 'app.js').read_text(encoding='utf-8')
    for token in ('altitude_mode', 'altitude_profile', 'fenceOutside',
                  'maybeShowOnboard', 'loadAppVersion', '本模式不用',
                  'routeClosed = false', '不建议使用「自动」'):
        assert token in js, token
    assert 'id="closeState">关<' in html

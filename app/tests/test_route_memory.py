"""Tests for route memory core (`replica.application.route_memory`)."""

from __future__ import annotations

import json
import math
import os
import random
import time

from replica.application import route_memory as RM
from replica.domain import routes as ROUTES
from replica.domain import trajectory as T


def _make_rect(
    lat0: float = 22.4100,
    lng0: float = 112.9400,
    dlat: float = 0.00317,
    dlng: float = 0.00340,
    per_edge: int = 12,
    reverse: bool = False,
    laps: int = 1,
    noise_m: float = 0.0,
    seed: int = 0,
) -> list[tuple[float, float]]:
    """Build a synthetic ~1.4 km closed rectangular loop."""
    corners = [
        (lat0 + dlat, lng0),
        (lat0 + dlat, lng0 + dlng),
        (lat0, lng0 + dlng),
        (lat0, lng0),
    ]
    if reverse:
        corners = [corners[0], corners[3], corners[2], corners[1]]
    lap: list[tuple[float, float]] = []
    for k in range(4):
        a = corners[k]
        b = corners[(k + 1) % 4]
        for s in range(per_edge):
            f = s / per_edge
            lap.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))

    pts = lap * laps + [lap[0]]
    if noise_m > 0.0:
        rng = random.Random(seed)
        lat_scale = 1.0 / 110540.0
        lng_scale = 1.0 / (111320.0 * math.cos(math.radians(lat0)))
        noisy = []
        for lat, lng in pts:
            d_lat = rng.gauss(0.0, noise_m) * lat_scale
            d_lng = rng.gauss(0.0, noise_m) * lng_scale
            noisy.append((lat + d_lat, lng + d_lng))
        pts = noisy
    return pts


def _make_out_and_back(
    lat0: float = 22.4100,
    lng0: float = 112.9400,
    dlat: float = 0.0060,
    dlng: float = 0.0020,
    steps: int = 24,
    noise_m: float = 0.0,
    seed: int = 0,
) -> list[tuple[float, float]]:
    """Build an open corridor line (~750 m, clearly non-closed)."""
    pts: list[tuple[float, float]] = []
    for i in range(steps):
        f = i / (steps - 1)
        pts.append((lat0 + dlat * f, lng0 + dlng * f))
    if noise_m > 0.0:
        rng = random.Random(seed)
        lat_scale = 1.0 / 110540.0
        lng_scale = 1.0 / (111320.0 * math.cos(math.radians(lat0)))
        pts = [
            (lat + rng.gauss(0.0, noise_m) * lat_scale,
             lng + rng.gauss(0.0, noise_m) * lng_scale)
            for lat, lng in pts
        ]
    return pts


def _track_dict(
    line,
    *,
    rrid: str = '1001',
    distance_m: float | None = None,
    duration_s: int = 900,
    start_ms: int | None = None,
    is_cheat: int = 0,
    complete: bool = True,
    source: str = 'server',
) -> dict:
    if distance_m is None:
        distance_m = T.polyline_length_m(line)
    if start_ms is None:
        start_ms = int(time.time() * 1000) - 2 * 86400 * 1000
    return {
        'rrid': rrid,
        'line': list(line),
        'distance_m': float(distance_m),
        'duration_s': duration_s,
        'start_ms': start_ms,
        'is_cheat': is_cheat,
        'complete': complete,
        'source': source,
    }


def _assert_no_tuples(obj) -> None:
    assert not isinstance(obj, tuple)
    if isinstance(obj, list):
        for item in obj:
            _assert_no_tuples(item)
    elif isinstance(obj, dict):
        for v in obj.values():
            _assert_no_tuples(v)


def test_track_from_detail_filters_invalid_points():
    nan = float('nan')
    detail = {
        'ok': True,
        'track': [
            {'lat': 22.4101, 'lng': 112.9401},
            {'lat': 0, 'lng': 0},
            {'lat': 0.0, 'lng': 112.9402},
            {'lat': 22.4102, 'lng': 0.0},
            {'lat': -1, 'lng': -1},
            {'lat': -1.0, 'lng': 112.9403},
            {'lat': 22.4103, 'lng': -1.0},
            {'lat': nan, 'lng': 112.9404},
            {'lat': 22.4104, 'lng': nan},
            {'lat': 95.0, 'lng': 112.9405},
            {'lat': 22.4105, 'lon': 112.9405},
            'bad',
            None,
        ],
    }
    pts = RM.track_from_detail(detail)
    assert pts == [(22.4101, 112.9401), (22.4105, 112.9405)]
    assert RM.track_from_detail({}) == []
    assert RM.track_from_detail(None) == []


def test_learn_10_loop_tracks_single_template_real_medoid():
    now_ms = int(time.time() * 1000)
    tracks = []
    expected_candidates = []
    for i in range(10):
        laps = 2 if i % 2 == 0 else 3
        rev = i % 3 == 0
        raw = _make_rect(per_edge=12, reverse=rev, laps=laps, noise_m=5.0, seed=100 + i)
        tracks.append(
            _track_dict(
                raw,
                rrid=f'r{i}',
                distance_m=1400.0 * laps,
                duration_s=450 * laps,
                start_ms=now_ms - (i + 1) * 86400 * 1000,
            )
        )
        simp = T.simplify(raw, 2.5, 400)
        loop = RM._extract_single_lap(simp)
        assert loop is not None
        expected_candidates.append([[float(p[0]), float(p[1])] for p in T.canonical_loop(loop)])

    doc = RM.learn_from_tracks(tracks, owner='100:200', campus={'center': (22.411, 112.941)})
    _assert_no_tuples(doc)
    assert json.loads(json.dumps(doc)) == doc
    assert doc['records_seen'] == 10
    assert doc['records_readable'] == 10
    assert doc['records_skipped'] == 0
    assert len(doc['templates']) == 1

    tpl = doc['templates'][0]
    assert tpl['name'] == '学习路线 1'
    assert tpl['closed'] is True
    assert tpl['samples'] == 10
    assert tpl['medoid_distance_m'] < 40.0
    assert 1300.0 < tpl['lap_m'] < 1550.0
    assert tpl['line'] in expected_candidates


def test_learn_open_and_closed_do_not_merge():
    now_ms = int(time.time() * 1000)
    tracks = []
    for i in range(10):
        raw = _make_rect(per_edge=12, reverse=(i % 2 == 1), laps=2, noise_m=4.0, seed=200 + i)
        tracks.append(
            _track_dict(raw, rrid=f'loop_{i}', distance_m=2800.0, start_ms=now_ms - i * 3600_000)
        )
    for i in range(5):
        raw = _make_out_and_back(steps=25, noise_m=4.0, seed=300 + i)
        tracks.append(
            _track_dict(raw, rrid=f'open_{i}', distance_m=1500.0, start_ms=now_ms - i * 3600_000)
        )

    doc = RM.learn_from_tracks(tracks, owner='100:200')
    assert doc['records_readable'] == 15
    assert len(doc['templates']) == 2
    closed_flags = {t['closed']: t for t in doc['templates']}
    assert True in closed_flags and False in closed_flags
    assert closed_flags[True]['samples'] == 10
    assert closed_flags[False]['samples'] == 5


def test_learn_filters_junk_tracks_and_counts_accurately():
    good_line = _make_rect(per_edge=10, laps=2)
    sparse_line = _make_rect(per_edge=2, laps=1)
    assert len(sparse_line) < RM.MIN_TRACK_POINTS

    tracks = [
        _track_dict(good_line, rrid='good1', distance_m=2800.0),
        _track_dict(good_line, rrid='tool1', distance_m=2800.0, source='tool'),
        _track_dict(good_line, rrid='cheat1', distance_m=2800.0, is_cheat=1),
        _track_dict(good_line, rrid='inc1', distance_m=2800.0, complete=False),
        _track_dict(sparse_line, rrid='sparse1', distance_m=1400.0),
        _track_dict(good_line, rrid='too_long', distance_m=25000.0),
        _track_dict(good_line, rrid='too_short', distance_m=300.0),
    ]
    doc = RM.learn_from_tracks(tracks, owner='u:s')
    assert doc['records_seen'] == 7
    assert doc['records_readable'] == 1
    assert doc['records_skipped'] == 6
    assert len(doc['templates']) == 1
    assert doc['templates'][0]['source_rrids'] == ['good1']


def test_learn_2_5km_and_3_0km_variants_same_template_id():
    # 500 m loop run 5 laps (2.5 km) and 6 laps (3.0 km)
    loop_500 = dict(dlat=0.00113, dlng=0.00112, per_edge=10)
    t_25 = _track_dict(_make_rect(**loop_500, laps=5), rrid='v25', distance_m=2500.0)
    t_30 = _track_dict(_make_rect(**loop_500, laps=6), rrid='v30', distance_m=3000.0)

    doc_25 = RM.learn_from_tracks([t_25], owner='u:s')
    doc_30 = RM.learn_from_tracks([t_30], owner='u:s')
    doc_both = RM.learn_from_tracks([t_25, t_30], owner='u:s')

    assert len(doc_25['templates']) == 1
    assert len(doc_30['templates']) == 1
    assert doc_25['templates'][0]['id'] == doc_30['templates'][0]['id']
    assert len(doc_both['templates']) == 1
    assert doc_both['templates'][0]['samples'] == 2


def test_summarize_empty_weak_and_ok():
    empty_doc = RM.learn_from_tracks([], owner='u:s')
    s_empty = RM.summarize(empty_doc)
    assert s_empty['status'] == 'empty'
    assert s_empty['templates'] == 0
    assert RM.suggest(empty_doc, 2.5, []) == []

    # Fewer than 5 readable -> weak
    tracks_3 = [_track_dict(_make_rect(per_edge=10, laps=2), rrid=f'a{i}') for i in range(3)]
    weak_few = RM.learn_from_tracks(tracks_3, owner='u:s')
    assert RM.summarize(weak_few)['status'] == 'weak'
    assert RM.suggest(weak_few, 2.5, []) == []

    # >= 5 readable, but max sample count < 3 -> weak
    tracks_spread = []
    for i in range(5):
        pts = _make_rect(lat0=22.41 + i * 0.005, per_edge=10, laps=2)
        tracks_spread.append(_track_dict(pts, rrid=f's{i}', distance_m=2800.0))
    weak_spread = RM.learn_from_tracks(tracks_spread, owner='u:s')
    assert RM.summarize(weak_spread)['status'] == 'weak'
    assert RM.suggest(weak_spread, 2.5, []) == []

    # medoid_distance_m > 60 -> weak
    doc_noisy = {
        'version': 1,
        'owner': 'u:s',
        'campus': {},
        'updated_at_ms': 1000,
        'records_seen': 6,
        'records_readable': 6,
        'records_skipped': 0,
        'templates': [{
            'id': 'lr_11111111',
            'name': '学习路线 1',
            'line': [[float(p[0]), float(p[1])] for p in _make_rect(per_edge=4, laps=1)],
            'closed': True,
            'lap_m': 1400.0,
            'samples': 6,
            'source_rrids': ['1'],
            'first_seen_ms': 1000,
            'last_seen_ms': 2000,
            'avg_distance_m': 2800.0,
            'avg_pace': 6.0,
            'quality': {'ok': 6, 'cheat': 0},
            'medoid_distance_m': 65.0,
        }],
    }
    assert RM.summarize(doc_noisy)['status'] == 'weak'
    assert RM.suggest(doc_noisy, 2.5, []) == []

    # Healthy >= 5 readable, >= 3 samples, medoid <= 60 -> ok
    tracks_ok = [_track_dict(_make_rect(per_edge=10, laps=2), rrid=f'k{i}') for i in range(5)]
    doc_ok = RM.learn_from_tracks(tracks_ok, owner='u:s')
    s_ok = RM.summarize(doc_ok)
    assert s_ok['status'] == 'ok'
    assert s_ok['templates'] == 1
    assert s_ok['samples'] == 5
    assert s_ok['usable'] == 5
    assert len(s_ok['templates_detail']) == 1


def test_suggest_fence_must_points_snap_scoring_and_top3():
    now_ms = int(time.time() * 1000)
    # 4 distinct inside templates (> 80 m apart) sharing the north-edge must-point P1
    # (22.41317, 112.9415), plus 1 outside template.
    inside_rect = _make_rect(lat0=22.4100, lng0=112.9400, per_edge=10, laps=2)
    alt_a = _make_rect(
        lat0=22.41317, lng0=112.9400, dlat=0.0020, dlng=0.00340, per_edge=10, laps=2
    )
    alt_b = _make_rect(
        lat0=22.40850, lng0=112.9400, dlat=0.00467, dlng=0.00340, per_edge=10, laps=2
    )
    alt_c = _make_rect(
        lat0=22.41100, lng0=112.9390, dlat=0.00217, dlng=0.00500, per_edge=10, laps=2
    )
    outside_rect = _make_rect(lat0=22.4500, lng0=112.9800, per_edge=10, laps=2)

    tracks = []
    for i in range(5):
        tracks.append(_track_dict(inside_rect, rrid=f'in{i}', distance_m=2800.0, start_ms=now_ms))
        tracks.append(_track_dict(alt_a, rrid=f'a{i}', distance_m=2200.0, start_ms=now_ms))
        tracks.append(_track_dict(alt_b, rrid=f'b{i}', distance_m=3200.0, start_ms=now_ms))
        tracks.append(_track_dict(alt_c, rrid=f'c{i}', distance_m=2800.0, start_ms=now_ms))
        tracks.append(_track_dict(outside_rect, rrid=f'out{i}', distance_m=2800.0, start_ms=now_ms))

    doc = RM.learn_from_tracks(tracks, owner='u:s')
    assert len(doc['templates']) == 5
    fence = [[
        (22.4075, 112.9380),
        (22.4075, 112.9455),
        (22.4165, 112.9455),
        (22.4165, 112.9380),
    ]]

    # 4 points on inside_rect + 1 normal point off the West edge -> 4/5 hit, snap -> 5/5
    pts_normal = [
        {'lat': 22.41317, 'lon': 112.9415, 'isMustPoint': True, 'name': 'P1'},
        {'lat': 22.4115, 'lon': 112.94340, 'isMustPoint': False, 'name': 'P2'},
        {'lat': 22.4100, 'lon': 112.9415, 'isMustPoint': False, 'name': 'P3'},
        {'lat': 22.4100, 'lon': 112.9400, 'isMustPoint': False, 'name': 'P4'},
        {'lat': 22.4116, 'lon': 112.9406, 'isMustPoint': False, 'name': 'P5_off'},
    ]
    cands = RM.suggest(doc, 2.8, pts_normal, fence=fence, center=(22.4100, 112.9400))
    assert len(cands) == 3
    # Outside template must be rejected
    outside_tpl = next(t for t in doc['templates'] if t['line'][0][0] > 22.44)
    assert all(c['template_id'] != outside_tpl['id'] for c in cands)

    top = cands[0]
    assert 0.0 <= top['score'] <= 1.0
    assert top['fence_ok'] is True
    assert top['missed'] == []
    assert ROUTES.missed_points(top['line'], pts_normal) == []
    assert top['detour_m'] > 0.0
    assert any('4/5 点位' in r for r in top['reasons'])
    assert any('围栏内' in r for r in top['reasons'])

    # Must-point miss rejects candidate
    pts_must_miss = [
        {'lat': 22.4115, 'lon': 112.9415, 'isMustPoint': True, 'name': 'MUST_CENTER'},
    ]
    assert RM.suggest(doc, 2.8, pts_must_miss, fence=fence) == []

    # Post-snap fence check rejects if snapping a missed point pulls the line outside
    pts_outside_snap = [
        {'lat': 22.41317, 'lon': 112.9415, 'isMustPoint': True, 'name': 'P1'},
        {'lat': 22.42000, 'lon': 112.9415, 'isMustPoint': False, 'name': 'OUT_SNAP'},
    ]
    assert RM.suggest(doc, 2.8, pts_outside_snap, fence=fence) == []


def test_apply_template_missing_rotation_cap_and_snap():
    tracks = [_track_dict(_make_rect(per_edge=10, laps=2), rrid=f't{i}') for i in range(5)]
    doc = RM.learn_from_tracks(tracks, owner='u:s')
    tpl_id = doc['templates'][0]['id']

    missing = RM.apply_template(doc, 'lr_doesnotexist', 2.5)
    assert missing == {'ok': False, 'error': '模板不存在'}

    # First checkpoint is near SE corner (22.4100, 112.94340) -> closed route rotates there
    cps = [
        {'lat': 22.4100, 'lon': 112.94340, 'isMustPoint': True, 'name': 'SE'},
        {'lat': 22.4115, 'lon': 112.94150, 'isMustPoint': False, 'name': 'MID_OFF'},
    ]
    res = RM.apply_template(doc, tpl_id, 2.8, points=cps)
    assert res['ok'] is True
    assert res['closed'] is True
    assert res['fence_ok'] is True
    assert len(res['waypoints']) <= 400
    assert res['missed'] == []
    assert T.haversine_m(res['waypoints'][0], (22.4100, 112.94340)) < 5.0
    assert res['laps'] >= 1

    # No points -> no snap
    res_no_pts = RM.apply_template(doc, tpl_id, 2.8, points=[])
    assert res_no_pts['ok'] is True
    assert res_no_pts['missed'] == []

    # Waypoints capped at 400 even on dense templates
    dense_line = [
        [22.4100 + 0.00001 * i, 112.9400 + (0.0005 if i % 2 else 0.0)]
        for i in range(450)
    ]
    dense_doc = {
        'version': 1,
        'templates': [{'id': 'lr_dense', 'line': dense_line, 'closed': False, 'lap_m': 1000.0}],
    }
    res_dense = RM.apply_template(dense_doc, 'lr_dense', 2.0)
    assert res_dense['ok'] is True
    assert len(res_dense['waypoints']) <= 400


def test_storage_roundtrip_corruption_version_sanitization_and_isolation(tmp_path):
    store = str(tmp_path)
    tracks_a = [_track_dict(_make_rect(per_edge=10, laps=2), rrid=f'a{i}') for i in range(5)]
    tracks_b = [
        _track_dict(_make_rect(lat0=22.4200, per_edge=10, laps=2), rrid=f'b{i}')
        for i in range(5)
    ]
    doc_a = RM.learn_from_tracks(tracks_a, owner='111:222')
    doc_b = RM.learn_from_tracks(tracks_b, owner='333:444')

    RM.save(store, '111', '222', doc_a)
    RM.save(store, '333', '444', doc_b)

    loaded_a = RM.load(store, '111', '222')
    loaded_b = RM.load(store, '333', '444')
    assert loaded_a['templates'][0]['id'] == doc_a['templates'][0]['id']
    assert loaded_b['templates'][0]['id'] == doc_b['templates'][0]['id']
    assert loaded_a['templates'][0]['id'] != loaded_b['templates'][0]['id']

    # Corrupt file -> empty doc without raising
    path_a = RM.memory_path(store, '111', '222')
    with open(path_a, 'w', encoding='utf-8') as f:
        f.write('{corrupt json')
    corrupt_loaded = RM.load(store, '111', '222')
    assert corrupt_loaded['version'] == RM.SCHEMA_VERSION
    assert corrupt_loaded['templates'] == []

    # Version 99 -> empty doc
    with open(path_a, 'w', encoding='utf-8') as f:
        json.dump({'version': 99, 'templates': [{'id': 'lr_x'}]}, f)
    old_loaded = RM.load(store, '111', '222')
    assert old_loaded['version'] == RM.SCHEMA_VERSION
    assert old_loaded['templates'] == []

    # Clear removes file; clearing missing file is safe
    RM.clear(store, '111', '222')
    assert not os.path.exists(path_a)
    RM.clear(store, '111', '222')

    # Path sanitisation & 80-char cap
    unsafe = RM.memory_path(store, '../a/b:c*d?e"f<g>h|i j', 'x' * 120)
    base = os.path.basename(unsafe)
    assert len(base) <= 80
    assert all(c.isalnum() or c in '_.-' for c in base)
    assert os.path.dirname(unsafe) == os.path.join(store, 'route_memory')


def test_stable_ids_repeat_and_direction_reversal():
    fwd = _make_rect(per_edge=10, reverse=False, laps=2)
    rev = list(reversed(fwd))

    doc1 = RM.learn_from_tracks([_track_dict(fwd, rrid='1')], owner='u:s')
    doc2 = RM.learn_from_tracks([_track_dict(fwd, rrid='1')], owner='u:s')
    doc_rev = RM.learn_from_tracks([_track_dict(rev, rrid='2')], owner='u:s')

    id1 = doc1['templates'][0]['id']
    id2 = doc2['templates'][0]['id']
    id_rev = doc_rev['templates'][0]['id']

    assert id1 == id2
    assert id1 == id_rev


def test_learn_400m_track_with_200m_min_lap():
    """A real 400 m oval must not be filtered by the old 500 m floor."""
    now_ms = int(time.time() * 1000)
    tracks = []
    for i in range(3):
        raw = _make_rect(
            dlat=0.0009, dlng=0.0010, per_edge=10, laps=4,
            noise_m=4.0, seed=700 + i,
        )
        tracks.append(
            _track_dict(
                raw, rrid=f'track400_{i}', distance_m=1620.0,
                duration_s=600, start_ms=now_ms - i * 3600_000,
            )
        )
    doc = RM.learn_from_tracks(tracks, owner='400:track')
    assert doc['records_readable'] == 3
    assert len(doc['templates']) == 1
    tpl = doc['templates'][0]
    assert tpl['closed'] is True
    assert tpl['samples'] == 3
    assert 350.0 <= tpl['lap_m'] <= 500.0


def test_load_corrupt_numeric_fields_does_not_raise(tmp_path):
    store = str(tmp_path)
    path = RM.memory_path(store, 'u', 's')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = [[float(p[0]), float(p[1])] for p in _make_rect(per_edge=4, laps=1)]
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({
            'version': 1,
            'owner': 'u:s',
            'campus': {},
            'updated_at_ms': {'bad': 1},
            'records_seen': 'many',
            'records_readable': None,
            'records_skipped': [],
            'templates': [{
                'id': 'lr_badfields',
                'name': 'bad',
                'line': line,
                'closed': True,
                'lap_m': 'not-a-number',
                'samples': 'nope',
                'last_seen_ms': {},
                'medoid_distance_m': 'far',
            }],
        }, f, ensure_ascii=False)

    doc = RM.load(store, 'u', 's')
    assert doc['records_seen'] == 0
    assert doc['records_readable'] == 0
    summary = RM.summarize(doc)
    assert summary['status'] in ('weak', 'empty')
    # Must not raise on malformed template metrics.
    assert RM.suggest(doc, 2.5, []) == []


def test_closed_ring_checkpoint_near_closing_segment():
    tracks = [
        _track_dict(_make_rect(per_edge=10, laps=2), rrid=f'close{i}')
        for i in range(5)
    ]
    doc = RM.learn_from_tracks(tracks, owner='u:close')
    tpl = doc['templates'][0]
    ring = RM._close_ring([(float(p[0]), float(p[1])) for p in tpl['line']])
    a, b = ring[-2], ring[-1]
    cp = {
        'lat': (a[0] + b[0]) / 2.0,
        'lon': (a[1] + b[1]) / 2.0,
        'isMustPoint': False,
        'name': 'CLOSING_SEGMENT',
    }
    assert ROUTES.missed_points(ring, [cp]) == []
    cands = RM.suggest(doc, 2.8, [cp], center=(22.4100, 112.9400))
    assert cands
    assert cands[0]['missed'] == []

    res = RM.apply_template(doc, tpl['id'], 2.8, points=[cp])
    assert res['ok'] is True
    assert T.haversine_m(res['waypoints'][0], res['waypoints'][-1]) < 1e-3


def test_suggest_ignores_malformed_checkpoints():
    tracks = [
        _track_dict(_make_rect(per_edge=10, laps=2), rrid=f'cp{i}')
        for i in range(5)
    ]
    doc = RM.learn_from_tracks(tracks, owner='u:cp')
    cps = [
        {'lat': None, 'lon': 112.94, 'isMustPoint': True},
        {'lat': 22.41, 'lng': None, 'isMustPoint': False},
        {'lat': 'x', 'lon': 'y'},
        {'lat': 22.4100, 'lon': 112.9400, 'isMustPoint': False},
    ]
    assert RM.suggest(doc, 2.8, cps) == RM.suggest(doc, 2.8, [cps[-1]])


def test_apply_template_rejects_more_than_30_laps():
    tracks = [
        _track_dict(_make_rect(dlat=0.0009, dlng=0.0010, per_edge=10, laps=4),
                    rrid=f'lap{i}', distance_m=1620.0)
        for i in range(3)
    ]
    doc = RM.learn_from_tracks(tracks, owner='u:lap')
    tpl = doc['templates'][0]
    res = RM.apply_template(doc, tpl['id'], 20.0)
    assert res['ok'] is False
    assert '30' in res['error']


def test_fence_and_checkpoint_accept_glat_glon():
    ring = [
        {'glat': 22.4075, 'glon': 112.9380},
        {'glat': 22.4075, 'glon': 112.9455},
        {'glat': 22.4165, 'glon': 112.9455},
        {'glat': 22.4165, 'glon': 112.9380},
    ]
    rings = RM._normalize_fence([ring])
    assert len(rings) == 1
    assert len(rings[0]) == 4
    cp = {'glat': 22.4100, 'glon': 112.9400, 'isMustPoint': False}
    assert RM._clean_checkpoints([cp]) == [
        {'glat': 22.4100, 'glon': 112.9400, 'isMustPoint': False,
         'lat': 22.4100, 'lon': 112.9400}
    ]


def test_normalize_fence_accepts_single_ring_and_dict_payload():
    ring_tuples = [(22.4075, 112.9380), (22.4075, 112.9455),
                   (22.4165, 112.9455), (22.4165, 112.9380)]
    ring_dicts = [{'glat': la, 'glon': ln} for la, ln in ring_tuples]
    for fence in (ring_tuples, ring_dicts, {'polygon': ring_dicts}):
        rings = RM._normalize_fence(fence)
        assert len(rings) == 1, fence
        assert len(rings[0]) == 4
    # A single coordinate is not a ring.
    assert RM._normalize_fence({'glat': 22.4075, 'glon': 112.9380}) == []

    # A single far-away ring must reject a route inside the campus.
    tracks = [
        _track_dict(_make_rect(per_edge=10, laps=2), rrid=f'fence{i}')
        for i in range(5)
    ]
    doc = RM.learn_from_tracks(tracks, owner='u:fence')
    far = [(23.0, 113.0), (23.0, 113.01), (23.01, 113.01), (23.01, 113.0)]
    assert RM.suggest(doc, 2.8, [], fence=far) == []
    near = [(22.40, 112.93), (22.40, 112.95), (22.42, 112.95), (22.42, 112.93)]
    assert RM.suggest(doc, 2.8, [], fence=near)


def test_suggest_skips_low_sample_templates():
    strong = [
        _track_dict(_make_rect(per_edge=10, laps=2), rrid=f's{i}')
        for i in range(5)
    ]
    doc = RM.learn_from_tracks(strong, owner='u:low')
    weak_tpl = json.loads(json.dumps(doc['templates'][0]))
    weak_tpl['id'] = 'lr_weaksample'
    weak_tpl['samples'] = 1
    weak_tpl['line'] = [[22.4100, 112.9400], [22.4100, 112.9406],
                        [22.4106, 112.9406], [22.4106, 112.9400]]
    weak_tpl['closed'] = True
    weak_tpl['lap_m'] = 400.0
    weak_tpl['medoid_distance_m'] = 1.0
    doc['templates'].insert(0, weak_tpl)
    assert RM.summarize(doc)['status'] == 'ok'
    cands = RM.suggest(doc, 2.8, [{'lat': 22.4100, 'lon': 112.9406}])
    assert cands
    assert all(c['samples'] >= 3 for c in cands)
    assert 'lr_weaksample' not in [c['template_id'] for c in cands]


def test_malformed_inputs_do_not_raise():
    assert RM.apply_template(['bad'], 'lr_x', 2.5) == {
        'ok': False, 'error': '模板不存在'}
    assert RM.suggest(['bad'], 2.5, []) == []
    doc = RM.learn_from_tracks([], owner='u:bad')
    assert RM.suggest(doc, 2.5, 123) == []
    bad = {
        'rrid': 'bad', 'line': 123, 'distance_m': 1000.0,
        'duration_s': float('inf'), 'start_ms': float('inf'),
        'is_cheat': 0, 'complete': True, 'source': 'server',
    }
    cleaned = RM.learn_from_tracks([bad], owner='u:bad')
    assert cleaned['records_readable'] == 0
    assert cleaned['templates'] == []
    assert RM._normalize_fence(123) == []
    assert RM._clean_checkpoints(123) == []


def test_open_route_single_pass_distance_fit():
    now_ms = int(time.time() * 1000)
    tracks = []
    for i in range(5):
        raw = _make_out_and_back(
            dlat=0.0235, steps=100, noise_m=0.0, seed=800 + i)
        tracks.append(
            _track_dict(raw, rrid=f'open{i}',
                        distance_m=T.polyline_length_m(raw),
                        duration_s=900, start_ms=now_ms - i * 3600_000)
        )
    doc = RM.learn_from_tracks(tracks, owner='u:open')
    assert doc['templates'][0]['closed'] is False
    cands = RM.suggest(doc, 2.5, [])
    assert cands
    reasons = ''.join(cands[0]['reasons'])
    assert '单程' in reasons
    assert '折返' not in reasons


def _make_circle(
    laps: int = 1,
    per_lap: int = 40,
    radius_m: float = 200.0 / (2 * math.pi),
    noise_m: float = 2.0,
    seed: int = 0,
) -> list[tuple[float, float]]:
    rng = random.Random(seed)
    lat0, lng0 = 22.4100, 112.9400
    lat_scale = 1.0 / 110540.0
    lng_scale = 1.0 / (111320.0 * math.cos(math.radians(lat0)))
    pts: list[tuple[float, float]] = []
    for _ in range(laps):
        for i in range(per_lap):
            a = 2 * math.pi * i / per_lap
            pts.append((
                lat0 + radius_m * math.cos(a) * lat_scale
                + rng.gauss(0.0, noise_m) * lat_scale,
                lng0 + radius_m * math.sin(a) * lng_scale
                + rng.gauss(0.0, noise_m) * lng_scale,
            ))
    pts.append(pts[0])
    return pts


def test_200m_loop_extracts_one_lap_for_1_to_6_laps():
    for laps in (1, 2, 3, 4, 6):
        simplified = T.simplify(_make_circle(laps=laps, seed=500 + laps), 2.5, 400)
        single = RM._extract_single_lap(simplified)
        assert single is not None, laps
        lap_m = T.polyline_length_m(RM._close_ring(single))
        # 200 m true loop; noise at 5 m spacing inflates the measured length.
        assert 190.0 <= lap_m <= 245.0, (laps, lap_m)


def test_learn_200m_loop_tracks_are_one_template():
    now_ms = int(time.time() * 1000)
    tracks = []
    for i in range(5):
        raw = _make_circle(laps=3, seed=900 + i)
        tracks.append(
            _track_dict(raw, rrid=f'c200_{i}',
                        distance_m=T.polyline_length_m(raw) * 3.0,
                        duration_s=450, start_ms=now_ms - i * 3600_000)
        )
    doc = RM.learn_from_tracks(tracks, owner='200:loop')
    assert doc['records_readable'] == 5
    assert len(doc['templates']) == 1
    tpl = doc['templates'][0]
    assert tpl['closed'] is True
    assert tpl['samples'] == 5
    assert 180.0 <= tpl['lap_m'] <= 250.0
    assert RM.summarize(doc)['status'] == 'ok'

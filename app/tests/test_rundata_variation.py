"""The generated track must look like a run, not a metronome."""
import json
import math
import random
import statistics

from replica.domain.rundata import generate_run_data, new_run_uuid, validate_run_data

WAYPOINTS = [(22.4108, 112.9408), (22.4118, 112.9418), (22.4100, 112.9410)]

#: Six waypoints in a small campus box.  This seed used to crowd the last two
#: samples until the 2 s floor pushed one past stopTime, making the final gap
#: negative (`timestamps-not-monotonic` in validate_run_data).
CROWDED_WAYPOINTS = [
    (22.40504173, 112.93276581),
    (22.40382004, 112.93371518),
    (22.40602909, 112.93467491),
    (22.40468653, 112.93471078),
    (22.40341647, 112.93475016),
    (22.40506008, 112.93332831),
]


def _run(seed=7, **kw):
    return generate_run_data(4.0, 6.0, 172, waypoints=WAYPOINTS,
                             closed=True, rng=random.Random(seed), **kw)


def test_run_is_still_valid():
    assert validate_run_data(_run()) == []


def test_sampling_gaps_are_not_exactly_five_seconds():
    run = _run()
    pts = run['allLocJson']
    gaps = [(b['ts'] - a['ts']) / 1000 for a, b in zip(pts, pts[1:])]
    assert min(gaps) >= 3.5, min(gaps)
    assert max(gaps) <= 7.0, max(gaps)
    assert len(set(round(g, 1) for g in gaps)) > 5


def test_per_point_speed_actually_varies():
    run = _run()
    pts = run['locPoints'][1:-1]
    speeds = [p['speed'] for p in pts if p['speed'] > 0]
    assert statistics.pstdev(speeds) > 0.2, speeds[:5]
    # `speed` is PACE in min/km (2.17-10.0 is the school's band), not km/h.
    assert 4.5 <= statistics.median(speeds) <= 7.5, statistics.median(speeds)
    in_band = [s for s in speeds if 2.17 <= s <= 10.0]
    assert len(in_band) >= len(speeds) * 0.85, (min(speeds), max(speeds))
    # The official record satisfies avgSpeed = 16.6667 / speed on every point.
    for p in pts:
        if p['speed'] > 0:
            assert abs(16.6667 / p['speed'] - p['avgSpeed']) <= 0.03, p


def test_track_point_has_the_27_official_fields():
    run = _run()
    p = run['locPoints'][1]
    assert len(p) == 27, sorted(p)
    assert 'steps' not in p
    official = {
        'avgSpeed', 'bdA', 'bdD', 'bdG', 'bdS', 'coorType', 'count', 'dtr',
        'flag', 'gLat', 'gLng', 'gainTime', 'id', 'lat', 'lng', 'locType',
        'locationId', 'queueNum', 'radius', 'speed', 'state', 'stepDistance',
        'totalDis', 'totalTime', 'type', 'validDis', 'validTime',
    }
    assert set(p) == official
    # 7.4.0 MyLocation writes -1/-1; the real GCJ pair is gLat/gLng.
    assert p['lat'] == -1.0 and p['lng'] == -1.0, p
    assert p['gLat'] and p['gLng'], p


def test_queue_id_and_sentinels_match_the_official_track():
    run = _run()
    pts = run['locPoints']
    assert [p['id'] for p in pts] == list(range(1, len(pts) + 1))
    assert pts[0]['type'] == 5 and pts[0]['id'] == 1
    assert pts[-1]['type'] == 6
    assert all(p['locType'] == 0 for p in pts)
    # queueNum advances every 30 s, not every sample.  Points on the boundary
    # still belong to the window that just closed (30 s -> 0, 31 s -> 1).
    for p in pts:
        assert p['queueNum'] == max(0, (p['totalTime'] - 1) // 30)
    assert len({p['queueNum'] for p in pts}) > 1


def test_segment_json_is_the_30s_analysis_entity():
    run = _run()
    seg = run['segment_json']
    assert len(seg) == math.ceil(run['sportTime'] / 30), len(seg)
    assert [s['id'] for s in seg] == list(range(1, len(seg) + 1))
    for s in seg:
        assert set(s) == {
            'avgSpeed', 'avgStep', 'distance', 'endTime', 'flag', 'id',
            'intervalStepModel', 'originAvgStep', 'speedStandard',
            'startTime', 'state',
        }
        assert s['speedStandard'] == 900
        assert s['flag'] == run['locPoints'][0]['flag']
        assert s['endTime'] - s['startTime'] <= 30_000
        if s['distance'] > 0:
            pace = ((s['endTime'] - s['startTime']) / 1000.0 / 60.0) \
                / (s['distance'] / 1000.0)
            assert abs(pace - s['avgSpeed']) < 0.01, (pace, s['avgSpeed'])
    # Every 30 s segment is three 10 s windows; the distances must add up.
    assert abs(sum(s['distance'] for s in seg) - run['juli']) < 0.01


def test_radius_and_satellites_wander_smoothly():
    pts = _run()['locPoints'][1:-1]
    radii = [p['radius'] for p in pts]
    counts = [p['count'] for p in pts]
    # Official records: radius 2.7-18.2 m within a run, satellite count 53-79.
    assert 2.5 <= min(radii) and max(radii) <= 15.0, (min(radii), max(radii))
    assert statistics.pstdev(radii) >= 0.3, statistics.pstdev(radii)
    steps = [abs(b - a) for a, b in zip(radii, radii[1:])]
    assert statistics.median(steps) <= 0.8, statistics.median(steps)
    assert 53 <= min(counts) and max(counts) <= 79, (min(counts), max(counts))
    assert statistics.pstdev(counts) >= 2.0, statistics.pstdev(counts)


def test_geo_fences_json_uses_the_official_fence_shape():
    fence = {'id': 1939, 'name': '学校', 'polygon': [
        (22.4100, 112.9400), (22.4102, 112.9404), (22.4105, 112.9401)]}
    run = generate_run_data(1.0, 6.0, 150, waypoints=WAYPOINTS,
                            closed=True, rng=random.Random(5),
                            geo_fences=[fence])
    raw = run['fivePointJson']['geoFencesJson']
    assert isinstance(raw, str)
    parsed = json.loads(raw)
    assert parsed[0]['id'] == 1939 and parsed[0]['name'] == '学校'
    assert len(parsed[0]['points']) == 3
    assert set(parsed[0]['points'][0]) == {'lat', 'lng', 'glat', 'glng'}
    assert parsed[0]['points'][0]['lat'] == 0.0
    assert parsed[0]['points'][0]['glat'] == 22.41
    # No fence -> null, exactly like PointJsonEntity's @Nullable column.
    assert _run()['fivePointJson']['geoFencesJson'] is None


def test_bearing_tracks_the_route():
    run = _run()
    pts = run['locPoints']
    dirs = [p['bdD'] for p in pts[1:-1]]
    # Every point used to report 0.0 -- due north for the whole record.
    assert len({round(d) for d in dirs}) > 10, dirs[:10]
    assert all(0.0 <= d < 360.0 for d in dirs), dirs[:10]
    assert pts[0]['bdD'] == 0.0, 'the setStartPoint marker must stay all-zero'


def test_totals_stay_exact():
    run = _run()
    last = run['locPoints'][-1]
    assert last['totalDis'] == run['juli'] == 4000
    # Splits are random, but they are rescaled so the sum is still km * pace.
    splits = [s['seconds'] for s in run['km_splits']]
    assert run['sportTime'] == sum(splits) == last['totalTime'] == 1440
    assert len({s for s in splits}) == len(splits)
    # Total steps live on the 10 s windows, not on every GPS sample.
    assert sum(x['stepsNum'] for x in run['step_freq_json']) == run['total_steps']


def test_crowded_tail_keeps_timestamps_increasing():
    run = generate_run_data(3.0791117675740813, 5.593286746278895, 173,
                            waypoints=CROWDED_WAYPOINTS, closed=True,
                            rng=random.Random(1695))
    ts = [p['ts'] for p in run['allLocJson']]
    assert all(b > a for a, b in zip(ts, ts[1:]))
    assert ts[-1] - ts[0] == run['sportTime'] * 1000
    assert validate_run_data(run) == []


def test_tight_window_still_passes_every_waypoint():
    """A naive 20-50 m start offset cut the far must-point out of a 2 km window."""
    wps = [
        (22.4109, 112.9407),
        (22.405248476301683, 112.93376972722123),
        (22.404859229355317, 112.93466748860722),
        (22.403851928519252, 112.934068637221),
    ]
    run = generate_run_data(2.0, 6.0, 172, waypoints=wps, closed=True,
                            rng=random.Random(3))
    pts = run['allLocJson']
    for lat, lng in wps:
        near = min(
            math.hypot((p['lat'] - lat) * 110540,
                       (p['lng'] - lng) * 111320
                       * math.cos(math.radians(lat)))
            for p in pts)
        assert near <= 40.0, (lat, lng, near)
    assert validate_run_data(run) == []


def test_runs_start_at_different_points():
    starts = [_run(seed=seed)['allLocJson'][0] for seed in (1, 3, 7, 11, 13)]
    gaps = []
    for i, a in enumerate(starts):
        for b in starts[i + 1:]:
            gaps.append(math.hypot((a['lat'] - b['lat']) * 110540,
                                   (a['lng'] - b['lng']) * 111320))
    # The start is rotated 20-50 m along the loop, so different runs must not
    # begin on the exact same coordinate; the pair distance itself is random.
    # Two loops can still pass within a metre of each other, so this only
    # guards against coincidence, not against a minimum separation.
    assert min(gaps) > 0.01, gaps
    assert max(gaps) > 10.0, gaps


def test_run_uuid_matches_the_platform():
    android = new_run_uuid('android')
    ios = new_run_uuid('ios')
    assert len(android) == 36 and android == android.lower()
    assert len(ios) == 36 and ios == ios.upper()
    assert android.count('-') == ios.count('-') == 4


def test_run_uuid_is_used_verbatim():
    fixed = 'AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE'
    assert _run(run_uuid=fixed)['sportunid'] == fixed

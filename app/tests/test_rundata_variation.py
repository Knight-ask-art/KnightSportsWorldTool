"""The generated track must look like a run, not a metronome."""
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
    assert min(gaps) >= 2.0, min(gaps)
    assert max(gaps) <= 7.0, max(gaps)
    assert len(set(round(g, 1) for g in gaps)) > 5


def test_per_point_speed_actually_varies():
    run = _run()
    speeds = [p['speed'] for p in run['locPoints'][2:-1]]
    assert statistics.pstdev(speeds) > 0.2, speeds[:5]
    # A stop window legitimately reports ~0 km/h now, so the floor applies to
    # the bulk of the run, not to every single sample.
    in_band = [s for s in speeds if 6.0 * 0.95 <= s <= 16.2]
    assert len(in_band) >= len(speeds) * 0.85, (min(speeds), max(speeds))
    assert max(speeds) <= 16.2


def test_bearing_tracks_the_route():
    run = _run()
    pts = run['locPoints']
    dirs = [p['bdD'] for p in pts[2:-1]]
    # Every point used to report 0.0 -- due north for the whole record.
    assert len({round(d) for d in dirs}) > 10, dirs[:10]
    assert all(0.0 <= d < 360.0 for d in dirs), dirs[:10]
    assert pts[1]['bdD'] == 0.0, 'the setStartPoint marker must stay all-zero'


def test_totals_stay_exact():
    run = _run()
    last = run['locPoints'][-1]
    assert last['totalDis'] == run['juli'] == 4000
    # Splits are random, but they are rescaled so the sum is still km * pace.
    splits = [s['seconds'] for s in run['km_splits']]
    assert run['sportTime'] == sum(splits) == last['totalTime'] == 1440
    assert len({s for s in splits}) == len(splits)
    assert last['steps'] == run['total_steps']


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
    starts = [_run(seed=seed)['allLocJson'][0] for seed in (7, 11, 13)]
    gaps = []
    for i, a in enumerate(starts):
        for b in starts[i + 1:]:
            gaps.append(math.hypot((a['lat'] - b['lat']) * 110540,
                                   (a['lng'] - b['lng']) * 111320))
    # The start is rotated 20-50 m along the loop, so different runs must not
    # begin on the exact same coordinate; the pair distance itself is random.
    assert min(gaps) > 1.0, gaps
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

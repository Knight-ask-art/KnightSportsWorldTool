"""The generated track must look like a run, not a metronome."""
import math
import random
import statistics

from replica.domain.rundata import generate_run_data, new_run_uuid, validate_run_data

WAYPOINTS = [(22.4108, 112.9408), (22.4118, 112.9418), (22.4100, 112.9410)]


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
    assert min(speeds) >= 6.0 * 0.95
    assert max(speeds) <= 16.2


def test_totals_stay_exact():
    run = _run()
    last = run['locPoints'][-1]
    assert last['totalDis'] == run['juli'] == 4000
    assert last['totalTime'] == run['sportTime'] == 1440
    assert last['steps'] == run['total_steps']


def test_runs_start_at_different_points():
    a = _run(seed=7)['allLocJson'][0]
    b = _run(seed=11)['allLocJson'][0]
    gap = math.hypot((a['lat'] - b['lat']) * 110540,
                     (a['lng'] - b['lng']) * 111320)
    assert gap > 5, gap


def test_run_uuid_matches_the_platform():
    android = new_run_uuid('android')
    ios = new_run_uuid('ios')
    assert len(android) == 36 and android == android.lower()
    assert len(ios) == 36 and ios == ios.upper()
    assert android.count('-') == ios.count('-') == 4


def test_run_uuid_is_used_verbatim():
    fixed = 'AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE'
    assert _run(run_uuid=fixed)['sportunid'] == fixed

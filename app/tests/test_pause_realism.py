"""P1 realism profiles: pauses and GPS quality."""

import os
import random
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from replica.domain.rundata import generate_run_data  # noqa: E402

CENTER = (22.4108, 112.9408)


def _run(**kw):
    return generate_run_data(2.0, 6.0, 150, center_lat=CENTER[0],
                             center_lng=CENTER[1], rng=random.Random(11),
                             **kw)


def test_manual_pause_advances_wall_clock_only():
    run = _run(pause_profile='manual',
               pauses=[{'after_s': 120, 'seconds': 30}])
    assert run['pause_total_s'] == 30
    assert run['wall_stop_ms'] - run['locPoints'][0]['flag'] == \
        (run['sportTime'] + 30) * 1000
    first = datetime.strptime(run['locPoints'][0]['gainTime'], '%Y-%m-%d %H:%M:%S')
    last = datetime.strptime(run['locPoints'][-1]['gainTime'], '%Y-%m-%d %H:%M:%S')
    assert int((last - first).total_seconds()) == run['sportTime'] + 30
    assert run['locPoints'][-1]['totalTime'] == run['sportTime']
    assert abs(sum(w['distance'] for w in run['speed_json']) - run['juli']) < 1.0
    assert sum(w['stepsNum'] for w in run['step_freq_json']) == run['total_steps']


def test_auto_pause_default_start_still_ends_in_the_past():
    from replica.domain import safety
    run = _run(pause_profile='auto')
    assert run['pause_total_s'] > 0
    assert run['wall_stop_ms'] <= safety.now_ms()


def test_clean_profile_is_constant():
    run = _run(gps_profile='clean')
    assert {p['count'] for p in run['locPoints']} == {60}
    assert {p['radius'] for p in run['locPoints']} == {5.0}


def test_tunnel_profile_dips_count_and_raises_radius():
    run = _run(gps_profile='tunnel')
    assert min(p['count'] for p in run['locPoints']) <= 8
    assert max(p['radius'] for p in run['locPoints']) >= 20
    assert abs(sum(w['distance'] for w in run['speed_json']) - run['juli']) < 1.0


def test_default_profile_still_wanders():
    run = _run()
    counts = [p['count'] for p in run['locPoints'][1:-1]]
    radii = [p['radius'] for p in run['locPoints'][1:-1]]
    assert len(set(counts)) > 1 and len(set(radii)) > 1

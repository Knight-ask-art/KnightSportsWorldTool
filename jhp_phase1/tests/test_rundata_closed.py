"""Regression: close/open route modes must generate valid, distinct tracks."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from replica.rundata import generate_run_data, validate_run_data


WAYPOINTS = [(22.4108, 112.9408), (22.4118, 112.9418), (22.4100, 112.9410)]


def _gap_m(a, b):
    import math
    dx = (b['lng'] - a['lng']) * 111320 * math.cos(math.radians(a['lat']))
    dy = (b['lat'] - a['lat']) * 110540
    return math.hypot(dx, dy)


def test_closed_route_is_valid():
    run = generate_run_data(2.5, 6.0, 172, waypoints=WAYPOINTS, closed=True)
    assert validate_run_data(run) == []


def test_open_route_is_valid_and_not_forced_closed():
    run = generate_run_data(2.5, 6.0, 172, waypoints=WAYPOINTS, closed=False)
    assert validate_run_data(run) == []
    pts = run['allLocJson']
    assert _gap_m(pts[0], pts[-1]) > 20, 'open route ended back at its start'
    assert max(_gap_m(a, b) for a, b in zip(pts, pts[1:])) <= 100

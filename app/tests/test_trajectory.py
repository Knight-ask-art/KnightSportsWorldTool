"""Tests for trajectory geometry (`replica.domain.trajectory`)."""

from __future__ import annotations

import math
import time

from replica.domain import trajectory as T


def _make_square(
    lat0: float = 22.4100,
    lng0: float = 112.9400,
    dlat: float = 0.0020,
    dlng: float = 0.0020,
    per_edge: int = 10,
    close: bool = True,
) -> list[tuple[float, float]]:
    """Build a clockwise square ring starting at the NW corner (max lat, min lng)."""
    corners = [
        (lat0 + dlat, lng0),
        (lat0 + dlat, lng0 + dlng),
        (lat0, lng0 + dlng),
        (lat0, lng0),
    ]
    pts: list[tuple[float, float]] = []
    for k in range(4):
        a = corners[k]
        b = corners[(k + 1) % 4]
        for s in range(per_edge):
            f = s / per_edge
            pts.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
    if close:
        pts.append(corners[0])
    return pts


def test_haversine_short_latitude_step():
    d = T.haversine_m((22.4100, 112.9400), (22.4110, 112.9400))
    assert 110.0 < d < 112.5
    assert T.haversine_m((22.4100, 112.9400), (22.4100, 112.9400)) == 0.0


def test_polyline_length_straight_line():
    line = [(22.4100, 112.9400), (22.4110, 112.9400), (22.4130, 112.9400)]
    expected = T.haversine_m(line[0], line[-1])
    assert abs(T.polyline_length_m(line) - expected) < 0.05
    assert T.polyline_length_m([]) == 0.0
    assert T.polyline_length_m([line[0]]) == 0.0


def test_simplify_preserves_endpoints_drops_collinear_and_caps_max_points():
    line = [(22.4100 + i * 0.0002, 112.9400) for i in range(10)]
    snap = list(line)
    out = T.simplify(line, tolerance_m=2.5)
    assert line == snap
    assert out == [line[0], line[-1]]

    zigzag = [
        (22.4100 + i * 0.0001, 112.9400 + (0.0002 if i % 2 else 0.0))
        for i in range(50)
    ]
    capped = T.simplify(zigzag, tolerance_m=0.5, max_points=12)
    assert len(capped) <= 12
    assert capped[0] == zigzag[0]
    assert capped[-1] == zigzag[-1]


def test_resample_count_endpoints_and_equal_spacing():
    straight = [(22.4100, 112.9400), (22.4140, 112.9400)]
    out_straight = T.resample(straight, n=41)
    assert len(out_straight) == 41
    assert out_straight[0] == straight[0]
    assert out_straight[-1] == straight[-1]
    segs_s = [T.haversine_m(a, b) for a, b in zip(out_straight, out_straight[1:])]
    mean_s = sum(segs_s) / len(segs_s)
    assert max(abs(s - mean_s) for s in segs_s) < 0.05

    l_shape = [(22.4100, 112.9400), (22.4120, 112.9400), (22.4120, 112.9420)]
    out_l = T.resample(l_shape, n=65)
    assert len(out_l) == 65
    assert out_l[0] == l_shape[0]
    assert out_l[-1] == l_shape[-1]
    segs_l = [T.haversine_m(a, b) for a, b in zip(out_l, out_l[1:])]
    step_l = T.polyline_length_m(l_shape) / 64.0
    # At most one chord cuts across the 90-degree corner (<= step_l); all others match step_l.
    off = [s for s in segs_l if abs(s - step_l) > 0.05]
    assert len(off) <= 1
    assert all(0.0 < s <= step_l + 0.05 for s in segs_l)


def test_is_closed_square_vs_open_line():
    square = _make_square(per_edge=5, close=True)
    assert T.is_closed(square) is True

    open_line = [(22.4100, 112.9400), (22.4110, 112.9405), (22.4120, 112.9410)]
    assert T.is_closed(open_line) is False


def test_extract_loop_two_lap_square():
    lap = _make_square(per_edge=20, close=False)
    two_laps = lap + lap + [lap[0]]
    full_len = T.polyline_length_m(two_laps)

    extracted = T.extract_loop(two_laps, min_lap_m=500.0)
    assert extracted is not None
    assert T.haversine_m(extracted[0], extracted[-1]) > 1e-3

    lap_len = T.polyline_length_m(extracted)
    assert abs(lap_len - 0.5 * full_len) < 40.0

    assert T.extract_loop(two_laps[:15]) is None
    small_ring = _make_square(dlat=0.0005, dlng=0.0005, per_edge=5, close=True)
    assert T.extract_loop(small_ring, min_lap_m=500.0) is None


def test_canonical_loop_rotation_and_clockwise_normalization():
    ring = _make_square(per_edge=5, close=True)
    rotated = ring[7:-1] + ring[:7]
    reversed_ring = list(reversed(rotated))

    c1 = T.canonical_loop(rotated)
    c2 = T.canonical_loop(reversed_ring)

    assert len(c1) == len(c2)
    assert T.haversine_m(c1[0], c1[-1]) > 1e-3

    max_lat = max(p[0] for p in c1)
    assert abs(c1[0][0] - max_lat) < 1e-12
    top_pts = [p for p in c1 if abs(p[0] - max_lat) < 1e-12]
    assert c1[0][1] == min(p[1] for p in top_pts)

    for a, b in zip(c1, c2):
        assert T.haversine_m(a, b) < 1.0

    y0, x0 = c1[0]
    area2 = 0.0
    for i in range(len(c1)):
        yi, xi = c1[i][0] - y0, c1[i][1] - x0
        yj, xj = c1[(i + 1) % len(c1)][0] - y0, c1[(i + 1) % len(c1)][1] - x0
        area2 += xi * yj - xj * yi
    assert area2 <= 0.0


def test_route_distance_closed_and_open():
    ring = _make_square(per_edge=12, close=False)
    shifted_rev = list(reversed(ring[15:] + ring[:15]))
    assert T.route_distance(ring, shifted_rev, closed=True) < 5.0

    other = _make_square(lat0=22.4125, lng0=112.9425, per_edge=12, close=False)
    assert T.route_distance(ring, other, closed=True) > 50.0

    line_a = [(22.4100, 112.9400), (22.4100, 112.9420)]
    line_b = [(22.4100, 112.9400), (22.4100, 112.9420)]
    line_c = [(22.4120, 112.9400), (22.4120, 112.9420)]
    assert T.route_distance(line_a, line_b, closed=False) < 1e-6
    assert T.route_distance(line_a, line_c, closed=False) > 200.0


def test_medoid_index_selects_real_track():
    t0 = _make_square(lat0=22.4100, lng0=112.9400, per_edge=8, close=False)
    t1 = [(lat + 1e-5, lng) for lat, lng in t0]
    outlier = _make_square(lat0=22.4150, lng0=112.9450, per_edge=8, close=False)

    idx = T.medoid_index([t0, t1, outlier], closed=True)
    assert idx in (0, 1)

    idx_pair = T.medoid_index([t0, t1], closed=True)
    assert idx_pair in (0, 1)
    assert T.medoid_index([t0], closed=True) == 0


def test_point_to_polyline_horizontal_segment():
    line = [(22.4100, 112.9400), (22.4100, 112.9420)]
    point = (22.4110, 112.9410)
    d = T.point_to_polyline_m(point, line)
    assert 109.5 < d < 112.0


def test_empty_single_point_nan_and_malformed_safety():
    nan = float('nan')
    valid_pt = (22.4100, 112.9400)
    valid_line = [valid_pt, (22.4110, 112.9400)]

    assert T.haversine_m((nan, 112.94), valid_pt) == float('inf')
    assert T.haversine_m(None, valid_pt) == float('inf')
    assert T.polyline_length_m([None, (0, 0), (nan, 0), valid_pt]) == 0.0
    assert T.simplify([None, (0, 0), (nan, 112.94)]) == []
    assert T.resample([]) == []
    assert T.resample([valid_pt]) == [valid_pt]
    assert T.is_closed([]) is False
    assert T.is_closed([valid_pt]) is False
    assert T.extract_loop([]) is None
    assert T.canonical_loop([]) == []
    assert T.route_distance([], valid_line, closed=False) == float('inf')
    assert T.route_distance([valid_pt], valid_line, closed=True) == float('inf')
    assert T.route_distance([(nan, 0), (0, 0)], valid_line) == float('inf')
    assert T.medoid_index([]) == -1
    assert T.medoid_index([[valid_pt], [(nan, 0)]]) == -1
    assert T.point_to_polyline_m(valid_pt, []) == float('inf')
    assert T.point_to_polyline_m((0, 0), valid_line) == float('inf')
    assert T.point_to_polyline_m((nan, 112.94), valid_line) == float('inf')

    dirty = [None, (0.0, 0.0), (nan, 112.94), valid_pt, ('bad',), (22.4110, 112.9400)]
    dirty_copy = list(dirty)
    assert len(T.simplify(dirty)) == 2
    assert dirty == dirty_copy


def test_medoid_index_40_rings_performance():
    rings = []
    for k in range(40):
        jitter = (k - 20) * 1e-5
        rings.append(
            _make_square(
                lat0=22.4100 + jitter,
                lng0=112.9400 - jitter * 0.5,
                per_edge=32,
                close=False,
            )
        )
    t_start = time.perf_counter()
    idx = T.medoid_index(rings, closed=True)
    elapsed = time.perf_counter() - t_start
    assert 0 <= idx < 40
    assert elapsed < 2.0


def test_pure_python_distance_matches_numpy(monkeypatch):
    ring_a = _make_square(per_edge=8, close=False)
    ring_b = _make_square(lat0=22.4102, lng0=112.9402, per_edge=8, close=False)

    np_closed = T.route_distance(ring_a, ring_b, closed=True)
    np_open = T.route_distance(ring_a, ring_b, closed=False)

    monkeypatch.setattr(T, '_np', None)
    py_closed = T.route_distance(ring_a, ring_b, closed=True)
    py_open = T.route_distance(ring_a, ring_b, closed=False)

    assert abs(np_closed - py_closed) < 1e-6
    assert abs(np_open - py_open) < 1e-6

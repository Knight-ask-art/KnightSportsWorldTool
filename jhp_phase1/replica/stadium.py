"""Standard athletics-track geometry, optionally fitted to real checkpoints.

Two modes:

* `make_stadium_ring(points, perimeter)` — **fit** a standard oval to the live
  campus checkpoints (ported from upstream PR #44 `src/track/stadium.rs`).
  The pose search tries 181 orientations, scores each by the sum of squared
  distances from the checkpoints to the oval centreline, and rejects the fit
  when any checkpoint is further than `radius*0.30 + 3 m` away.

* `stadium_route(center, perimeter, bearing)` — plain geometry around a known
  centre, used when no checkpoints are available.

Both emit **counter-clockwise** rings, which is the direction the IAAF requires
and what campus runners actually do.

Dimensions follow the IAAF 400 m reference (84.39 m straights, 36.80 m radii)
scaled to the requested perimeter:

    P = 2*straight + 2*pi*radius
"""

from __future__ import annotations

import math

IAAF_STRAIGHT_M = 84.39
IAAF_RADIUS_M = 36.80
BASE_PERIMETER_M = 2.0 * IAAF_STRAIGHT_M + 2.0 * math.pi * IAAF_RADIUS_M
M_PER_DEG_LAT = 110540.0
M_PER_DEG_LNG = 111320.0

#: A checkpoint must sit within this distance of the oval, else the preset is
#: rejected rather than silently producing a track that misses it.
TOLERANCE_FACTOR = 0.30
TOLERANCE_SLACK_M = 3.0


def dimensions_for(perimeter_m: float = 400.0) -> tuple[float, float]:
    """(straight_m, radius_m) for a stadium of the requested perimeter."""
    if not (perimeter_m > 0):
        raise ValueError('perimeter must be > 0')
    scale = perimeter_m / BASE_PERIMETER_M
    return IAAF_STRAIGHT_M * scale, IAAF_RADIUS_M * scale


def tolerance_for(radius_m: float) -> float:
    return radius_m * TOLERANCE_FACTOR + TOLERANCE_SLACK_M


# --------------------------------------------------------------------------
# geometry in a local metre plane
# --------------------------------------------------------------------------

def stadium_polyline(straight_m: float, radius_m: float,
                     step_m: float = 0.75) -> list[tuple[float, float]]:
    """Counter-clockwise oval, centred on the origin, sampled every `step_m`."""
    half = straight_m / 2.0
    sc = max(8, int(math.ceil(straight_m / step_m)))
    ac = max(8, int(math.ceil(math.pi * radius_m / step_m)))

    def arc(cx: float, start: float, end: float) -> list[tuple[float, float]]:
        return [(cx + radius_m * math.cos(start + (end - start) * i / ac),
                 radius_m * math.sin(start + (end - start) * i / ac))
                for i in range(ac)]

    pts: list[tuple[float, float]] = []
    pts += [(-half + straight_m * i / sc, -radius_m) for i in range(sc)]
    pts += arc(half, -math.pi / 2, math.pi / 2)
    pts += [(half - straight_m * i / sc, radius_m) for i in range(sc)]
    pts += arc(-half, math.pi / 2, 3 * math.pi / 2)
    return pts


def distance_to_centerline(x: float, y: float, straight_m: float,
                           radius_m: float) -> float:
    """Distance from a point to the oval's centreline (metres)."""
    half = straight_m / 2.0
    if abs(x) <= half:
        return abs(abs(y) - radius_m)
    cx = -half if x < 0 else half
    return abs(math.hypot(x - cx, y) - radius_m)


def rotate(x: float, y: float, angle: float) -> tuple[float, float]:
    c, s = math.cos(angle), math.sin(angle)
    return x * c - y * s, x * s + y * c


def _bbox_center(points) -> tuple[float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0


def fit_pose(points, straight_m: float, radius_m: float,
             step_deg: float = 1.0) -> tuple[float, tuple[float, float], float]:
    """Best (angle, offset, rms_error) for the oval against `points`.

    Mirrors `stadium.rs::fit_pose`: rotate the points by -angle, centre them by
    bounding box, and score the sum of squared distances to the centreline.
    """
    best_angle = 0.0
    best_offset = _bbox_center(points)
    best_err = float('inf')
    n = max(1, int(round(180.0 / step_deg)))
    for i in range(n + 1):
        angle = -math.pi / 2 + i * step_deg * math.pi / 180.0
        rot = [rotate(x, y, -angle) for x, y in points]
        ox, oy = _bbox_center(rot)
        err = 0.0
        for x, y in rot:
            d = distance_to_centerline(x - ox, y - oy, straight_m, radius_m)
            err += d * d
        if err < best_err:
            best_angle, best_offset, best_err = angle, (ox, oy), err
    rms = math.sqrt(best_err / max(1, len(points)))
    return best_angle, best_offset, rms


# --------------------------------------------------------------------------
# public: fit to checkpoints
# --------------------------------------------------------------------------

def make_stadium_ring(points_latlon: list[tuple[float, float]],
                      perimeter_m: float = 400.0) -> dict:
    """Fit an oval to the campus checkpoints.

    `points_latlon` are GCJ-02 `(lat, lng)`.  Raises ValueError when the points
    cannot be explained by a track of this size — a preset that does not match
    the real geometry must fail loudly, not produce a track that misses points.
    """
    if len(points_latlon) < 2:
        raise ValueError('服务器打卡点不足（至少 2 个），无法拟合跑道')
    for lat, lng in points_latlon:
        if not (math.isfinite(lat) and math.isfinite(lng)):
            raise ValueError('打卡点包含非有限坐标')

    straight, radius = dimensions_for(perimeter_m)
    plat = sum(p[0] for p in points_latlon) / len(points_latlon)
    plng = sum(p[1] for p in points_latlon) / len(points_latlon)
    m_lng = M_PER_DEG_LNG * math.cos(math.radians(plat))

    local = [((lng - plng) * m_lng, (lat - plat) * M_PER_DEG_LAT)
             for lat, lng in points_latlon]

    angle, (ox, oy), rms = fit_pose(local, straight, radius)

    deviations = []
    for x, y in local:
        rx, ry = rotate(x, y, -angle)
        deviations.append(distance_to_centerline(rx - ox, ry - oy,
                                                 straight, radius))
    furthest = max(deviations)
    tol = tolerance_for(radius)
    if furthest > tol:
        raise ValueError(
            '打卡点与 %dm 跑道不匹配：最大偏差 %.1fm（允许 %.1fm）'
            % (int(round(perimeter_m)), furthest, tol))

    ring = []
    for bx, by in stadium_polyline(straight, radius):
        rx, ry = rotate(bx + ox, by + oy, angle)
        ring.append((plat + ry / M_PER_DEG_LAT, plng + rx / m_lng))

    return {'ring': ring, 'angle_deg': math.degrees(angle), 'rms_m': rms,
            'max_deviation_m': furthest, 'tolerance_m': tol,
            'straight_m': straight, 'radius_m': radius,
            'perimeter_m': perimeter_m, 'fitted_to_points': True}


# --------------------------------------------------------------------------
# public: plain geometry around a centre
# --------------------------------------------------------------------------

def stadium_route(center_lat: float, center_lng: float,
                  perimeter_m: float = 400.0,
                  bearing_deg: float = 0.0,
                  n_points: int = 180,
                  laps: float = 1.0) -> list[tuple[float, float]]:
    """One lap (or `laps`) around a track centred on `(lat, lng)`.

    No checkpoints are used; `make_stadium_ring` is the fitting variant.
    """
    straight, radius = dimensions_for(perimeter_m)
    base = stadium_polyline(straight, radius)
    total = perimeter_m * max(0.01, laps)
    n = max(8, int(n_points))

    # cumulative arc length for even sampling
    seg = []
    acc = 0.0
    for i in range(len(base)):
        a = base[i]
        b = base[(i + 1) % len(base)]
        acc += math.hypot(b[0] - a[0], b[1] - a[1])
        seg.append(acc)
    if acc <= 0:
        raise ValueError('degenerate stadium polyline')

    b = math.radians(bearing_deg)
    m_lng = M_PER_DEG_LNG * math.cos(math.radians(center_lat))

    def at(s_m: float) -> tuple[float, float]:
        s = s_m % acc
        lo, hi = 0, len(seg) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if seg[mid] < s:
                lo = mid + 1
            else:
                hi = mid
        a = base[lo - 1] if lo > 0 else base[-1]
        a_end = seg[lo - 1] if lo > 0 else 0.0
        bpt = base[lo]
        span = seg[lo] - a_end
        k = 0.0 if span <= 0 else (s - a_end) / span
        x = a[0] + (bpt[0] - a[0]) * k
        y = a[1] + (bpt[1] - a[1]) * k
        rx, ry = rotate(x, y, b)
        return (center_lat + ry / M_PER_DEG_LAT, center_lng + rx / m_lng)

    return [at(total * i / (n - 1)) for i in range(n)]


def perimeter_of(route: list[tuple[float, float]]) -> float:
    """Measured perimeter of a lat/lng polyline (metres)."""
    total = 0.0
    for a, b in zip(route, route[1:]):
        total += math.hypot((b[0] - a[0]) * M_PER_DEG_LAT,
                            (b[1] - a[1]) * M_PER_DEG_LNG
                            * math.cos(math.radians(a[0])))
    return total


# --------------------------------------------------------------------------
# self test
# --------------------------------------------------------------------------

def self_test() -> list[tuple[str, bool, str]]:
    out = []

    straight, radius = dimensions_for(400.0)
    out.append(('400m dimensions',
                abs(straight - 84.39) < 0.01 and abs(radius - 36.80) < 0.01,
                's=%.2f r=%.2f' % (straight, radius)))
    out.append(('P = 2s + 2πr',
                abs(2 * straight + 2 * math.pi * radius - 400.0) < 0.05,
                '%.2f' % (2 * straight + 2 * math.pi * radius)))

    for per in (200.0, 300.0, 400.0):
        route = stadium_route(22.410787, 112.940804, perimeter_m=per,
                              n_points=721, laps=1.0)
        got = perimeter_of(route + [route[0]])
        out.append(('%dm route perimeter' % int(per),
                    abs(got - per) <= per * 0.02, 'measured %.1fm' % got))

    # CCW: the first segment should head +x along the bottom straight
    route = stadium_route(22.41, 112.94, 400.0, n_points=721)
    dx = (route[1][1] - route[0][1])
    dy = (route[1][0] - route[0][0])
    out.append(('starts counter-clockwise', dx > 0 and abs(dy) < abs(dx),
                'dx=%.6f dy=%.6f' % (dx, dy)))

    # ---- fitting round-trip -------------------------------------------
    for angle_deg in (0.0, 37.0, -64.0):
        ring = stadium_route(22.410787, 112.940804, 400.0,
                             bearing_deg=angle_deg, n_points=360)
        cp = [ring[0], ring[90], ring[180], ring[270]]
        fit = make_stadium_ring(cp, 400.0)
        out.append(('fit recovers %+.0f° track' % angle_deg,
                    fit['max_deviation_m'] < 3.0,
                    'dev=%.2fm rms=%.2f' % (fit['max_deviation_m'],
                                            fit['rms_m'])))
        out.append(('  fitted ring length',
                    abs(perimeter_of(fit['ring'] + [fit['ring'][0]]) - 400.0)
                    < 8.0,
                    '%.1fm' % perimeter_of(fit['ring'] + [fit['ring'][0]])))

    # ---- rejection ------------------------------------------------------
    # A 100 m circle is not a 400 m track: every point is ~63 m off the oval.
    circle = stadium_route(22.410787, 112.940804, 628.0, n_points=24)
    rejected = False
    try:
        make_stadium_ring(circle, 400.0)
    except ValueError as e:
        rejected = '不匹配' in str(e)
    out.append(('rejects wrong-size checkpoint set', rejected, 'raised'))

    # Too few points
    few = False
    try:
        make_stadium_ring([(22.41, 112.94)], 400.0)
    except ValueError:
        few = True
    out.append(('rejects <2 checkpoints', few, 'raised'))
    return out


if __name__ == '__main__':
    ok = True
    for name, passed, detail in self_test():
        print('%-34s %s   %s' % (name, 'PASS' if passed else 'FAIL', detail))
        ok = ok and passed
    print('\nRESULT:', 'PASS' if ok else 'FAIL')
    raise SystemExit(0 if ok else 1)

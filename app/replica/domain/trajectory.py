"""Pure trajectory geometry utilities for route memory.

All coordinates are ``(lat, lng)`` pairs in degrees (GCJ-02 internally).
Distances use spherical metres (:func:`haversine_m`) or a local tangent-plane
metre projection ``x = (lng - lng0) * 111320 * cos(lat0)``,
``y = (lat - lat0) * 110540``.
"""

from __future__ import annotations

from bisect import bisect_right
import math
from typing import Sequence

try:
    import numpy as _np
except ImportError:  # pragma: no cover
    _np = None

Point = tuple[float, float]

R_EARTH_M = 6371000.0
M_PER_DEG_LAT = 110540.0
M_PER_DEG_LNG = 111320.0

if _np is not None:
    _IDX_CACHE: dict[int, tuple] = {}
else:  # pragma: no cover
    _IDX_CACHE = {}


def _valid_coord(p, *, reject_zero: bool = True) -> Point | None:
    if p is None or isinstance(p, (str, bytes, dict)):
        return None
    try:
        if len(p) < 2:
            return None
        lat = float(p[0])
        lng = float(p[1])
    except Exception:  # noqa: BLE001
        return None
    if not (math.isfinite(lat) and math.isfinite(lng)):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
        return None
    if reject_zero and abs(lat) < 1e-9 and abs(lng) < 1e-9:
        return None
    return (lat, lng)


def _clean_points(points: Sequence[Point] | None) -> list[Point]:
    if points is None:
        return []
    out: list[Point] = []
    try:
        iterator = iter(points)
    except TypeError:
        return []
    for p in iterator:
        vp = _valid_coord(p, reject_zero=True)
        if vp is not None:
            out.append(vp)
    return out


def _projection_scale(lat0: float) -> tuple[float, float]:
    return (
        M_PER_DEG_LNG * max(1e-6, math.cos(math.radians(lat0))),
        M_PER_DEG_LAT,
    )


def _project_xy(pts: Sequence[Point], lat0: float, lng0: float) -> list[tuple[float, float]]:
    sx, sy = _projection_scale(lat0)
    return [((lng - lng0) * sx, (lat - lat0) * sy) for lat, lng in pts]


def _seg_dist_xy(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> float:
    dx = bx - ax
    dy = by - ay
    den = dx * dx + dy * dy
    if den <= 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / den
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def haversine_m(a: Point, b: Point) -> float:
    """两点球面距离，米。"""
    pa = _valid_coord(a, reject_zero=False)
    pb = _valid_coord(b, reject_zero=False)
    if pa is None or pb is None:
        return float('inf')
    lat1 = math.radians(pa[0])
    lng1 = math.radians(pa[1])
    lat2 = math.radians(pb[0])
    lng2 = math.radians(pb[1])
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    h = (
        math.sin(dlat * 0.5) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlng * 0.5) ** 2
    )
    h = min(1.0, max(0.0, h))
    return 2.0 * R_EARTH_M * math.asin(math.sqrt(h))


def _cumulative_lengths(pts: Sequence[Point]) -> list[float]:
    cum = [0.0]
    total = 0.0
    for a, b in zip(pts, pts[1:]):
        total += haversine_m(a, b)
        cum.append(total)
    return cum


def polyline_length_m(points: Sequence[Point]) -> float:
    """折线累计长度，米。少于 2 点返回 0。"""
    pts = _clean_points(points)
    if len(pts) < 2:
        return 0.0
    return sum(haversine_m(a, b) for a, b in zip(pts, pts[1:]))


def simplify(
    points: Sequence[Point],
    tolerance_m: float = 2.5,
    max_points: int = 400,
) -> list[Point]:
    """Douglas-Peucker；结果不超过 max_points（超过时按步长抽稀）。"""
    pts = _clean_points(points)
    if max_points <= 0:
        return []
    n = len(pts)
    if n <= 2:
        return pts
    if max_points == 1:
        return [pts[0]]

    lat0 = sum(p[0] for p in pts) / n
    lng0 = sum(p[1] for p in pts) / n
    xy = _project_xy(pts, lat0, lng0)

    keep = [False] * n
    keep[0] = True
    keep[n - 1] = True
    stack = [(0, n - 1)]
    tol = max(0.0, float(tolerance_m))

    while stack:
        s, e = stack.pop()
        if e - s <= 1:
            continue
        ax, ay = xy[s]
        bx, by = xy[e]
        best_idx = -1
        best_dist = -1.0
        for i in range(s + 1, e):
            px, py = xy[i]
            d = _seg_dist_xy(px, py, ax, ay, bx, by)
            if d > best_dist:
                best_dist = d
                best_idx = i
        if best_idx != -1 and best_dist > tol:
            keep[best_idx] = True
            stack.append((s, best_idx))
            stack.append((best_idx, e))

    out = [pts[i] for i, flag in enumerate(keep) if flag]
    if len(out) > max_points:
        step = (len(out) - 1) / (max_points - 1)
        indices = [int(round(i * step)) for i in range(max_points - 1)]
        indices.append(len(out) - 1)
        out = [out[i] for i in indices]
    return out


def resample(points: Sequence[Point], n: int = 128) -> list[Point]:
    """按弧长等距重采样；输入少于 2 点原样返回。"""
    pts = _clean_points(points)
    if len(pts) < 2:
        return pts
    count = max(2, int(n))
    cum = _cumulative_lengths(pts)
    total = cum[-1]
    if total <= 0.0:
        return [pts[0]] * count

    out: list[Point] = [pts[0]]
    last_idx = len(pts) - 2
    for k in range(1, count - 1):
        target = total * k / (count - 1)
        i = bisect_right(cum, target) - 1
        if i < 0:
            i = 0
        elif i > last_idx:
            i = last_idx
        seg = cum[i + 1] - cum[i]
        frac = (target - cum[i]) / seg if seg > 0.0 else 0.0
        a = pts[i]
        b = pts[i + 1]
        out.append((a[0] + (b[0] - a[0]) * frac, a[1] + (b[1] - a[1]) * frac))
    out.append(pts[-1])
    return out


def is_closed(
    points: Sequence[Point],
    tolerance_m: float = 30.0,
    ratio: float = 0.03,
) -> bool:
    """首尾距离 <= max(tolerance_m, ratio * length) 视为闭环。"""
    pts = _clean_points(points)
    if len(pts) < 3:
        return False
    length = polyline_length_m(pts)
    if length <= 0.0:
        return False
    return haversine_m(pts[0], pts[-1]) <= max(tolerance_m, ratio * length)


def extract_loop(
    points: Sequence[Point],
    min_lap_m: float = 500.0,
) -> list[Point] | None:
    """从闭环轨迹中提取单圈。

    从累计长度 40% 之后开始，找第一个回到起点 tolerance 内的点；
    该段累计长度 >= min_lap_m 时返回 start..i；否则返回 None。
    输入不是闭环时返回 None。
    """
    pts = _clean_points(points)
    if not is_closed(pts):
        return None
    cum = _cumulative_lengths(pts)
    total = cum[-1]
    if total <= 0.0:
        return None
    scan_after = 0.4 * total
    start = pts[0]
    for i in range(1, len(pts)):
        if cum[i] < scan_after:
            continue
        if haversine_m(start, pts[i]) <= 30.0:
            if cum[i] < min_lap_m:
                return None
            loop = pts[: i + 1]
            while len(loop) > 1 and haversine_m(loop[0], loop[-1]) < 1e-3:
                loop.pop()
            return loop
    return None


def canonical_loop(points: Sequence[Point]) -> list[Point]:
    """闭环规范化：起点旋到纬度最大（并列取经度最小）的点；
    再按有向面积统一为顺时针；返回首尾不重复的 list。"""
    pts = _clean_points(points)
    if not pts:
        return []
    while len(pts) > 1 and haversine_m(pts[0], pts[-1]) < 1e-3:
        pts.pop()
    n = len(pts)
    if n < 3:
        return pts

    y0, x0 = pts[0]
    area2 = 0.0
    for i in range(n):
        yi, xi = pts[i][0] - y0, pts[i][1] - x0
        yj, xj = pts[(i + 1) % n][0] - y0, pts[(i + 1) % n][1] - x0
        area2 += xi * yj - xj * yi
    if area2 > 0.0:
        pts.reverse()

    start_idx = max(range(n), key=lambda i: (pts[i][0], -pts[i][1]))
    return pts[start_idx:] + pts[:start_idx]


def _resample_for_distance(pts: list[Point], closed: bool, n: int = 128) -> list[Point]:
    if closed:
        ring = list(pts)
        if haversine_m(ring[0], ring[-1]) > 1e-3:
            ring.append(ring[0])
        return resample(ring, n + 1)[:-1]
    return resample(pts, n)


def _cyclic_indices(n: int):
    cached = _IDX_CACHE.get(n)
    if cached is not None:
        return cached
    idx = _np.arange(n)
    shifts = _np.arange(n)[:, None]
    fwd = (idx[None, :] + shifts) % n
    rev = (shifts - idx[None, :]) % n
    cached = (idx, fwd, rev)
    _IDX_CACHE[n] = cached
    return cached


def _pair_distance_resampled(
    ra: Sequence[Point], rb: Sequence[Point], closed: bool
) -> float:
    n = len(ra)
    m = len(rb)
    lat0 = (sum(p[0] for p in ra) + sum(p[0] for p in rb)) / (n + m)
    lng0 = (sum(p[1] for p in ra) + sum(p[1] for p in rb)) / (n + m)
    sx, sy = _projection_scale(lat0)

    if _np is not None:
        a_arr = _np.asarray(ra, dtype=_np.float64)
        b_arr = _np.asarray(rb, dtype=_np.float64)
        ax = (a_arr[:, 1] - lng0) * sx
        ay = (a_arr[:, 0] - lat0) * sy
        bx = (b_arr[:, 1] - lng0) * sx
        by = (b_arr[:, 0] - lat0) * sy
        dx = ax[:, None] - bx[None, :]
        dy = ay[:, None] - by[None, :]
        dist_mat = _np.hypot(dx, dy)
        if not closed:
            return float(0.5 * (dist_mat.min(axis=1).mean() + dist_mat.min(axis=0).mean()))
        idx, fwd, rev = _cyclic_indices(n)
        fwd_min = dist_mat[idx[None, :], fwd].mean(axis=1).min()
        rev_min = dist_mat[idx[None, :], rev].mean(axis=1).min()
        return float(min(fwd_min, rev_min))

    ax_list = [(p[1] - lng0) * sx for p in ra]
    ay_list = [(p[0] - lat0) * sy for p in ra]
    bx_list = [(p[1] - lng0) * sx for p in rb]
    by_list = [(p[0] - lat0) * sy for p in rb]
    dist_rows = [
        [math.hypot(ax_list[i] - bx_list[j], ay_list[i] - by_list[j]) for j in range(m)]
        for i in range(n)
    ]
    if not closed:
        a_to_b = sum(min(row) for row in dist_rows) / n
        b_to_a = sum(min(dist_rows[i][j] for i in range(n)) for j in range(m)) / m
        return 0.5 * (a_to_b + b_to_a)
    best = float('inf')
    for s in range(n):
        fwd = sum(dist_rows[i][(i + s) % n] for i in range(n)) / n
        if fwd < best:
            best = fwd
        rev = sum(dist_rows[i][(s - i) % n] for i in range(n)) / n
        if rev < best:
            best = rev
    return best


def route_distance(
    a: Sequence[Point],
    b: Sequence[Point],
    closed: bool = False,
) -> float:
    """两条轨迹的形状距离，米。

    closed=True：重采样 128 点，正反方向 + 所有循环偏移取最小；
    closed=False：重采样 128 点，对称 Chamfer 距离。
    """
    pa = _clean_points(a)
    pb = _clean_points(b)
    if len(pa) < 2 or len(pb) < 2:
        return float('inf')
    ra = _resample_for_distance(pa, closed, 128)
    rb = _resample_for_distance(pb, closed, 128)
    return _pair_distance_resampled(ra, rb, closed)


def medoid_index(tracks: Sequence[Sequence[Point]], closed: bool = False) -> int:
    """返回到其他轨迹平均 route_distance 最小的下标。空输入返回 -1。"""
    if not tracks:
        return -1
    if len(tracks) == 1:
        return 0 if len(_clean_points(tracks[0])) >= 2 else -1

    resampled: list[tuple[int, list[Point]]] = []
    for idx, track in enumerate(tracks):
        pts = _clean_points(track)
        if len(pts) >= 2:
            resampled.append((idx, _resample_for_distance(pts, closed, 128)))

    k = len(resampled)
    if k == 0:
        return -1
    if k == 1:
        return resampled[0][0]

    totals = [0.0] * k
    for i in range(k):
        ri = resampled[i][1]
        for j in range(i + 1, k):
            d = _pair_distance_resampled(ri, resampled[j][1], closed)
            totals[i] += d
            totals[j] += d

    best_pos = min(range(k), key=lambda i: totals[i])
    return resampled[best_pos][0]


def point_to_polyline_m(point: Point, line: Sequence[Point]) -> float:
    """点到折线各段的最小距离，米。"""
    vp = _valid_coord(point, reject_zero=True)
    if vp is None:
        return float('inf')
    pts = _clean_points(line)
    if not pts:
        return float('inf')
    if len(pts) == 1:
        return haversine_m(vp, pts[0])
    xy = _project_xy(pts, vp[0], vp[1])
    return min(
        _seg_dist_xy(0.0, 0.0, ax, ay, bx, by)
        for (ax, ay), (bx, by) in zip(xy, xy[1:])
    )

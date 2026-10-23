"""User-authored routes: load from a file, smooth them, verify the checkpoints.

Why this exists
---------------
The AMap branch could only take two addresses, and — worse — every hand-drawn
or imported route used to return `checkpoints=[]`, so the submitted record had
no real checkpoint ticks and could never be complete.

A route is only usable if it still passes the campus checkpoints.  This module
provides the three pieces that were missing:

* `load_route()` — read a drawing from JSON / GPX / CSV;
* `smooth()` — centripetal Catmull-Rom, so a 10-point sketch is a curve rather
  than a polygon, while still passing **through** every control point;
* `missed_points()` — which checkpoints the route does not actually pass
  (40 m, the same tolerance `isPass` uses).

Coordinate frames
-----------------
Our whole pipeline is GCJ-02.  A drawing made on an AMap tile is GCJ already,
but a GPX from a watch is WGS84 and must be shifted; `load_route` converts GPX
by default and the caller can force a frame for the others.
"""

from __future__ import annotations

import json
import math
import os
import re

from . import coords

#: `isPass` tolerance in the record (`wire.rs::passes_point`).
HIT_RADIUS_M = 40.0


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

def _valid(lat, lng) -> bool:
    try:
        lat = float(lat)
        lng = float(lng)
    except Exception:  # noqa: BLE001
        return False
    return -90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0 and not (
        abs(lat) < 1e-9 and abs(lng) < 1e-9)


def _from_json(data):
    pts = None
    if isinstance(data, list):
        pts = data
    elif isinstance(data, dict):
        if data.get('type') == 'FeatureCollection':
            feats = data.get('features') or []
            if feats:
                data = feats[0]
        if isinstance(data, dict) and data.get('type') == 'Feature':
            data = data.get('geometry') or {}
        if isinstance(data, dict) and data.get('type') == 'LineString':
            # GeoJSON is [lng, lat]
            return [(float(p[1]), float(p[0])) for p in data.get('coordinates') or []]
        for key in ('points', 'route', 'path', 'coordinates'):
            if isinstance(data.get(key), list):
                pts = data[key]
                break
    if not isinstance(pts, list):
        raise ValueError('JSON 里找不到点列表')
    out = []
    for item in pts:
        if isinstance(item, dict):
            lat = item.get('lat', item.get('latitude'))
            lng = item.get('lng', item.get('lon', item.get('longitude')))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            lat, lng = item[0], item[1]
        else:
            raise ValueError('无法解析的点：%r' % (item,))
        if not _valid(lat, lng):
            raise ValueError('非法坐标：%r' % (item,))
        out.append((float(lat), float(lng)))
    return out


def _from_gpx(text: str):
    """GPX trkpt / rtept in WGS84 (that is what a watch writes)."""
    try:
        import xml.etree.ElementTree as ET
    except Exception as e:  # noqa: BLE001
        raise ValueError('GPX 解析器不可用: %s' % e)
    text = re.sub(r'\sxmlns(:\w+)?="[^"]*"', '', text, count=1)
    try:
        root = ET.fromstring(text)
    except Exception as e:  # noqa: BLE001
        raise ValueError('GPX 格式错误: %s' % e)
    out = []
    for el in root.iter():
        tag = el.tag.split('}')[-1]
        if tag in ('trkpt', 'rtept', 'wpt'):
            lat, lng = el.get('lat'), el.get('lon')
            if _valid(lat, lng):
                out.append((float(lat), float(lng)))
    return out


def _from_text(text: str):
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in '#;' or line.lower().startswith('lat'):
            continue
        parts = re.split(r'[,\s;]+', line)
        if len(parts) < 2:
            continue
        if not _valid(parts[0], parts[1]):
            raise ValueError('无法解析的行：%r' % raw)
        out.append((float(parts[0]), float(parts[1])))
    return out


def load_route(path: str, frame: str = 'auto') -> list[tuple[float, float]]:
    """Read a route file. `frame` is 'auto' | 'gcj' | 'wgs84'.

    `auto` treats `.gpx` as WGS84 (a watch export) and everything else as GCJ
    (drawn on an AMap tile).
    """
    ext = os.path.splitext(path)[1].lower()
    with open(path, 'r', encoding='utf-8-sig') as f:
        text = f.read()
    if not text.strip():
        raise ValueError('路线文件是空的')

    if ext == '.gpx':
        pts = _from_gpx(text)
        detected = 'wgs84'
    elif ext == '.json':
        pts = _from_json(json.loads(text))
        detected = 'gcj'
    else:
        pts = _from_text(text)
        detected = 'gcj'

    if len(pts) < 2:
        raise ValueError('路线至少需要 2 个点（当前 %d）' % len(pts))

    src = detected if frame in ('auto', None, '') else frame
    if src == 'wgs84':
        pts = [coords.wgs84_to_gcj02(lat, lng) for lat, lng in pts]
    return pts


# --------------------------------------------------------------------------
# smoothing (centripetal Catmull-Rom)
# --------------------------------------------------------------------------

def _cr(p0, p1, p2, p3, t: float, alpha: float = 0.5):
    def knot(ti, a, b):
        d = math.hypot(b[0] - a[0], b[1] - a[1])
        return ti + (d ** alpha if d > 1e-12 else 1e-6)

    t0 = 0.0
    t1 = knot(t0, p0, p1)
    t2 = knot(t1, p1, p2)
    t3 = knot(t2, p2, p3)
    tt = t1 + (t2 - t1) * t

    def lerp(a, b, ta, tb):
        if tb - ta <= 1e-12:
            return a
        k = (tt - ta) / (tb - ta)
        return (a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k)

    a1 = lerp(p0, p1, t0, t1)
    a2 = lerp(p1, p2, t1, t2)
    a3 = lerp(p2, p3, t2, t3)
    b1 = lerp(a1, a2, t0, t2)
    b2 = lerp(a2, a3, t1, t3)
    return lerp(b1, b2, t1, t2)


def smooth(points: list[tuple[float, float]], samples: int = 10,
           closed: bool = False) -> list[tuple[float, float]]:
    """Centripetal Catmull-Rom through every control point.

    The curve passes through the inputs, so checkpoint hits are preserved; it
    only rounds the corners between them.
    """
    pts = [tuple(map(float, p)) for p in points]
    if len(pts) < 3 or samples < 1:
        return pts
    if closed:
        ext = [pts[-2], pts[-1]] + pts + [pts[0], pts[1]]
        rng = range(len(pts))
    else:
        ext = [pts[0]] + pts + [pts[-1]]
        rng = range(len(pts) - 1)
    ext = list(ext)
    out = []
    for i in rng:
        p0, p1, p2, p3 = ext[i], ext[i + 1], ext[i + 2], ext[i + 3]
        for k in range(samples):
            out.append(_cr(p0, p1, p2, p3, k / samples))
    out.append(pts[-1] if not closed else pts[0])
    return out


# --------------------------------------------------------------------------
# checkpoint coverage
# --------------------------------------------------------------------------

def _seg_dist_m(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    den = dx * dx + dy * dy
    t = 0.0 if den <= 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / den))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def missed_points(polyline: list[tuple[float, float]],
                  checkpoints: list[dict],
                  radius_m: float = HIT_RADIUS_M) -> list[dict]:
    """Checkpoints the route does not pass within `radius_m`.

    Measures the distance to the **segments**, not just the vertices: a sparse
    drawing would otherwise look like it misses everything between samples.
    """
    if not polyline or not checkpoints:
        return []
    lat0 = sum(p[0] for p in polyline) / len(polyline)
    m_lat = 110540.0
    m_lng = 111320.0 * max(0.2, math.cos(math.radians(lat0)))
    poly = [((lat - lat0) * m_lat, (lng - 0.0) * m_lng) for lat, lng in polyline]

    out = []
    for cp in checkpoints:
        lat = float(cp['lat'])
        lng = float(cp['lon'] if 'lon' in cp else cp['lng'])
        p = ((lat - lat0) * m_lat, (lng - 0.0) * m_lng)
        best = min(_seg_dist_m(p[0], p[1], a[0], a[1], b[0], b[1])
                   for a, b in zip(poly, poly[1:])) if len(poly) > 1 else 1e9
        if best > radius_m:
            out.append({'lat': lat, 'lon': lng, 'distance_m': round(best, 1),
                        'isMustPoint': bool(cp.get('isMustPoint')),
                        'name': cp.get('name') or ''})
    return out


def snap_through(polyline: list[tuple[float, float]],
                 points: list[dict]) -> list[tuple[float, float]]:
    """Insert checkpoints into the route at their nearest segment.

    Accepts the detour: a route that does not pass a checkpoint cannot produce
    a complete record, so pulling it through is the lesser evil.
    """
    out = list(polyline)
    for cp in points:
        lat = float(cp['lat'])
        lng = float(cp['lon'] if 'lon' in cp else cp['lng'])
        target = (lat, lng)
        lat0 = sum(p[0] for p in out) / max(1, len(out))
        m_lat = 110540.0
        m_lng = 111320.0 * max(0.2, math.cos(math.radians(lat0)))
        tp = ((lat - lat0) * m_lat, (lng - 0.0) * m_lng)
        best_i, best_d = 0, float('inf')
        for i, (a, b) in enumerate(zip(out, out[1:])):
            ap = ((a[0] - lat0) * m_lat, (a[1] - 0.0) * m_lng)
            bp = ((b[0] - lat0) * m_lat, (b[1] - 0.0) * m_lng)
            d = _seg_dist_m(tp[0], tp[1], ap[0], ap[1], bp[0], bp[1])
            if d < best_d:
                best_i, best_d = i, d
        out.insert(best_i + 1, target)
    return out


# --------------------------------------------------------------------------
# geofence
# --------------------------------------------------------------------------

def point_in_polygon(lat: float, lng: float,
                     ring: list[tuple[float, float]]) -> bool:
    """Ray casting on a (lat, lng) ring.  Fewer than 3 vertices -> always inside.

    A point sitting on the boundary counts as inside: a geofence edge is a legal
    place to be, and ray casting alone answers "outside" for it.
    """
    n = len(ring)
    if n < 3:
        return True
    if distance_to_ring_m(lat, lng, ring) <= 0.3:
        return True
    inside = False
    j = n - 1
    for i in range(n):
        yi, xi = ring[i][0], ring[i][1]
        yj, xj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            den = (yj - yi) if abs(yj - yi) > 1e-18 else 1e-18
            cut = (xj - xi) * (lat - yi) / den + xi
            if lng < cut:
                inside = not inside
        j = i
    return inside


def distance_to_ring_m(lat: float, lng: float,
                       ring: list[tuple[float, float]]) -> float:
    """Shortest distance from a point to the polygon boundary (metres)."""
    if len(ring) < 2:
        return 0.0
    lat0 = sum(p[0] for p in ring) / len(ring)
    m_lat = 110540.0
    m_lng = 111320.0 * max(0.2, math.cos(math.radians(lat0)))
    p = ((lat - lat0) * m_lat, (lng - 0.0) * m_lng)
    closed = list(ring) + [ring[0]]
    best = float('inf')
    for a, b in zip(closed, closed[1:]):
        ap = ((a[0] - lat0) * m_lat, (a[1] - 0.0) * m_lng)
        bp = ((b[0] - lat0) * m_lat, (b[1] - 0.0) * m_lng)
        best = min(best, _seg_dist_m(p[0], p[1], ap[0], ap[1], bp[0], bp[1]))
    return best


def outside_fence(points: list[tuple[float, float]],
                  ring: list[tuple[float, float]],
                  max_report: int = 0) -> dict:
    """Which route points fall outside the campus geofence.

    Returns {'count', 'total', 'points': [{'index','lat','lon','distance_m'}]}.
    `max_report` caps the detail list (0 = all).
    """
    ring = [(float(a), float(b)) for a, b in (ring or [])]
    if len(ring) < 3 or not points:
        return {'count': 0, 'total': len(points or []), 'points': []}
    bad = []
    for i, (lat, lng) in enumerate(points):
        if point_in_polygon(lat, lng, ring):
            continue
        bad.append({'index': i, 'lat': lat, 'lon': lng,
                    'distance_m': round(distance_to_ring_m(lat, lng, ring), 1)})
    found = len(bad)
    if max_report:
        bad = bad[:max_report]
    return {'count': found, 'total': len(points), 'points': bad}


# --------------------------------------------------------------------------
# self test
# --------------------------------------------------------------------------

def self_test() -> list[tuple[str, bool, str]]:
    import tempfile
    out = []

    # ---- JSON shapes ---------------------------------------------------
    tmp = tempfile.mkdtemp(prefix='jhp_route_')
    j1 = os.path.join(tmp, 'a.json')
    with open(j1, 'w', encoding='utf-8') as f:
        json.dump([[22.41, 112.94], [22.42, 112.95]], f)
    out.append(('json list of pairs', len(load_route(j1)) == 2, 'ok'))

    j2 = os.path.join(tmp, 'b.json')
    with open(j2, 'w', encoding='utf-8') as f:
        json.dump({'type': 'LineString',
                   'coordinates': [[112.94, 22.41], [112.95, 22.42]]}, f)
    geo = load_route(j2)
    out.append(('geojson lng/lat order',
                abs(geo[0][0] - 22.41) < 1e-9 and abs(geo[0][1] - 112.94) < 1e-9,
                str(geo[0])))

    # ---- GPX (WGS84 -> GCJ) -------------------------------------------
    gpx = os.path.join(tmp, 'c.gpx')
    with open(gpx, 'w', encoding='utf-8') as f:
        f.write('<?xml version="1.0"?><gpx version="1.1" '
                'xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>'
                '<trkpt lat="22.410787" lon="112.940804"></trkpt>'
                '<trkpt lat="22.411000" lon="112.941000"></trkpt>'
                '</trkseg></trk></gpx>')
    g = load_route(gpx)
    expect = coords.wgs84_to_gcj02(22.410787, 112.940804)
    out.append(('gpx converted WGS84->GCJ',
                abs(g[0][0] - expect[0]) < 1e-9, '%.6f' % g[0][0]))
    out.append(('gpx ignores namespace', len(g) == 2, len(g)))

    # ---- CSV -----------------------------------------------------------
    csv = os.path.join(tmp, 'd.csv')
    with open(csv, 'w', encoding='utf-8') as f:
        f.write('# lat,lng\nlat,lng\n22.41,112.94\n22.411,112.941\n')
    out.append(('csv skips header/comments', len(load_route(csv)) == 2, 'ok'))

    # ---- smoothing passes through control points -----------------------
    ctrl = [(22.4100, 112.9400), (22.4110, 112.9405), (22.4115, 112.9415),
            (22.4105, 112.9420)]
    curve = smooth(ctrl, samples=8)
    out.append(('smooth produces more points', len(curve) > len(ctrl),
                '%d -> %d' % (len(ctrl), len(curve))))
    hits = [min(math.hypot(c[0] - x[0], c[1] - x[1]) for x in curve)
            for c in ctrl]
    out.append(('curve passes through controls',
                all(d < 1e-9 for d in hits), '%.2e' % max(hits)))

    # ---- missed / snap -------------------------------------------------
    ring = [(22.4100, 112.9400), (22.4100, 112.9420),
            (22.4120, 112.9420), (22.4120, 112.9400), (22.4100, 112.9400)]
    cps = [{'lat': 22.4100, 'lon': 112.9410, 'isMustPoint': True, 'name': 'A'},
           {'lat': 22.4110, 'lon': 112.9410, 'isMustPoint': False, 'name': 'B'}]
    miss = missed_points(ring, cps)
    names = sorted(m['name'] for m in miss)
    out.append(('point on edge counts as hit', 'A' not in names, names))
    out.append(('point in the middle is missed', names == ['B'], names))
    out.append(('miss records the distance',
                miss and miss[0]['distance_m'] > 40, miss[0]['distance_m']))

    snapped = snap_through(ring, [cps[1]])
    out.append(('snap inserts the point', len(snapped) == len(ring) + 1,
                '%d -> %d' % (len(ring), len(snapped))))
    out.append(('after snap nothing is missed',
                missed_points(snapped, [cps[1]]) == [], 'ok'))

    # ---- geofence ------------------------------------------------------
    fence = [(22.4100, 112.9400), (22.4100, 112.9420),
             (22.4120, 112.9420), (22.4120, 112.9400)]
    out.append(('point inside fence',
                point_in_polygon(22.4110, 112.9410, fence), 'inside'))
    out.append(('point outside fence',
                not point_in_polygon(22.4150, 112.9410, fence), 'outside'))
    out.append(('point on the top edge counts as inside',
                point_in_polygon(22.4120, 112.9410, fence), 'edge'))
    out.append(('empty ring accepts everything',
                point_in_polygon(22.9, 113.9, []), 'inside'))

    res = outside_fence([(22.4110, 112.9410), (22.4150, 112.9410)],
                        fence)
    out.append(('outside_fence finds one', res['count'] == 1,
                res))
    out.append(('outside reports the excess distance',
                res['points'][0]['distance_m'] > 300,
                res['points'][0]['distance_m']))
    clean = outside_fence([(22.4110, 112.9410)], fence)
    out.append(('inside has no findings', clean['count'] == 0, clean))

    capped = outside_fence([(22.4150, 112.9410), (22.4160, 112.9410),
                            (22.4170, 112.9410)], fence, max_report=2)
    out.append(('max_report caps details but not the count',
                capped['count'] == 3 and len(capped['points']) == 2,
                (capped['count'], len(capped['points']))))
    return out


if __name__ == '__main__':
    ok = True
    for name, passed, detail in self_test():
        print('%-40s %s   %s' % (name, 'PASS' if passed else 'FAIL', detail))
        ok = ok and passed
    print('\nRESULT:', 'PASS' if ok else 'FAIL')
    raise SystemExit(0 if ok else 1)

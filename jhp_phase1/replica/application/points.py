"""Campus live checkpoints — POST api/v560/get/1/distance/<target metres> (sportType=4).

Ported from YanamiNeko/NekoSportsWorldTool `src/api/points.rs`.

Request body
------------
    sportType    4
    longitude    anchor lng
    latitude     anchor lat
    sign         md5("http://<host><path>" + SALT)      # https -> http rewrite
    uuid         random uuid4
    selectedUnid unid (as string)
    runec        envelope(f"{uid}{lon:.6f}{lat:.6f}{(now_ms//1000)*1000}",
                         outer order = observed)
    runAreaId    optional, only when the school configures a run area

Response
--------
    data.pointsResModels[]  -> {lat, lon, isMustPoint, ...}
      lat/lon are BD-09; `isMustPoint` marks the credits-run "必经点位".

Rate limit
----------
The endpoint allows only 3 calls per 5 minutes (error 10603), so every call is
cached for 300 s keyed by uid + unid + runAreaId + anchor(~1e-4 deg).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import uuid

from ..transport import netsec
from ..domain import coords

POINTS_PATH_PREFIX = '/api/v560/get/1/distance/'
# Kept for callers that only need a sentinel path; real requests must use
# points_path(target_distance_m).
POINTS_PATH = POINTS_PATH_PREFIX + '1'
SALT = '2slhe02lsfiwowlcixisla_sls-_slaor'   # same as SKEY5


def points_path(distance_m: float) -> str:
    """Official path ends in the *target distance* in metres.

    Upstream real-device capture (PR #106) uses ``.../distance/2000``.  A
    hard-coded ``/distance/1`` makes the server return only the minimal/fixed
    checkpoint set for a 1 m run, which is why some campuses looked like they
    had only a handful of points.
    """
    try:
        metres = int(round(float(distance_m)))
    except (TypeError, ValueError):
        metres = 2000
    return '%s%d' % (POINTS_PATH_PREFIX, max(1, metres))
CACHE_TTL = 300.0
RATE_LIMIT_ERR = 10603

_DEFAULT_CACHE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data',
    'points_cache.json')
_CACHE_LOCK = threading.RLock()


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------

def _cache_path() -> str:
    return os.environ.get('JHP_POINTS_CACHE', _DEFAULT_CACHE)


def _load_cache() -> dict:
    try:
        with open(_cache_path(), encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    path = _cache_path()
    tmp = None
    try:
        folder = os.path.dirname(path) or '.'
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix='.tmp-', suffix='.json')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        tmp = None
    except Exception:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def cache_key(uid, unid, run_area_id, lat: float, lon: float,
              distance_m: float = 2000.0) -> str:
    """Anchor quantised to ~11 m; the target distance is part of the scope.

    The server returns a different checkpoint set per distance, so a 2 km
    cache entry must never be reused for a 4 km run.
    """
    try:
        dist = max(1, int(round(float(distance_m))))
    except (TypeError, ValueError):
        dist = 2000
    return '%s|%s|%s|%.4f|%.4f|d%d' % (
        uid, unid, run_area_id or '', lat, lon, dist)


def cached_points(uid, unid, run_area_id, lat, lon,
                  ttl: float = CACHE_TTL, distance_m: float = 2000.0):
    """Return cached points if fresh, else None. Expired/future entries miss."""
    with _CACHE_LOCK:
        entry = _load_cache().get(
            cache_key(uid, unid, run_area_id, lat, lon, distance_m))
    if not entry:
        return None
    age = time.time() - float(entry.get('ts', 0))
    if age < 0 or age > ttl:          # reject future and stale entries
        return None
    return entry.get('points')


def store_points(uid, unid, run_area_id, lat, lon, points,
                 distance_m: float = 2000.0) -> None:
    # The bridge is threaded: two refreshes used to interleave read/modify/
    # write and lose one entry (or leave a shared .tmp file).
    with _CACHE_LOCK:
        cache = _load_cache()
        key = cache_key(uid, unid, run_area_id, lat, lon, distance_m)
        cache[key] = {'ts': time.time(), 'points': points}
        # keep the file bounded
        if len(cache) > 40:
            for k in sorted(cache,
                            key=lambda k: cache[k].get('ts', 0))[:len(cache) - 40]:
                cache.pop(k, None)
        _save_cache(cache)


# --------------------------------------------------------------------------
# request
# --------------------------------------------------------------------------

def url_sign(host: str, path: str) -> str:
    """md5 over the http:// variant of the URL plus the shared salt."""
    return hashlib.md5(('http://%s%s%s' % (host, path, SALT)).encode()).hexdigest()


def build_body(acct, lat: float, lon: float, session: netsec.EnvelopeSession,
               anchor_ms: int | None = None,
               run_area_id=None, distance_m: float = 2000.0) -> dict:
    now = int(time.time() * 1000) if anchor_ms is None else anchor_ms
    runec_input = '%s%.6f%.6f%d' % (
        acct.uid, lon, lat, (now // 1000) * 1000)
    runec = netsec.build_envelope(session, runec_input, 'observed')['json']
    body = {
        'sportType': 4,
        'longitude': lon,
        'latitude': lat,
        'sign': url_sign(_host(), points_path(distance_m)),
        'uuid': str(uuid.uuid4()),
        'selectedUnid': str(acct.unid),
        'runec': runec,
    }
    if run_area_id not in (None, '', 0, -1):
        body['runAreaId'] = run_area_id
    return body


def _host() -> str:
    from ..transport.realclient import HOST
    return HOST


# --------------------------------------------------------------------------
# parse
# --------------------------------------------------------------------------

def parse_points(business) -> list[dict]:
    """Extract pointsResModels, accepting both flat and data-nested shapes."""
    if not isinstance(business, dict):
        raise ValueError('points response is not an object')
    err = business.get('error')
    if err not in (None, 10000):
        raise ValueError('points error %s: %s' % (err, business.get('message')))

    container = business
    if 'pointsResModels' not in container:
        d = business.get('data')
        if isinstance(d, dict):
            container = d
        elif isinstance(d, str):
            container = json.loads(d)
    pts = container.get('pointsResModels') if isinstance(container, dict) else None
    if not pts:
        raise ValueError('points response carried no pointsResModels')
    out = []
    for p in pts:
        # The checkpoint endpoint, like the fence endpoint, may fill only the
        # GCJ pair and leave lat/lon at 0 (see src/api/fence.rs point_xy).
        gcj = coords.pair_gcj(p)
        if gcj is None:
            raise ValueError('point missing usable coordinates: %r' % (p,))
        bd = coords.pair_bd(p) or coords.gcj02_to_bd09(*gcj)
        out.append({
            # canonical frame for the track is GCJ
            'lat': gcj[0], 'lon': gcj[1],
            'bd_lat': bd[0], 'bd_lon': bd[1],
            'isMustPoint': bool(p.get('isMustPoint')),
            'name': p.get('pointName') or p.get('name') or '',
            'pid': p.get('id'),
            'isFixed': p.get('isFixed'),
            'position': p.get('position'),
            'raw': p,
        })
    return out


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def fetch_points(client, acct, lat: float, lon: float,
                 run_area_id=None, force: bool = False,
                 ttl: float = CACHE_TTL, log=None,
                 distance_m: float = 2000.0) -> list[dict]:
    """Return the campus checkpoints for the target distance.

    The 300 s cache is scoped by distance: the server may return a different
    checkpoint set for a 2 km and a 4 km run.
    """
    log = log or (lambda *_: None)
    dist = max(1, int(round(float(distance_m))))
    if not force:
        hit = cached_points(acct.uid, acct.unid, run_area_id, lat, lon, ttl,
                            distance_m=dist)
        if hit is not None:
            log('[points] cache hit (%d points, %d m)' % (len(hit), dist))
            return hit

    session = getattr(client, '_ns', None)
    if session is None:
        raise RuntimeError('points require the netsec backend '
                           '(JHP_CRYPTO=netsec)')
    path = points_path(dist)
    body = build_body(acct, lat, lon, session, run_area_id=run_area_id,
                      distance_m=dist)
    res = client.request(acct, path.lstrip('/'), body, retries=2)
    biz = res.get('json')
    if biz is None:
        raise RuntimeError('points request failed: %s (HTTP %s)'
                           % (res.get('error'), res.get('status')))
    if biz.get('error') == RATE_LIMIT_ERR:
        log('[points] 10603 rate limited - reusing cache if any')
        hit = cached_points(acct.uid, acct.unid, run_area_id, lat, lon,
                            ttl=86400, distance_m=dist)
        if hit is not None:
            return hit
        raise RuntimeError('points rate limited and no cached copy available')

    points = parse_points(biz)
    store_points(acct.uid, acct.unid, run_area_id, lat, lon, points,
                 distance_m=dist)
    log('[points] fetched %d points (%d mandatory, %d m)'
        % (len(points), sum(1 for p in points if p['isMustPoint']), dist))
    return points


def mandatory(points: list[dict]) -> list[dict]:
    return [p for p in points if p['isMustPoint']]


# --------------------------------------------------------------------------
# campus geofence
# --------------------------------------------------------------------------

FENCE_PATH = 'api/v1/getGeoFenceForRun'


def fetch_fence(client, acct) -> dict:
    """Campus geofence polygon in GCJ.

    Returns {'polygon': [(lat, lng)], 'center': (lat, lng), 'name': str}.
    The endpoint may fill only `glat/glon` (this campus does); `coords.pair_gcj`
    handles both shapes.
    """
    res = client.request(acct, FENCE_PATH, {}, retries=2)
    biz = res.get('json')
    if biz is None:
        raise RuntimeError('fence request failed: %s (HTTP %s)'
                           % (res.get('error'), res.get('status')))
    if not isinstance(biz, dict):
        raise RuntimeError('fence response is not an object')
    if biz.get('error') != 10000:
        raise RuntimeError('fence error %s: %s'
                           % (biz.get('error'), biz.get('message')))

    data = biz.get('data')
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:  # noqa: BLE001
            data = {}
    data = data if isinstance(data, dict) else {}

    fences = data.get('geoFences') or []
    if not fences:
        raise RuntimeError('围栏为空')
    fence = fences[0] if isinstance(fences[0], dict) else {}
    poly = []
    for p in fence.get('points') or []:
        xy = coords.pair_gcj(p) if isinstance(p, dict) else None
        if xy:
            poly.append(xy)
    if len(poly) < 3:
        raise RuntimeError('围栏点不足（%d）' % len(poly))

    lat = sum(p[0] for p in poly) / len(poly)
    lng = sum(p[1] for p in poly) / len(poly)
    return {'polygon': poly, 'center': (lat, lng),
            'name': fence.get('name') or ''}


def merge_required(required: list[tuple], ordered: list[tuple]) -> list[tuple]:
    """Policy must-points first, then the live route, de-duplicated.

    `policy.rs`'s `route_points(&pol.must_points, &pts)` exists because the
    checkpoint endpoint and the policy can disagree: a required point may be
    absent from `pointsResModels`, and a track that never passes it can never be
    marked complete.
    """
    out: list[tuple] = []
    for p in list(required or []) + list(ordered or []):
        if not any(abs(p[0] - q[0]) < 1e-6 and abs(p[1] - q[1]) < 1e-6
                   for q in out):
            out.append(p)
    return out


def rotate_to(points: list[tuple], first: tuple | None) -> list[tuple]:
    """Rotate a closed loop so the user's first tap is index 0."""
    pts = list(points or [])
    if not pts or not first:
        return pts
    for i, p in enumerate(pts):
        if abs(p[0] - first[0]) < 1e-9 and abs(p[1] - first[1]) < 1e-9:
            return pts[i:] + pts[:i]
    return pts


def route_order(points: list[dict], start_lat: float, start_lng: float) -> list[tuple]:
    """Order points for a run: nearest-neighbour from the start, mandatory first.

    Returns [(lat, lon), ...] in BD-09.  Mandatory points are always kept;
    normal points are appended if they still help form a sane loop.
    """
    import math

    def dist(a, b):
        return math.hypot((b[1] - a[1]) * 111320 * math.cos(math.radians(a[0])),
                          (b[0] - a[0]) * 110540)

    must = [(p['lat'], p['lon']) for p in points if p['isMustPoint']]
    rest = [(p['lat'], p['lon']) for p in points if not p['isMustPoint']]

    ordered = []
    cur = (start_lat, start_lng)
    pool = list(must)
    while pool:
        pool.sort(key=lambda q: dist(cur, q))
        nxt = pool.pop(0)
        ordered.append(nxt)
        cur = nxt
    # then reachable normal points, nearest first
    pool = list(rest)
    while pool:
        pool.sort(key=lambda q: dist(cur, q))
        nxt = pool.pop(0)
        if dist(cur, nxt) < 400:      # skip strays that would distort the loop
            ordered.append(nxt)
            cur = nxt
        else:
            break
    return ordered

# --------------------------------------------------------------------------
# self test
# --------------------------------------------------------------------------

def self_test() -> list[tuple[str, bool, str]]:
    from ..domain import coords
    out = []

    # BD-only payload (the usual case)
    bd = {'pointsResModels': [{'lat': '39.9', 'lon': '116.4',
                               'isMustPoint': True, 'pointName': 'A'}]}
    pts = parse_points(bd)
    gcj = coords.bd09_to_gcj02(39.9, 116.4)
    out.append(('BD payload converted to GCJ',
                abs(pts[0]['lat'] - gcj[0]) < 1e-9
                and abs(pts[0]['bd_lat'] - 39.9) < 1e-9,
                'gcj=%.6f,%.6f' % (pts[0]['lat'], pts[0]['lon'])))
    out.append(('flags preserved', pts[0]['isMustPoint'] and pts[0]['name'] == 'A',
                'ok'))

    # GCJ-only payload (this campus's fence behaves that way)
    gcj_only = {'pointsResModels': [{'lat': 0, 'lon': 0,
                                     'glat': 22.41, 'glon': 112.94}]}
    pts2 = parse_points(gcj_only)
    out.append(('GCJ-only payload accepted',
                abs(pts2[0]['lat'] - 22.41) < 1e-9
                and abs(pts2[0]['lon'] - 112.94) < 1e-9, 'ok'))

    out.append(('empty payload rejected',
                _raises(lambda: parse_points({'pointsResModels': []})), 'error'))
    out.append(('business error rejected',
                _raises(lambda: parse_points({'error': 10603,
                                              'message': 'too many'})), 'error'))

    # merge_required: policy must-points come first, duplicates collapse
    a, b, c = (22.41, 112.94), (22.42, 112.95), (22.43, 112.96)
    merged = merge_required([a], [b, a, c])
    out.append(('merge_required puts policy points first',
                merged == [a, b, c], str(len(merged))))
    out.append(('merge_required handles empty',
                merge_required([], [a]) == [a] and merge_required([a], []) == [a],
                'ok'))
    out.append(('rotate_to keeps the user start',
                rotate_to([a, b, c], b) == [b, c, a], 'ok'))
    out.append(('rotate_to leaves unknown start alone',
                rotate_to([a, b], (0.0, 0.0)) == [a, b], 'ok'))
    out.append(('points path carries target distance',
                points_path(2500).endswith('/distance/2500')
                and points_path(0).endswith('/distance/1'), points_path(2500)))
    out.append(('cache key is distance-scoped',
                cache_key('u', '1', None, a[0], a[1], 2000)
                != cache_key('u', '1', None, a[0], a[1], 3000), 'd2000/d3000'))
    return out


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except Exception:  # noqa: BLE001
        return True


if __name__ == '__main__':
    ok = True
    for name, passed, detail in self_test():
        print('%-34s %s   %s' % (name, 'PASS' if passed else 'FAIL', detail))
        ok = ok and passed
    print('\nRESULT:', 'PASS' if ok else 'FAIL')
    raise SystemExit(0 if ok else 1)

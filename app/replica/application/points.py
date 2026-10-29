"""Campus live checkpoints — POST api/v560/get/1/distance/<target metres> (sportType=4).

Ported from YanamiNeko/NekoSportsWorldTool `src/api/points.rs`.

Request body
------------
    sportType    4
    longitude    anchor lng
    latitude     anchor lat
    sign         md5("http://<host><path>" + SALT)      # https -> http rewrite
    uuid         random uuid4
    selectedUnid unid (int when numeric)
    runec        envelope(f"{uid}{lon:.6f}{lat:.6f}{(now_ms//1000)*1000}",
                         outer order = observed)
    runAreaId    current run area id; always sent, -1 when none

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
import math
import os
import re
import tempfile
import threading
import time
import uuid

from ..build_info import app_root
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
    app_root(), 'data', 'points_cache.json')
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
    points = entry.get('points') or []
    # Old caches were written before `isFixed` was understood: normalise them
    # on read so an upgrade does not need to wait for the 300 s TTL (or a
    # forced refresh) to see the mandatory point and the sequence number.
    for p in points:
        if not isinstance(p, dict):
            continue
        if p.get('seq') is None:
            p['seq'] = point_seq(p)
        if not p.get('isMustPoint') and _truthy(p.get('isFixed')):
            p['isMustPoint'] = True
    return points


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
    try:
        selected_unid = int(acct.unid)
    except (TypeError, ValueError):
        selected_unid = 0
    body = {
        'sportType': 4,
        'longitude': lon,
        'latitude': lat,
        'sign': url_sign(_host(), points_path(distance_m)),
        'uuid': str(uuid.uuid4()),
        'selectedUnid': selected_unid,
        'runec': runec,
    }
    # The official client always puts runAreaId in the body (default -1).
    try:
        body['runAreaId'] = int(run_area_id) if run_area_id is not None else -1
    except (TypeError, ValueError):
        body['runAreaId'] = -1
    return body


def _host() -> str:
    from ..transport.realclient import HOST
    return HOST


# --------------------------------------------------------------------------
# parse
# --------------------------------------------------------------------------

def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'y', '是')
    return bool(value)


def point_seq(point: dict):
    """Sequence number the official client sorts ``position`` by.

    `SportRecord.getFivePoint()` orders the rows by ``position`` and
    `RunSeqLocationManager` consumes ``get(0)``.  The server may return the
    array in a different order (this account's cache: 点位2/3/1/7/4), so fall
    back to the number printed in ``pointName`` when ``position`` is absent.
    No number means the server's array order is kept.
    """
    seq = _int_or_none((point or {}).get('position'))
    if seq is not None:
        return seq
    name = str((point or {}).get('pointName')
               or (point or {}).get('name') or '')
    match = re.search(r'(\d+)\s*$', name) or re.search(r'(\d+)', name)
    if match:
        return int(match.group(1))
    return None


def parse_points(business, allow_empty: bool = False) -> list[dict]:
    """Extract pointsResModels, accepting both flat and data-nested shapes.

    An empty list is **not** treated as a free run by default.  The decompiled
    client has no ``pointsResModels``-empty -> free-run branch on the normal
    ``/distance/`` path; official free run is a separate service type
    (``RunFreePImpl``: serviceType 3 / sportType 4 / empty fivePointJson).
    ``allow_empty`` is only for callers that have explicit operator
    confirmation for a no-checkpoint school.
    """
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
        elif isinstance(d, list):
            container = {'pointsResModels': d}
        elif isinstance(d, str):
            container = json.loads(d)
    pts = container.get('pointsResModels') if isinstance(container, dict) else None
    if isinstance(pts, dict):
        pts = list(pts.values())
    if not pts:
        if allow_empty:
            return []
        raise ValueError('points response carried no pointsResModels')
    out = []
    for p in pts:
        # The checkpoint endpoint, like the fence endpoint, may fill only the
        # GCJ pair and leave lat/lon at 0 (see src/api/fence.rs point_xy).
        gcj = coords.pair_gcj(p)
        if gcj is None:
            raise ValueError('point missing usable coordinates: %r' % (p,))
        bd = coords.pair_bd(p) or coords.gcj02_to_bd09(*gcj)
        # `isFixed` is the official must-point flag
        # (`RunRanLocationManager.passPoint`: isFixed==1 -> type 2).  Some
        # endpoints omit `isMustPoint` entirely; this account's cache has only
        # `isFixed`, so reading `isMustPoint` alone saw zero mandatory points.
        raw_fixed = p.get('isFixed')
        fixed = _int_or_none(raw_fixed)
        if fixed is None and isinstance(raw_fixed, str):
            fixed = 1 if raw_fixed.strip().lower() in (
                '1', 'true', 'yes', 'y', '是') else 0
        raw_must = p.get('isMustPoint')
        must = _truthy(raw_must) or bool(fixed)
        out.append({
            # canonical frame for the track is GCJ
            'lat': gcj[0], 'lon': gcj[1],
            'bd_lat': bd[0], 'bd_lon': bd[1],
            'isMustPoint': must,
            'name': p.get('pointName') or p.get('name') or '',
            'pid': p.get('id'),
            'isFixed': fixed,
            'position': p.get('position'),
            'seq': point_seq(p),
            'raw': p,
        })
    return out


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def fetch_points(client, acct, lat: float, lon: float,
                 run_area_id=None, force: bool = False,
                 ttl: float = CACHE_TTL, log=None,
                 distance_m: float = 2000.0,
                 allow_empty: bool = False) -> list[dict]:
    """Return the campus checkpoints for the target distance.

    The 300 s cache is scoped by distance: the server may return a different
    checkpoint set for a 2 km and a 4 km run.

    ``allow_empty`` is opt-in.  The normal submit path leaves it false so an
    empty ``pointsResModels`` stops the run instead of being guessed as a free
    run.  Preview may pass true to show "no points (unconfirmed)".
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

    points = parse_points(biz, allow_empty=allow_empty)
    # Do not cache an empty answer for 300 s: an empty response may be a
    # transient/distance-specific server result, not proof that the campus has
    # no checkpoints.  Only a non-empty list is safe to reuse.
    if points:
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


class FenceNotConfigured(RuntimeError):
    """The school has no geofence, so there is no polygon to validate.

    This is not a transport failure.  The official client's
    ``LocationGeoFenceHelper.isInArea`` returns ``true`` when the list is
    empty, i.e. a campus without a fence accepts any location.
    """


class PolicyUnavailable(RuntimeError):
    """``runModePolicy`` failed, so the fence source is unknown.

    The official client cannot start a run without the policy reply.  In
    particular it must not substitute the separate ``getGeoFenceForRun``
    result: that object is cached as ``GeoFenceBean2`` and is never read by
    ``LocationGeoFenceHelper.isInArea``.
    """


class FenceAmbiguous(RuntimeError):
    """Several fences exist and no saved/route anchor identifies the campus.

    The official client uses the current GPS position, so it naturally knows
    which polygon the runner is in.  A forged run has no such position; this
    tool must stop instead of silently picking the first polygon.
    """


def _empty_fence(source: str = '', reason: str = '') -> dict:
    return {'id': '', 'name': '', 'polygon': [], 'polygons': [],
            'centers': [], 'center': None, 'ambiguous': False,
            'fences': [], 'source': source, 'reason': reason}


def _fence_polygon(fence: dict) -> list[tuple]:
    poly = []
    raw = fence.get('points') or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:  # noqa: BLE001
            raw = []
    for p in raw:
        xy = coords.pair_gcj(p) if isinstance(p, dict) else None
        if xy:
            poly.append(xy)
    return poly


def _fence_result(parsed: list[dict], source: str = 'policy') -> dict:
    """Return every fence, not one guessed campus polygon.

    ``LocationGeoFenceHelper.getGeoFences()`` turns every matching
    ``GeoFencesBean`` into a ring and ``isInArea`` accepts a point inside any
    of them.  A run area's ``fenceIds`` may legitimately contain several
    fences, so the caller must keep the whole list.
    """
    rings = [f['polygon'] for f in parsed]
    centers = [f['center'] for f in parsed]
    first = parsed[0]
    return {
        'id': first.get('id') or '',
        'name': first.get('name') or '',
        # `polygon` stays for older callers; new validation uses `polygons`.
        'polygon': rings[0],
        'polygons': rings,
        'centers': centers,
        # A single centre is meaningful only for one polygon.  With several
        # campuses the caller must decide from a saved/route anchor.
        'center': centers[0] if len(rings) == 1 else None,
        'ambiguous': len(rings) > 1,
        'fences': parsed,
        'source': source,
        'reason': '',
    }


def _norm_id(value) -> str:
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return str(value or '')


def _parse_fences(raw_fences) -> list[dict]:
    parsed = []
    for f in raw_fences or []:
        if not isinstance(f, dict):
            continue
        poly = _fence_polygon(f)
        if len(poly) < 3:
            continue
        parsed.append({'id': f.get('id') or '',
                       'name': f.get('name') or '',
                       'polygon': poly,
                       'center': (sum(p[0] for p in poly) / len(poly),
                                  sum(p[1] for p in poly) / len(poly))})
    return parsed


def filter_fences_by_area(fences: list[dict],
                          run_area: dict | None) -> list[dict]:
    """Keep only the fences the selected ``RunAreaModels`` owns.

    ``SportTypeViewModel.filterGeoFences`` builds a map of
    ``geoFence.geoFences`` by id and walks ``runAreaModels.fenceIds``.  This is
    the server-provided account/campus mapping; it is stronger than matching
    names or distances.
    """
    if not run_area:
        return fences
    ids = []
    for v in run_area.get('fence_ids') or []:
        try:
            ids.append(str(int(v)))
        except (TypeError, ValueError):
            continue
    # The official filter walks fenceIds and returns an empty list when the
    # area owns none.  An empty list means "no fence for this run area", not
    # "use every other campus's polygon".
    if not ids:
        return []
    wanted = set(ids)
    return [f for f in fences if _norm_id(f.get('id')) in wanted]


def _fence_response_fences(client, acct) -> list[dict]:
    # The official client sends `{"geoFenceUpdateTime": 0}`; an empty body can
    # take a different cache path on some schools and look like "no fence".
    res = client.request(acct, FENCE_PATH, {'geoFenceUpdateTime': 0},
                         retries=2)
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
    if isinstance(data, list):
        raw_fences = data
    else:
        data = data if isinstance(data, dict) else {}
        raw_fences = data.get('geoFences') or []
        if isinstance(raw_fences, str):
            try:
                raw_fences = json.loads(raw_fences)
            except Exception:  # noqa: BLE001
                raw_fences = []
    return raw_fences if isinstance(raw_fences, list) else []


def fetch_fence(client, acct, policy_data: dict | None = None,
                run_area: dict | None = None) -> dict:
    """Every campus geofence ring for this account, in GCJ.

    The only authoritative source is ``runModePolicy``'s ``geoFence``.  The
    official ``LocationGeoFenceHelper`` reads ``CacheDataUtils.getGeoFence()``
    (written by ``RunPolicyViewModel``) and never reads the separate
    ``getGeoFenceForRun`` reply (``GeoFenceBean2``).  Therefore:

    * no policy reply -> ``source='unavailable'`` and no legacy fallback;
    * policy with no/empty fence -> a valid "no fence" result;
    * a run area's ``fenceIds`` filter the list but all matches are kept.

    ``client``/``acct`` remain in the signature for callers that fetch the
    legacy endpoint separately; they are intentionally not used here.
    """
    if policy_data is None:
        return _empty_fence('unavailable', 'runModePolicy 未返回')
    from . import policy as POL
    raw_fences = POL.geo_fences(policy_data)
    if not raw_fences:
        return _empty_fence('policy-empty', 'runModePolicy 没有围栏')

    parsed = _parse_fences(raw_fences)
    if not parsed:
        raise RuntimeError('围栏点不足（服务器返回 %d 个围栏，均无法解析）'
                           % len(raw_fences))
    parsed = filter_fences_by_area(parsed, run_area)
    if not parsed:
        # The selected run area owns no fence in this reply.  The official
        # filter returns an empty list and `isInArea` then returns true, so
        # this is a valid config, not another campus's polygon.
        return _empty_fence('policy-empty', '该跑区没有围栏')
    return _fence_result(parsed, source='policy')


def fetch_legacy_fence(client, acct) -> dict:
    """``getGeoFenceForRun`` for display/diagnostics only.

    Do not pass this result to submit or to the in-area check: the official
    ``isInArea`` path does not read ``GeoFenceBean2``.
    """
    raw_fences = _fence_response_fences(client, acct)
    if not raw_fences:
        return _empty_fence('legacy-empty', 'getGeoFenceForRun 没有围栏')
    parsed = _parse_fences(raw_fences)
    if not parsed:
        raise RuntimeError('围栏点不足（服务器返回 %d 个围栏，均无法解析）'
                           % len(raw_fences))
    return _fence_result(parsed, source='legacy')


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


def ordered(points: list[dict], sequential: bool = False) -> list[dict]:
    """Order checkpoints for a sequential run.

    `SportRecord.getFivePoint()` sorts by ``position`` and
    `RunSeqLocationManager` consumes the first row, so a sequential school
    must visit the points in the school's numbering.  Points without a number
    keep their original relative order at the end.
    """
    pts = list(points)
    if not sequential or not pts:
        return pts
    indexed = list(enumerate(pts))

    def key(item):
        i, p = item
        seq = p.get('seq') if p.get('seq') is not None else point_seq(p)
        return (seq is None, seq if seq is not None else i, i)

    indexed.sort(key=key)
    return [p for _, p in indexed]


def route_order(points: list[dict], start_lat: float, start_lng: float,
                sequential: bool = False) -> list[tuple]:
    """Order points for a run: nearest-neighbour from the start, mandatory first.

    For a sequential school (server ``policy == 1``) the order is the school's
    point number, not the nearest-neighbour path: `RunSeqLocationManager`
    consumes the list from index 0.

    Returns [(lat, lon), ...] in GCJ-02.  Mandatory points are always kept;
    normal points are appended if they still help form a sane loop.
    """
    import math

    if sequential:
        return [(p['lat'], p['lon']) for p in ordered(points, True)]

    def dist(a, b):
        return math.hypot((b[1] - a[1]) * 111320 * math.cos(math.radians(a[0])),
                          (b[0] - a[0]) * 110540)

    must = [(p['lat'], p['lon']) for p in points if p['isMustPoint']]
    rest = [(p['lat'], p['lon']) for p in points if not p['isMustPoint']]

    path = []
    cur = (start_lat, start_lng)
    pool = list(must)
    while pool:
        pool.sort(key=lambda q: dist(cur, q))
        nxt = pool.pop(0)
        path.append(nxt)
        cur = nxt
    # then reachable normal points, nearest first
    pool = list(rest)
    while pool:
        pool.sort(key=lambda q: dist(cur, q))
        nxt = pool.pop(0)
        if dist(cur, nxt) < 400:      # skip strays that would distort the loop
            path.append(nxt)
            cur = nxt
        else:
            break
    return path

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
    out.append(('empty payload allowed for a no-point campus',
                parse_points({'pointsResModels': []}, allow_empty=True) == [],
                'ok'))
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

    # A run area can own several fences; all of them must be kept.
    parsed = _parse_fences([
        {'id': '1', 'name': '东校区',
         'points': [{'glat': 30.0, 'glon': 120.0},
                    {'glat': 30.001, 'glon': 120.0},
                    {'glat': 30.001, 'glon': 120.001}]},
        {'id': '2', 'name': '西校区',
         'points': [{'glat': 31.0, 'glon': 121.0},
                    {'glat': 31.001, 'glon': 121.0},
                    {'glat': 31.001, 'glon': 121.001}]},
    ])
    multi = _fence_result(parsed)
    out.append(('all run-area fences are kept',
                len(multi['polygons']) == 2 and multi['ambiguous'], '2'))
    out.append(('multi-fence has no guessed centre',
                multi['center'] is None, 'None'))
    one = _fence_result([parsed[0]])
    out.append(('single fence keeps its centre',
                one['center'] is not None and not one['ambiguous'], 'ok'))
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

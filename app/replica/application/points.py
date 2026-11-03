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

The reply is **not a stable campus list**: a live probe on 2026-10-30 asked for
1000 / 1060 / 2500 m and each call returned five of at least seven points, with
a different membership and a different ``isFixed`` point.  The distance changes
which points are eligible, and the server draws five per request.  Two
consequences drive the v0.1.7 rules:

* the UI must not re-query for every 10 m of drawn route (it burns the 3/5 min
  budget and the next answer is a different set anyway);
* submit must reuse the set the operator actually saw, otherwise the record
  routes through points that were never displayed.
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
from ..domain import clock
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


def points_distance_for_submit(points_distance_m=None, km=None,
                               default_m: float = 2000.0) -> float:
    """Distance to query when submitting.

    The UI shows a set fetched for ``points_distance_m``.  Re-querying with the
    current ``km`` would return a different random five-point draw (see the
    module docstring), so the displayed set's distance wins; ``km`` is only a
    fallback for callers that never loaded points.
    """
    try:
        dist = float(points_distance_m)
        if dist > 0:
            return dist
    except (TypeError, ValueError):
        pass
    try:
        target = float(km)
        if target > 0:
            return target * 1000.0
    except (TypeError, ValueError):
        pass
    return default_m

CACHE_TTL = 300.0
FALLBACK_TTL = 86400.0          # 10603 may reuse the last known set for a day
RATE_LIMIT_WINDOW = 300.0       # 3 calls / 5 min, error 10603
RATE_LIMIT_ERR = 10603


class PointsRateLimited(RuntimeError):
    """The endpoint is cooling down and no cached set can be served.

    Carries ``retry_after`` seconds so the UI can show a countdown instead of
    re-sending a request that is guaranteed to fail.
    """

    def __init__(self, retry_after: float, message: str = ''):
        self.retry_after = max(1, int(round(float(retry_after or 1))))
        super().__init__(message or (
            '点位接口限流，约 %d 秒后可重试' % self.retry_after))


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


# --------------------------------------------------------------------------
# rate-limit cooldown (persisted, so a restart cannot start a new burst)
# --------------------------------------------------------------------------

def _ratelimit_path() -> str:
    env = os.environ.get('JHP_POINTS_RATELIMIT', '')
    if env:
        return env
    return os.path.join(os.path.dirname(_cache_path()) or '.',
                        'points_ratelimit.json')


def _write_json_atomic(path: str, payload: dict) -> None:
    tmp = None
    try:
        folder = os.path.dirname(path) or '.'
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix='.tmp-', suffix='.json')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
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


def _load_rate_limits() -> dict:
    try:
        with open(_ratelimit_path(), encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _account_key(uid, unid) -> str:
    return '%s|%s' % (uid, '' if unid is None else unid)


def _rate_limit_until(uid=None, unid=None) -> float:
    data = _load_rate_limits()
    accounts = data.get('accounts')
    accounts = accounts if isinstance(accounts, dict) else {}
    if uid is None:
        # Diagnostic view: the newest cooldown anywhere (tests use this).
        values = [float((v or {}).get('until') or 0)
                  for v in accounts.values() if isinstance(v, dict)]
        values.append(float(data.get('until') or 0))
        return max(values or [0.0])
    entry = accounts.get(_account_key(uid, unid))
    if isinstance(entry, dict):
        return float(entry.get('until') or 0)
    # One-off migration: 0.1.7 test builds wrote a single {until}.  Apply it
    # once to whichever account asks next; the first per-account write moves
    # it under that account's key and drops the legacy field.
    return float(data.get('until') or 0)


def rate_limit_remaining(uid=None, unid=None) -> int:
    """Seconds left for this account (0 when clear).

    The cooldown is per ``uid|unid``: a 10603 on campus A must not block
    campus B on the same machine.
    """
    return max(0, int(math.ceil(_rate_limit_until(uid, unid) - time.time())))


def note_rate_limited(uid=None, unid=None, now=None) -> float:
    """Record a 10603 for this account and return the absolute ``until``."""
    current = time.time() if now is None else float(now)
    data = _load_rate_limits()
    raw_accounts = data.get('accounts')
    accounts = dict(raw_accounts) if isinstance(raw_accounts, dict) else {}
    legacy = float(data.get('until') or 0)
    if uid is None:
        until = max(legacy, current + RATE_LIMIT_WINDOW)
        _write_json_atomic(_ratelimit_path(),
                           {'until': until, 'noted_at': current})
        return until
    key = _account_key(uid, unid)
    entry = accounts.get(key)
    previous = (float(entry.get('until') or 0)
                if isinstance(entry, dict) else 0.0)
    until = max(previous, legacy, current + RATE_LIMIT_WINDOW)
    accounts[key] = {'until': until, 'noted_at': current}
    # Drop expired rows so the file cannot grow forever.
    cutoff = current - RATE_LIMIT_WINDOW
    accounts = {k: v for k, v in accounts.items()
                if isinstance(v, dict) and float(v.get('until') or 0) > cutoff}
    _write_json_atomic(_ratelimit_path(), {'accounts': accounts})
    return until


def clear_rate_limit(uid=None, unid=None) -> None:
    """Forget one account's cooldown; no args clears the whole file (tests)."""
    if uid is None:
        try:
            os.unlink(_ratelimit_path())
        except OSError:
            pass
        return
    data = _load_rate_limits()
    raw_accounts = data.get('accounts')
    accounts = dict(raw_accounts) if isinstance(raw_accounts, dict) else {}
    accounts.pop(_account_key(uid, unid), None)
    _write_json_atomic(_ratelimit_path(), {'accounts': accounts})


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


def _normalise_points(points: list) -> list:
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


def _entry_meta(key: str, entry, ttl: float):
    """One cache row -> ``{points, distance_m, ts}``; None when unusable."""
    if not isinstance(entry, dict):
        return None
    age = time.time() - float(entry.get('ts', 0) or 0)
    if age < 0 or age > ttl:          # reject future and stale entries
        return None
    points = entry.get('points') or []
    if not points:
        return None
    parts = str(key).split('|')
    dist = 2000
    if len(parts) > 5 and parts[5].startswith('d'):
        try:
            dist = max(1, int(parts[5][1:]))
        except ValueError:
            pass
    return {'points': _normalise_points(points), 'distance_m': dist,
            'ts': float(entry.get('ts', 0) or 0)}


def cached_entry(uid, unid, run_area_id, lat, lon,
                 ttl: float = CACHE_TTL, distance_m: float = 2000.0):
    """Exact anchor + distance row, or None."""
    key = cache_key(uid, unid, run_area_id, lat, lon, distance_m)
    with _CACHE_LOCK:
        entry = _load_cache().get(key)
    if not entry:
        return None
    return _entry_meta(key, entry, ttl)


def cached_points(uid, unid, run_area_id, lat, lon,
                  ttl: float = CACHE_TTL, distance_m: float = 2000.0):
    """Return cached points if fresh, else None. Expired/future entries miss."""
    hit = cached_entry(uid, unid, run_area_id, lat, lon, ttl, distance_m)
    return hit['points'] if hit else None


def newest_cached_entry(uid, unid, run_area_id, distance_m=None,
                        ttl: float = 86400.0):
    """Newest cache row for the same account/area.

    ``distance_m=None`` searches every distance.  Used only when the server
    rate-limits us: the 10603 reply carries no points, and the last known set
    is still a server-issued five-point set for this campus.  Callers must
    mark the result as stale and tell the operator which distance it came
    from.
    """
    prefix = '%s|%s|%s|' % (uid, unid, run_area_id or '')
    suffix = ''
    if distance_m is not None:
        try:
            dist = max(1, int(round(float(distance_m))))
        except (TypeError, ValueError):
            dist = 2000
        suffix = '|d%d' % dist
    with _CACHE_LOCK:
        entries = [(k, v) for k, v in _load_cache().items()
                   if k.startswith(prefix) and k.endswith(suffix)]
    best = None
    for key, entry in entries:
        meta = _entry_meta(key, entry, ttl)
        if meta is None:
            continue
        if best is None or meta['ts'] > best['ts']:
            best = meta
    return best


def newest_cached_points(uid, unid, run_area_id, distance_m: float = 2000.0,
                         ttl: float = 86400.0, any_distance: bool = False):
    """Points from the newest row (exact distance by default)."""
    hit = newest_cached_entry(
        uid, unid, run_area_id,
        None if any_distance else distance_m, ttl)
    return hit['points'] if hit else None


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
    now = clock.now_ms() if anchor_ms is None else anchor_ms
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

def _cache_meta(hit: dict, dist: int, source: str, rate_limited: bool,
                retry_after: int) -> dict:
    return {
        'points': hit['points'],
        'distance_m': hit['distance_m'],
        'source': source,
        # `stale` means "this set was issued for another target distance".
        'stale': hit['distance_m'] != dist,
        'rate_limited': (bool(rate_limited)
                         or source == 'cache-other-distance'),
        'retry_after_s': max(0, int(retry_after or 0)),
        'ts': hit['ts'],
    }


def fetch_points_meta(client, acct, lat: float, lon: float,
                      run_area_id=None, force: bool = False,
                      ttl: float = CACHE_TTL, log=None,
                      distance_m: float = 2000.0,
                      allow_empty: bool = False) -> dict:
    """Fetch checkpoints and describe where the set came from.

    Resolution order (lowest network cost first):

    1. exact anchor + distance, 300 s;
    2. while cooling down after a 10603: exact distance any anchor, then the
       newest set for this account/area at *any* distance (24 h, flagged
       ``stale``) - no request is sent;
    3. the live endpoint;
    4. on a fresh 10603: same two cache fallbacks, then ``PointsRateLimited``.

    The reply is a random five-of-N draw per request (see the module
    docstring), so a cached set is a valid server-issued set; it is only
    "stale" in the sense that it was ordered for another target distance.
    Callers must surface that to the operator instead of silently pretending
    the server answered for the current distance.
    """
    log = log or (lambda *_: None)
    dist = max(1, int(round(float(distance_m))))
    remaining = rate_limit_remaining(acct.uid, acct.unid)

    if not force:
        hit = cached_entry(acct.uid, acct.unid, run_area_id, lat, lon, ttl,
                           dist)
        if hit is not None:
            log('[points] cache hit (%d points, %d m)'
                % (len(hit['points']), dist))
            return _cache_meta(hit, dist, 'cache', remaining > 0, remaining)

    def _cached_fallback():
        # Same distance, possibly a moved anchor (fence-centre correction).
        hit = cached_entry(acct.uid, acct.unid, run_area_id, lat, lon,
                           FALLBACK_TTL, dist)
        if hit is not None:
            return _cache_meta(hit, dist, 'cache', True, remaining)
        # Last resort: the newest set for this account/area, any distance.
        hit = newest_cached_entry(acct.uid, acct.unid, run_area_id,
                                  None, FALLBACK_TTL)
        if hit is not None:
            same_distance = hit['distance_m'] == dist
            if not same_distance:
                log('[points] %d m request -> reusing %d m cache'
                    % (dist, hit['distance_m']))
            return _cache_meta(hit, dist,
                               'cache' if same_distance
                               else 'cache-other-distance',
                               True, remaining)
        return None

    if remaining > 0:
        # `force` only bypasses the 300 s cache, never the server's cooldown:
        # every extra call extends the ban and the next reply is a different
        # random set anyway.
        hit = _cached_fallback()
        if hit is not None:
            log('[points] cooldown %ds - serving cached points' % remaining)
            return hit
        raise PointsRateLimited(remaining)

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
        until = note_rate_limited(acct.uid, acct.unid)
        left = max(1, int(round(until - time.time())))
        log('[points] 10603 rate limited - reusing cache if any')
        hit = _cached_fallback()
        if hit is not None:
            hit['retry_after_s'] = left
            return hit
        raise PointsRateLimited(left)

    points = parse_points(biz, allow_empty=allow_empty)
    # Do not cache an empty answer for 300 s: an empty response may be a
    # transient/distance-specific server result, not proof that the campus has
    # no checkpoints.  Only a non-empty list is safe to reuse.
    if points:
        store_points(acct.uid, acct.unid, run_area_id, lat, lon, points,
                     distance_m=dist)
    # The server just answered, so the local window is no longer blocking.
    # Without this a forced retry that succeeds would still keep the UI
    # cooling down for the rest of the old 5-minute window.
    clear_rate_limit(acct.uid, acct.unid)
    log('[points] fetched %d points (%d mandatory, %d m)'
        % (len(points), sum(1 for p in points if p['isMustPoint']), dist))
    return {'points': points, 'distance_m': dist, 'source': 'live',
            'stale': False, 'rate_limited': False,
            'retry_after_s': 0, 'ts': time.time()}


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

    Callers that need the provenance (source distance, 10603 fallback) use
    :func:`fetch_points_meta`.
    """
    return fetch_points_meta(
        client, acct, lat, lon, run_area_id=run_area_id, force=force,
        ttl=ttl, log=log, distance_m=distance_m,
        allow_empty=allow_empty)['points']


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

    `runModePolicy`'s ``geoFence`` is the primary source (that is what
    ``LocationGeoFenceHelper`` reads).  Some accounts, however, get a reply
    with ``geoFence: null`` -- a frozen account is one live example (the
    server omits the run configuration) -- while ``getGeoFenceForRun`` still
    returns the school's ring.  In that case fall back to the legacy endpoint
    instead of claiming "no fence": the fence really exists and the official
    app keeps using its cached copy.

    * no policy reply -> ``source='unavailable'`` and no legacy fallback;
    * policy with a fence -> policy wins;
    * policy without a fence -> one legacy attempt (source='legacy-fallback');
    * no fence anywhere -> a valid "no fence" result.

    ``run_area.fenceIds`` filter the policy list.  The legacy reply has no
    run-area association, so it is never filtered by those ids (using them
    would drop the only ring we have).
    """
    if policy_data is None:
        return _empty_fence('unavailable', 'runModePolicy 未返回')
    from . import policy as POL
    raw_fences = POL.geo_fences(policy_data)
    if not raw_fences:
        if POL.has_fence_section(policy_data):
            # Explicit `geoFences: []` (or `{}`) means the school has no
            # fence; the legacy endpoint may hold stale data, so stop here.
            return _empty_fence('policy-empty', 'runModePolicy 没有围栏')
        if client is not None and acct is not None:
            try:
                legacy = fetch_legacy_fence(client, acct)
            except Exception as e:  # noqa: BLE001
                return _empty_fence('legacy-error', '围栏读取失败: %s' % e)
            if legacy.get('polygons'):
                legacy['source'] = 'legacy-fallback'
                legacy['reason'] = ('runModePolicy 没有围栏，'
                                    '已使用 getGeoFenceForRun')
                return legacy
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
    out.append(('submit keeps the displayed distance',
                points_distance_for_submit(1060, 1.06) == 1060.0
                and points_distance_for_submit(None, 1.06) == 1060.0
                and points_distance_for_submit(0, None) == 2000.0,
                '1060 / 1060 / 2000'))

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

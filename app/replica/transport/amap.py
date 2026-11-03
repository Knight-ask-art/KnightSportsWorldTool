"""Custom route via AMap (高德) — geocode + walking directions.

Ported from YanamiNeko/NekoSportsWorldTool `src/amap.rs` + `src/track/custom.rs`.

Coordinates coming back from AMap are GCJ-02, which is exactly the system the
campus geofence uses (`glat`/`glon`), so they can be fed straight into the
route generator as waypoints.

Modes
-----
* single  : A -> B, one leg
* loop    : A -> B -> A, outbound + return synthesised into a natural closed loop
* laps    : repeat the loop until the target distance is covered (0 = auto)

Only the user's own key is used; nothing is sent to the school's servers.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

GEO_URL = 'https://restapi.amap.com/v3/geocode/geo'
RE_GEO_URL = 'https://restapi.amap.com/v3/geocode/regeo'
WALK_URL = 'https://restapi.amap.com/v3/direction/walking'

KEY_ENV = 'JHP_AMAP_KEY'


class AmapError(RuntimeError):
    pass


def _key(key: str | None) -> str:
    k = key or os.environ.get(KEY_ENV, '')
    if not k:
        raise AmapError('AMap key missing: pass --amap-key or set %s' % KEY_ENV)
    return k


def _get(url: str, params: dict, timeout: float = 20.0) -> dict:
    full = url + '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={'User-Agent': 'jhp/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read().decode('utf-8', 'replace'))
    if str(body.get('status')) != '1':
        raise AmapError('AMap %s: %s' % (body.get('infocode'), body.get('info')))
    return body


# --------------------------------------------------------------------------
# geocode
# --------------------------------------------------------------------------

def geocode(address: str, city: str = '', key: str | None = None) -> tuple[float, float]:
    """Address text -> (lng, lat) in GCJ-02."""
    params = {'key': _key(key), 'address': address}
    if city:
        params['city'] = city
    body = _get(GEO_URL, params)
    geos = body.get('geocodes') or []
    if not geos:
        raise AmapError('address not found: %r' % address)
    lng, lat = geos[0]['location'].split(',')
    return float(lng), float(lat)


def regeo(lat: float, lng: float, key: str | None = None,
          radius: int = 1000, timeout: float = 20.0) -> dict:
    """GCJ-02 coordinate -> AMap ``addressComponent``.

    This mirrors the official client's ``MapLogKt.getCity(gLat, gLng)``:
    ``OutdoorRunViewModel.saveStartPointToDb()`` reverse-geocodes the run's
    start point and stores the resulting city in ``SportRecord.address``.
    """
    params = {
        'key': _key(key),
        'location': '%.6f,%.6f' % (float(lng), float(lat)),
        'extensions': 'base',
        'radius': max(1, int(radius)),
    }
    body = _get(RE_GEO_URL, params, timeout=float(timeout))
    return ((body.get('regeocode') or {}).get('addressComponent') or {})


def regeo_city(lat: float, lng: float, key: str | None = None,
               timeout: float = 20.0) -> str:
    """The city name for a GCJ point, matching AMap's ``getCity()``.

    AMap returns ``[]`` for the four municipalities; the official SDK falls
    back to ``province`` there (``北京市``), so do the same.  An empty result
    means "unknown", never a fabricated city.
    """
    comp = regeo(lat, lng, key=key, timeout=timeout)
    city = comp.get('city')
    if isinstance(city, list):
        city = ''
    city = str(city or '').strip()
    if not city:
        prov = comp.get('province')
        if isinstance(prov, list):
            prov = ''
        city = str(prov or '').strip()
    return city


# --------------------------------------------------------------------------
# walking route
# --------------------------------------------------------------------------

def walking(origin: tuple[float, float], dest: tuple[float, float],
            key: str | None = None) -> list[tuple[float, float]]:
    """Walking polyline between two (lng, lat) points -> [(lat, lng), ...]."""
    params = {
        'key': _key(key),
        'origin': '%.6f,%.6f' % origin,
        'destination': '%.6f,%.6f' % dest,
    }
    body = _get(WALK_URL, params)
    paths = (body.get('route') or {}).get('paths') or []
    if not paths:
        raise AmapError('no walking route between %s and %s' % (origin, dest))
    pts: list[tuple[float, float]] = []
    for step in paths[0].get('steps') or []:
        for pair in (step.get('polyline') or '').split(';'):
            if not pair:
                continue
            lng, lat = pair.split(',')
            ll = (float(lat), float(lng))
            if not pts or _far(pts[-1], ll) > 1e-7:
                pts.append(ll)
    if len(pts) < 2:
        raise AmapError('walking route degenerate')
    return pts


def _far(a, b) -> float:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def _dist_m(a, b) -> float:
    import math
    return math.hypot((b[0] - a[0]) * 110540.0,
                      (b[1] - a[1]) * 111320.0
                      * math.cos(math.radians(a[0])))


def route_length_m(points: list[tuple[float, float]]) -> float:
    return sum(_dist_m(a, b) for a, b in zip(points, points[1:]))


def decimate(points: list[tuple[float, float]], keep: int):
    """Reduce a polyline to at most `keep` vertices, preserving the ends."""
    if keep < 2 or len(points) <= keep:
        return list(points)
    step = (len(points) - 1) / (keep - 1)
    idx = sorted({int(round(i * step)) for i in range(keep)})
    idx[0], idx[-1] = 0, len(points) - 1
    return [points[i] for i in idx]


def snap_polyline(points, key: str | None = None, max_legs: int = 24,
                  delay: float = 0.35, log=None) -> dict:
    """Turn a hand-drawn polyline into one that follows real walking paths.

    AMap's walking endpoint only accepts origin+destination, so the drawing is
    split into legs.  A failed leg degrades to a straight segment and is
    reported — a partially snapped route is still usable.

    Returns {'route', 'legs_ok', 'legs_total', 'failed', 'length_m'}.
    """
    import time
    log = log or (lambda *_: None)

    pts = [(float(a), float(b)) for a, b in points]
    clean: list[tuple[float, float]] = []
    for p in pts:
        if not clean or _dist_m(clean[-1], p) >= 5.0:
            clean.append(p)
    if len(clean) < 2:
        raise AmapError('路线至少需要 2 个不同位置的点')
    if len(clean) > max_legs + 1:
        clean = decimate(clean, max_legs + 1)
        log('[snap] 折点过多，简化到 %d 个' % len(clean))

    out = [clean[0]]
    ok = 0
    failed = []
    total = len(clean) - 1
    for i in range(total):
        a, b = clean[i], clean[i + 1]
        try:
            leg = walking((a[1], a[0]), (b[1], b[0]), key)
            ok += 1
        except Exception as e:  # noqa: BLE001
            leg = [a, b]
            failed.append({'leg': i + 1, 'reason': str(e)[:80]})
            log('[snap] 第 %d/%d 段失败：%s' % (i + 1, total, e))
        for p in leg[1:]:
            if _dist_m(out[-1], p) > 0.5:
                out.append(p)
        if delay and i + 1 < total:
            time.sleep(delay)

    return {'route': out, 'legs_ok': ok, 'legs_total': total,
            'failed': failed, 'length_m': round(route_length_m(out), 1)}


# --------------------------------------------------------------------------
# route building
# --------------------------------------------------------------------------

def custom_route(start=None, end=None, start_address: str = '',
                 end_address: str = '', mode: str = 'loop',
                 city: str = '', key: str | None = None) -> list[tuple[float, float]]:
    """Build waypoints for a custom route.

    start/end are (lng, lat) in GCJ-02; if omitted, the address is geocoded.
    mode='single' -> A->B only (no return leg)
    mode='loop'   -> A->B->A (return leg reversed if AMap has no reverse route)

    Returns [(lat, lng), ...] ready for rundata.generate_run_data(waypoints=...).
    """
    if start is None:
        if not start_address:
            raise AmapError('need start coordinates or start address')
        start = geocode(start_address, city, key)
    if end is None:
        if not end_address:
            raise AmapError('need end coordinates or end address')
        end = geocode(end_address, city, key)

    out = walking(start, end, key)
    if mode == 'single':
        return out

    # return leg
    try:
        back = walking(end, start, key)
        if _far(back[-1], out[0]) > 1e-5:
            raise AmapError('return leg does not close the loop')
    except AmapError:
        back = list(reversed(out))          # graceful fallback
    return out + back[1:]


def closed_waypoints(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Ensure the polyline is explicitly closed (first == last)."""
    if not points:
        return points
    if _far(points[0], points[-1]) > 1e-9:
        return list(points) + [points[0]]
    return list(points)


# --------------------------------------------------------------------------
# offline self-check (no network)
# --------------------------------------------------------------------------

def self_test() -> list[tuple[str, bool, str]]:
    out = []

    a = (113.0, 22.0)
    b = (113.01, 22.01)
    loop = closed_waypoints([a, b])
    out.append(('closed_waypoints closes', loop[0] == loop[-1],
                '%d pts' % len(loop)))
    out.append(('already-closed untouched',
                closed_waypoints([a, b, a]) == [a, b, a], 'ok'))

    # ---- decimate / length -------------------------------------------
    line = [(22.0 + i * 1e-5, 113.0) for i in range(50)]
    dec = decimate(line, 5)
    out.append(('decimate keeps the ends',
                len(dec) == 5 and dec[0] == line[0] and dec[-1] == line[-1],
                len(dec)))
    out.append(('decimate no-op when short',
                decimate(line[:3], 5) == line[:3], 'ok'))
    out.append(('length is metres',
                route_length_m([(22.0, 113.0), (22.001, 113.0)]) > 100,
                round(route_length_m([(22.0, 113.0), (22.001, 113.0)]), 1)))

    # ---- snap_polyline with a stubbed walking endpoint ----------------
    orig = globals()['walking']
    try:
        def fake(origin, dest, key=None):
            return [(origin[1], origin[0]),
                    ((origin[1] + dest[1]) / 2, (origin[0] + dest[0]) / 2),
                    (dest[1], dest[0])]

        globals()['walking'] = fake
        res = snap_polyline([(22.0, 113.0), (22.002, 113.0),
                             (22.004, 113.0)], delay=0)
        out.append(('snap walks every leg',
                    res['legs_ok'] == 2 and res['legs_total'] == 2,
                    '%d/%d' % (res['legs_ok'], res['legs_total'])))
        out.append(('snap densifies the route', len(res['route']) >= 5,
                    len(res['route'])))
        out.append(('snap reports length', res['length_m'] > 100,
                    res['length_m']))

        def boom(*a, **k):
            raise AmapError('nope')

        globals()['walking'] = boom
        res2 = snap_polyline([(22.0, 113.0), (22.002, 113.0)], delay=0)
        out.append(('failed leg is reported', len(res2['failed']) == 1,
                    res2['failed']))
        out.append(('failed leg degrades to a straight line',
                    len(res2['route']) == 2, len(res2['route'])))
    finally:
        globals()['walking'] = orig
    return out


if __name__ == '__main__':
    ok = True
    for name, passed, detail in self_test():
        print('%-28s %s   %s' % (name, 'PASS' if passed else 'FAIL', detail))
        ok = ok and passed
    print('\nRESULT:', 'PASS' if ok else 'FAIL')
    raise SystemExit(0 if ok else 1)

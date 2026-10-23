"""BD-09 <-> GCJ-02 conversions, and the "which pair is filled" problem.

Background (all verified against the reference implementation):

* `api/v1/getGeoFenceForRun` returns polygons whose points may carry **only**
  ``glat/glon`` (GCJ) with ``lat/lon`` set to 0.  This campus does exactly that
  (`fixtures/campus_fence_47203.json`, `_note`).
* The checkpoint list (`pointsResModels`) is documented as BD-09, but
  `src/api/fence.rs` `point_xy()` explicitly handles the same "BD zeroed, GCJ
  filled" case for it, so both endpoints can be partial.
* The submit body's five-point payload carries **both** pairs for the same
  physical point: ``lat/lon`` in BD-09 and ``glat/glon`` in GCJ-02
  (`src/track/wire.rs` `five_point_payload`).

So the two pairs must never be equal — if they are, one of them is wrong.

Our internal frame is **GCJ-02**: the campus fence only gives us GCJ, and the
whole track is built around it.
"""

from __future__ import annotations

import math

X_PI = math.pi * 3000.0 / 180.0
A = 6378245.0
EE = 0.00669342162296594323


def out_of_china(lat: float, lng: float) -> bool:
    return not (73.66 < lng < 135.05 and 3.86 < lat < 53.55)


def _transform_lat(x: float, y: float) -> float:
    ret = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y
           + 0.2 * math.sqrt(abs(x)))
    ret += (20.0 * math.sin(6.0 * x * math.pi)
            + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi)
            + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi)
            + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(x: float, y: float) -> float:
    ret = (300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y
           + 0.1 * math.sqrt(abs(x)))
    ret += (20.0 * math.sin(6.0 * x * math.pi)
            + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi)
            + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi)
            + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lat: float, lng: float) -> tuple[float, float]:
    if out_of_china(lat, lng):
        return lat, lng
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((A * (1 - EE)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (A / sqrtmagic * math.cos(radlat) * math.pi)
    return lat + dlat, lng + dlng


def gcj02_to_wgs84(lat: float, lng: float) -> tuple[float, float]:
    if out_of_china(lat, lng):
        return lat, lng
    glat, glng = wgs84_to_gcj02(lat, lng)
    return lat * 2 - glat, lng * 2 - glng


def gcj02_to_bd09(lat: float, lng: float) -> tuple[float, float]:
    """GCJ-02 -> BD-09 (the pair the app puts in ``lat/lon``)."""
    z = math.sqrt(lat * lat + lng * lng) + 0.00002 * math.sin(lat * X_PI)
    theta = math.atan2(lat, lng) + 0.000003 * math.cos(lng * X_PI)
    bd_lng = z * math.cos(theta) + 0.0065
    bd_lat = z * math.sin(theta) + 0.006
    return bd_lat, bd_lng


def bd09_to_gcj02(bd_lat: float, bd_lng: float) -> tuple[float, float]:
    """BD-09 -> GCJ-02 (the same formula as `src/track/wire.rs`)."""
    x = bd_lng - 0.0065
    y = bd_lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * X_PI)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * X_PI)
    return z * math.sin(theta), z * math.cos(theta)


def _num(v):
    try:
        f = float(v)
        return f if f == f else None          # reject NaN
    except Exception:  # noqa: BLE001
        return None


def pair_gcj(p: dict) -> tuple[float, float] | None:
    """Extract one point's GCJ-02 coordinates from a server object.

    Mirrors `fence.rs::point_xy` but returns GCJ instead of BD: prefer the GCJ
    pair, fall back to the BD pair converted.  Both endpoints can be partial.
    """
    glat = _num(p.get('glat'))
    glng = _num(p.get('glon', p.get('glng')))
    if glat and glng:
        return glat, glng
    lat = _num(p.get('lat', p.get('latitude')))
    lng = _num(p.get('lon', p.get('lng', p.get('longitude'))))
    if lat and lng:
        return bd09_to_gcj02(lat, lng)
    return None


def pair_bd(p: dict) -> tuple[float, float] | None:
    """Extract one point's BD-09 coordinates (for the five-point payload)."""
    lat = _num(p.get('lat', p.get('latitude')))
    lng = _num(p.get('lon', p.get('lng', p.get('longitude'))))
    if lat and lng:
        return lat, lng
    g = pair_gcj(p)
    return gcj02_to_bd09(*g) if g else None


# --------------------------------------------------------------------------
# self test
# --------------------------------------------------------------------------

def self_test() -> list[tuple[str, bool, str]]:
    out = []

    # A campus point in GCJ
    glat, glng = 22.410787, 112.940804
    blat, blng = gcj02_to_bd09(glat, glng)
    out.append(('BD differs from GCJ',
                abs(blat - glat) > 0.003 and abs(blng - glng) > 0.003,
                'd=%.4f,%.4f' % (blat - glat, blng - glng)))

    back = bd09_to_gcj02(blat, blng)
    err = max(abs(back[0] - glat), abs(back[1] - glng))
    out.append(('round-trip < 1m', err < 1e-5, '%.7f deg' % err))

    # offset magnitude in metres
    m = math.hypot((blat - glat) * 110540, (blng - glng) * 111320
                   * math.cos(math.radians(glat)))
    out.append(('offset is ~500-1200m', 500 < m < 1200, '%.0f m' % m))

    out.append(('pair_gcj prefers glat/glon',
                pair_gcj({'glat': glat, 'glon': glng, 'lat': 0, 'lon': 0})
                == (glat, glng), 'gcj'))
    out.append(('pair_gcj falls back to BD',
                abs(pair_gcj({'lat': blat, 'lon': blng})[0] - glat) < 1e-5,
                'converted'))
    out.append(('pair_gcj rejects empty', pair_gcj({}) is None, 'None'))
    out.append(('pair_bd from gcj-only',
                abs(pair_bd({'glat': glat, 'glon': glng})[0] - blat) < 1e-5,
                'derived'))

    # aliases used by the two endpoints
    out.append(('glng alias', pair_gcj({'glat': glat, 'glng': glng})
                == (glat, glng), 'ok'))
    out.append(('lng alias', pair_gcj({'lat': blat, 'lng': blng}) is not None,
                'ok'))
    return out


if __name__ == '__main__':
    ok = True
    for name, passed, detail in self_test():
        print('%-32s %s   %s' % (name, 'PASS' if passed else 'FAIL', detail))
        ok = ok and passed
    print('\nRESULT:', 'PASS' if ok else 'FAIL')
    raise SystemExit(0 if ok else 1)

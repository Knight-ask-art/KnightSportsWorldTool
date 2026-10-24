"""OBS trail upload — the object the app's replay actually reads.

Ported from YanamiNeko/NekoSportsWorldTool `src/track/wire.rs` + `src/api/obs.rs`.

Flow
----
    POST /api/obs/temporary/url   {bucketName, objectKey, method, contentType}
      -> data.signedUrl
    PUT  <signedUrl>              (the JSON body below, raw)
    ... twice, with two object keys:
        run_data/{YYYYMMDDHH}/{uuid}.json
        run_data/{rrid//1000000}/{rrid}.json

Object (10 keys, every value base64(gzip(...)))
------------------------------------------------
    rrid, uuid, uid, run_data, fixed_point_json,
    segment_json(""), speed_json, step_freq_json, laps_json, runFaceCheck("")

`run_data` is itself JSON: {"allLocJson": "<json array string>", "useZip": false}
where the array holds 28-key points (gLat/gLng, cumulative totalDis/totalTime/steps).

Without this upload the record exists but the detail page / replay has no trail
and no checkpoints — see upstream issue #85.
"""

from __future__ import annotations

import base64
import gzip
import json
import math
import os
import time

from ..domain import safety
from ..domain import coords

OBS_SIGN_PATH = 'api/obs/temporary/url'
BUCKET = 'iydsj-hbase-hot'

#: Our internal frame is already GCJ-02 (the campus fence only returns
#: glat/glon), so OBS points are emitted as-is.  Set JHP_OBS_CONVERT_BD=1 only
#: if you feed the generator真 BD-09 coordinates.
CONVERT_BD = os.environ.get('JHP_OBS_CONVERT_BD', '0') == '1'


def gz(data: bytes) -> str:
    """base64(gzip(data)) — what every OBS field carries."""
    return base64.b64encode(gzip.compress(data, 6)).decode()


def gz_json(obj) -> str:
    return gz(json.dumps(obj, ensure_ascii=False,
                         separators=(',', ':')).encode())


def gz_str(text: str) -> str:
    return gz(text.encode())


def bd09_to_gcj02(bd_lat: float, bd_lng: float) -> tuple[float, float]:
    """Kept for callers; the implementation lives in `coords`."""
    return coords.bd09_to_gcj02(bd_lat, bd_lng)


# --------------------------------------------------------------------------
# track -> OBS points
# --------------------------------------------------------------------------

def _dist_m(a, b) -> float:
    import math
    return math.hypot((b['lat'] - a['lat']) * 110540,
                      (b['lng'] - a['lng']) * 111320
                      * math.cos(math.radians(a['lat'])))


def _cumulative(run: dict):
    """cumulative distance (m), time (s) and steps for every track point.

    Distance is distributed by TIME, not by measured geometry: the generator
    emits constant-pace runs, and deriving distance from jittered radii made the
    implied pace wobble (378 s/km then 331 s/km inside one 6'00" run), which is
    exactly the kind of artefact a reviewer notices.  Falls back to geometry if
    the timestamps are degenerate.
    """
    locs = run['allLocJson']
    t0 = locs[0]['ts']
    cum_t = [max(0, p['ts'] - t0) / 1000.0 for p in locs]
    span = cum_t[-1]

    if span > 0:
        cum_d = [run['juli'] * t / span for t in cum_t]
    else:
        raw = [0.0]
        for a, b in zip(locs, locs[1:]):
            raw.append(raw[-1] + _dist_m(a, b))
        scale = (run['juli'] / raw[-1]) if raw[-1] > 0 else 0.0
        cum_d = [d * scale for d in raw]

    if span <= 0:
        span = 1.0
    total_steps = int(run.get('total_steps') or 0)
    cum_s = [int(round(total_steps * t / span)) for t in cum_t]
    if cum_s:
        cum_s[-1] = total_steps
    return cum_d, cum_t, cum_s


def conv_point(loc, start_ms, cum_d, cum_t, cum_s, prev) -> dict:
    """One 28-key OBS point."""
    if CONVERT_BD:
        glat, glng = bd09_to_gcj02(loc['lat'], loc['lng'])
    else:
        glat, glng = loc['lat'], loc['lng']     # already GCJ-02

    dt = max(1e-6, cum_t - prev[1])
    dd = max(0.0, cum_d - prev[0])
    speed = dd / dt
    return {
        'avgSpeed': round(speed, 4),
        'bdA': round(float(loc.get('alt', 0.0)), 2),
        'bdD': 0.0,
        'bdG': 0,
        'bdS': round(speed, 4),
        'coorType': 'gcj02',
        'count': 1,
        'dtr': 0.0,
        'flag': start_ms,
        'gLat': round(glat, 7),
        'gLng': round(glng, 7),
        'gainTime': int(loc['ts']),
        'id': int(loc.get('index', 0)),
        'lat': -1.0,
        'lng': -1.0,
        'locType': 1,
        'locationId': '',
        'queueNum': int(loc.get('index', 0)),
        'radius': 5.0,
        'speed': round(speed, 4),
        'state': 0,
        'steps': int(cum_s),
        'stepDistance': 0.0,
        'totalDis': round(cum_d, 4),
        'totalTime': int(round(cum_t)),
        'type': 0,
        'validDis': round(cum_d, 4),
        'validTime': int(round(cum_t)),
    }


# --------------------------------------------------------------------------
# laps (one per full kilometre, last one partial)
# --------------------------------------------------------------------------

def build_laps(run, start_ms) -> list[dict]:
    locs = run['allLocJson']
    if not locs:
        return []
    cum_d, cum_t, cum_s = _cumulative(run)
    total_d = run['juli']
    alt0 = float(locs[0].get('alt', 0.0))
    alts = [float(p.get('alt', 0.0)) for p in locs]

    laps = []
    prev_d = prev_t = prev_steps = 0.0
    prev_gain = 0.0
    gain_total = 0.0
    for i in range(1, len(locs)):
        gain_total += max(0.0, alts[i] - alts[i - 1])

    d_now = 0.0
    while d_now < total_d:
        d_now = min(d_now + 1000.0, total_d)
        # interpolate time/steps/altitude at d_now
        t_now, s_now, alt_now = cum_t[-1], cum_s[-1], alts[-1]
        gain_now = gain_total
        for i in range(1, len(locs)):
            if cum_d[i] >= d_now and cum_d[i] > cum_d[i - 1]:
                k = (d_now - cum_d[i - 1]) / (cum_d[i] - cum_d[i - 1])
                t_now = cum_t[i - 1] + (cum_t[i] - cum_t[i - 1]) * k
                s_now = cum_s[i - 1] + (cum_s[i] - cum_s[i - 1]) * k
                alt_now = alts[i - 1] + (alts[i] - alts[i - 1]) * k
                g = sum(max(0.0, alts[j] - alts[j - 1]) for j in range(1, i))
                gain_now = g + max(0.0, alt_now - alts[i - 1])
                break

        lap_d = d_now - prev_d
        lap_t = t_now - prev_t
        lap_steps = s_now - prev_steps
        laps.append({
            'avgCadence': round(lap_steps / (lap_t / 60.0), 2) if lap_t > 0 else None,
            # seconds per km, NOT minutes (upstream pace-unit-fix)
            'avgPace': round(lap_t / (lap_d / 1000.0), 2) if lap_d > 0 else None,
            'avgStride': round(lap_d / lap_steps * 100.0, 2) if lap_steps > 0 else None,
            'cumulativeDuration': int(round(t_now)),
            'distance': round(lap_d, 4),
            'duration': int(round(lap_t)),
            'elevationGain': round(gain_now - prev_gain, 2),
            'endAltAbs': round(alt_now, 2),
            'endAltRel': round(alt_now - alt0, 2),
            'flag': start_ms,
            'id': len(laps) + 1,
            'isFullLap': lap_d >= 1000.0,
            'lapIndex': len(laps) + 1,
            'step': int(round(lap_steps)),
        })
        prev_d, prev_t, prev_steps, prev_gain = d_now, t_now, s_now, gain_now
    return laps


# --------------------------------------------------------------------------
# object + keys
# --------------------------------------------------------------------------

def _obs_windows(values: list, start_ms: int, rrid: int | None = None) -> list:
    """`wire.rs::build_windows` — real-device OBS window semantics.

    Upstream PR #106 (real-device capture) found three differences from the
    source-derived version we used to emit:

    * ``id`` is a per-run sequence derived from the start time, not rrid;
    * ``queueNum`` is the 0-based window index, not always 0;
    * the final window may be shorter than 10 s, so its distance/steps must be
      scaled by the actual duration or the replay shows 300+ spm.

    ``rrid`` is kept in the signature for callers/tests; it is intentionally
    not used for ids anymore.
    """
    del rrid
    out = []
    id_base = (int(start_ms) // 1000) % 100000
    for i, v in enumerate(values):
        d = dict(v)
        b = int(d.get('beginTime', start_ms))
        e = int(d.get('endTime', start_ms))
        window_s = max(0.001, (e - b) / 1000.0)
        frac = max(0.01, min(1.0, window_s / 10.0))
        if d.get('distance') is not None:
            d['distance'] = round(float(d['distance']) * frac, 2)
        if d.get('stepsNum') is not None:
            max_steps = int(math.ceil(220.0 * window_s / 60.0))
            d['stepsNum'] = max(0, min(
                max_steps, int(round(float(d['stepsNum']) * frac))))
        d['id'] = id_base + i
        d['queueNum'] = i
        d['flag'] = start_ms
        out.append(d)
    return out


def build_obs_object(run, rrid, uuid, uid, five_point_json='') -> dict:
    start_ms = run['allLocJson'][0]['ts']

    # Prefer the 28-key points built alongside the record column: they already
    # carry the sentinel `type`/`state`/`radius` values and seconds-based
    # totalTime, and using the same objects guarantees the replay and the
    # record agree.
    pts = run.get('wire_points')
    if not pts:
        cum_d, cum_t, cum_s = _cumulative(run)
        pts = []
        prev = (0.0, 0.0)
        for i, loc in enumerate(run['allLocJson']):
            p = conv_point(loc, start_ms, cum_d[i], cum_t[i], cum_s[i], prev)
            prev = (cum_d[i], cum_t[i])
            pts.append(p)
        # upstream: the first point takes the first non-zero speed
        if len(pts) > 1:
            for k in ('speed', 'avgSpeed', 'bdS'):
                pts[0][k] = pts[1][k]

    run_wrap = {
        'allLocJson': json.dumps(pts, ensure_ascii=False, separators=(',', ':')),
        'useZip': False,
    }
    return {
        'rrid': gz_str(str(rrid)),
        'uuid': gz_str(uuid),
        'uid': gz_str(str(uid)),
        'run_data': gz_json(run_wrap),
        'fixed_point_json': gz_str(five_point_json or ''),
        'segment_json': gz_str(''),
        'speed_json': gz_json(_obs_windows(run.get('speed_json') or [],
                                           start_ms, rrid)),
        'step_freq_json': gz_json(_obs_windows(run.get('step_freq_json') or [],
                                               start_ms, rrid)),
        'laps_json': gz_json(build_laps(run, start_ms)),
        'runFaceCheck': gz_str(''),
    }


def rrid_key(rrid) -> str:
    """The object key the official detail page reads for one record.

    `ObsClient.downloadJsonSync` uses
    ``run_data/{rrid // 1000000}/{rrid}.json``; the same key is written by
    :func:`obs_keys` as its second entry.
    """
    n = int(rrid)
    return 'run_data/%d/%d.json' % (n // 1000000, n)


def obs_keys(run, rrid, uuid) -> list[str]:
    start_ms = run['allLocJson'][0]['ts']
    t0 = safety.to_beijing(start_ms).strftime('%Y%m%d%H')
    return [
        'run_data/%s/%s.json' % (t0, uuid),
        rrid_key(rrid),
    ]


# --------------------------------------------------------------------------
# upload
# --------------------------------------------------------------------------

def sign_url(client, acct, key: str, method: str = 'Put') -> str:
    res = client.request(acct, OBS_SIGN_PATH, {
        'bucketName': BUCKET,
        'objectKey': key,
        'method': method,
        'contentType': 'application/json',
    })
    biz = res.get('json') or {}
    if biz.get('error') not in (None, 10000):
        raise RuntimeError('OBS sign error %s: %s'
                           % (biz.get('error'), biz.get('message')))
    data = biz.get('data')
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    if isinstance(data, dict) and data.get('signedUrl'):
        return data['signedUrl']
    raise RuntimeError('OBS sign response missing signedUrl: %r' % (biz,)[:200])


def put_object(signed_url: str, payload: bytes, timeout: float = 60.0) -> int:
    import urllib.request
    req = urllib.request.Request(signed_url, data=payload, method='PUT',
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def get_object_bytes(signed_url: str,
                     timeout: float = 60.0) -> tuple[int, bytes]:
    """GET a signed object. Returns (status, body)."""
    import urllib.request
    req = urllib.request.Request(signed_url, method='GET')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def get_object(signed_url: str, timeout: float = 60.0) -> tuple[int, int]:
    """GET a signed object. Returns (status, byte length)."""
    status, body = get_object_bytes(signed_url, timeout=timeout)
    return status, len(body)


def _retry(fn, tries: int = 3, base: float = 0.6):
    """Retry an idempotent network step with exponential backoff.

    PUT to the same object key is an overwrite, and the signed URL is reissued
    on every attempt, so retrying is safe.  A single transient failure used to
    cost the whole trail (issue #85 is exactly "record without a trail").
    """
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            if i + 1 < tries:
                time.sleep(base * (2 ** i))
    raise last


def upload_obs(client, acct, run, rrid, uuid, five_point_json='', log=None,
               verify: bool = True) -> dict:
    """Upload the trail to both object keys. Returns a summary dict.

    `verify` re-reads each object with a signed GET and compares the byte
    length.  The reference client does not do this, but a silently failed PUT
    means the record exists with no trail and no checkpoints (issue #85), and
    nothing else in the flow would notice.
    """
    log = log or (lambda *_: None)
    obj = build_obs_object(run, rrid, uuid, acct.uid, five_point_json)
    payload = json.dumps(obj, ensure_ascii=False,
                         separators=(',', ':')).encode()
    keys = obs_keys(run, rrid, uuid)

    ok = 0
    verified = 0
    errors = []
    for key in keys:
        try:
            def _put(_key=key):
                u = sign_url(client, acct, _key, 'Put')
                st = put_object(u, payload)
                if not (200 <= st < 300):
                    raise RuntimeError('PUT status %s' % st)
                return st

            status = _retry(_put)
            log('[obs] PUT %s -> %s (%d B)' % (key, status, len(payload)))

            if verify:
                def _get(_key=key):
                    u = sign_url(client, acct, _key, 'Get')
                    st, ln = get_object(u)
                    if not (200 <= st < 300) or ln != len(payload):
                        raise RuntimeError('回读不一致 status=%s len=%d (want %d)'
                                           % (st, ln, len(payload)))
                    return st, ln

                gstatus, glen = _retry(_get)
                verified += 1
                log('[obs] GET %s -> %s (%d B) ✓' % (key, gstatus, glen))
            ok += 1
        except Exception as e:  # noqa: BLE001
            log('[obs] %s 失败: %s' % (key, e))
            errors.append('%s: %s' % (key, e))
    return {'uploaded': ok, 'verified': verified, 'of': len(keys),
            'keys': keys, 'bytes': len(payload), 'errors': errors}


# --------------------------------------------------------------------------
# self test
# --------------------------------------------------------------------------

def self_test() -> list[tuple[str, bool, str]]:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from ..domain.rundata import generate_run_data

    out = []
    run = generate_run_data(distance_km=2.5, pace_min_per_km=6.0)
    obj = build_obs_object(run, 1333336472, 'UUID-TEST', 99999999, '{"a":1}')

    out.append(('10 keys', len(obj) == 10, str(sorted(obj.keys()))[:80]))

    decoded = json.loads(gzip.decompress(base64.b64decode(obj['run_data'])))
    out.append(('run_data unwraps',
                'allLocJson' in decoded and decoded['useZip'] is False,
                str(list(decoded.keys()))))

    pts = json.loads(decoded['allLocJson'])
    out.append(('28-key points', len(pts) > 0 and len(pts[0]) == 28,
                '%d points, %d keys' % (len(pts), len(pts[0]))))
    out.append(('lat/lng zeroed, gLat set',
                pts[0]['lat'] == -1.0 and pts[0]['gLat'] != 0,
                'gLat=%.6f' % pts[0]['gLat']))
    out.append(('cumulative distance ends at juli',
                abs(pts[-1]['totalDis'] - run['juli']) < 1.0,
                '%.1f vs %s' % (pts[-1]['totalDis'], run['juli'])))
    out.append(('cumulative time matches',
                abs(pts[-1]['totalTime'] - run['sportTime']) <= 1,
                '%s vs %s' % (pts[-1]['totalTime'], run['sportTime'])))

    laps = json.loads(gzip.decompress(base64.b64decode(obj['laps_json'])))
    out.append(('laps cover the distance',
                abs(sum(l['distance'] for l in laps) - run['juli']) < 1.0,
                '%d laps, %.0fm' % (len(laps), sum(l['distance'] for l in laps))))
    out.append(('avgPace in seconds/km',
                laps[0]['avgPace'] is not None and 300 < laps[0]['avgPace'] < 700,
                str(laps[0]['avgPace'])))

    keys = obs_keys(run, 1333336472, 'UUID-TEST')
    out.append(('two object keys', len(keys) == 2 and keys[0].startswith('run_data/'),
                ' | '.join(keys)))

    sp = json.loads(gzip.decompress(base64.b64decode(obj['speed_json'])))
    out.append(('speed windows compressed', len(sp) == len(run['speed_json']),
                '%d windows' % len(sp)))
    out.append(('speed window ids are sequential',
                all(sp[i]['id'] == sp[0]['id'] + i for i in range(len(sp))),
                '%s..%s' % (sp[0]['id'], sp[-1]['id'])))
    out.append(('speed window queueNum is the index',
                all(w['queueNum'] == i for i, w in enumerate(sp)),
                '0..%d' % (len(sp) - 1)))
    out.append(('step window sentinels',
                all(w['maxDiff'] == 0.0 and w['minDiff'] == 1000.0
                    for w in json.loads(
                        gzip.decompress(base64.b64decode(obj['step_freq_json'])))),
                '0.0 / 1000.0'))

    # A duration that is not a multiple of 10 s leaves a short final window.
    # The old code kept a full 10 s distance/steps there, so the replay showed
    # 300+ spm.  Use 2.54 km @ 6'00" = 914 s (last window 4 s).
    run2 = generate_run_data(distance_km=2.54, pace_min_per_km=6.0)
    obj2 = build_obs_object(run2, 1333336473, 'UUID-TEST-2', 99999999, '')
    st2 = json.loads(gzip.decompress(base64.b64decode(obj2['step_freq_json'])))
    sp2 = json.loads(gzip.decompress(base64.b64decode(obj2['speed_json'])))
    last_st = st2[-1]
    last_s = max(0.1, (last_st['endTime'] - last_st['beginTime']) / 1000.0)
    last_cad = last_st['stepsNum'] / last_s * 60.0
    last_sp = sp2[-1]
    last_v = (last_sp['distance'] / max(0.1, (last_sp['endTime']
                                              - last_sp['beginTime']) / 1000.0))
    out.append(('last partial window cadence sane',
                last_cad <= 225.0, '%.0f spm / %.1fs' % (last_cad, last_s)))
    out.append(('last partial window speed sane',
                last_v < 12.0, '%.1f m/s' % last_v))

    out.append(('object parses as JSON',
                isinstance(json.loads(json.dumps(obj)), dict), 'ok'))
    return out


if __name__ == '__main__':
    ok = True
    for name, passed, detail in self_test():
        print('%-30s %s   %s' % (name, 'PASS' if passed else 'FAIL', detail))
        ok = ok and passed
    print('\nRESULT:', 'PASS' if ok else 'FAIL')
    raise SystemExit(0 if ok else 1)

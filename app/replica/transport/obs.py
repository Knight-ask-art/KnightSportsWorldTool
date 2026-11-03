"""OBS trail upload — the object the app's replay actually reads.

Ported from YanamiNeko/NekoSportsWorldTool `src/track/wire.rs` + `src/api/obs.rs`.

Flow
----
    POST /api/obs/temporary/url   {bucketName, objectKey, method, contentType}
      -> data.signedUrl
    PUT  <signedUrl>              (the JSON body below, raw)
    ... once, with the hour/uuid key the official 7.4.0 capture writes:
        run_data/{YYYYMMDDHH}/{uuid}.json

Object (11 keys, every value base64(gzip(...)))
------------------------------------------------
    rrid, uuid, uid, run_data, extension_json(""), fixed_point_json,
    segment_json(""), speed_json, step_freq_json, laps_json, runFaceCheck("")

`run_data` is itself JSON: {"allLocJson": "<json array string>", "useZip": false}
where the array holds 27-key points (gLat/gLng, cumulative totalDis/totalTime;
`speed` is pace in min/km, `avgSpeed`/`bdS` are m/s).

Without this upload the record exists but the detail page / replay has no trail
and no checkpoints — see upstream issue #85.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
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
#: if the generator is fed real BD-09 coordinates.
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

    Distance comes from the generated 27-key points when they are present:
    ``totalDis`` already carries the per-kilometre pace profile and the stop
    windows.  Deriving it from time instead (the old behaviour) forced every
    lap to the same split and threw the whole profile away, which is exactly
    the artefact a reviewer notices.  Falls back to time, then geometry, when
    the record has no point column (hand-built payloads in tests).
    """
    locs = run['allLocJson']
    t0 = locs[0]['ts']
    cum_t = [max(0, p['ts'] - t0) / 1000.0 for p in locs]
    span = cum_t[-1]

    cum_d = None
    pts = run.get('locPoints') or []
    if len(pts) == len(locs):
        try:
            vals = [float(p.get('totalDis') or 0.0) for p in pts]
        except (TypeError, ValueError):
            vals = []
        if (len(vals) == len(locs) and vals[-1] > 0
                and all(b >= a for a, b in zip(vals, vals[1:]))):
            total = float(run['juli'])
            scale = total / vals[-1] if vals[-1] else 0.0
            cum_d = [v * scale for v in vals]
            cum_d[-1] = total

    if cum_d is None:
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
    cum_s = None
    windows = run.get('step_freq_json') or []
    if windows and len(cum_t) > 1:
        # Steps must come from the step windows, not from time.  Spreading
        # total_steps linearly over the clock made every lap report the exact
        # same cadence (150.0 spm on every split) while the stride column
        # still moved, which no real watch produces.
        bounds = []
        for w in windows:
            b = max(0.0, (float(w.get('beginTime', 0)) - t0) / 1000.0)
            e = max(b, (float(w.get('endTime', 0)) - t0) / 1000.0)
            bounds.append((b, e, max(0.0, float(w.get('stepsNum') or 0))))
        out = []
        j = 0
        done = 0.0
        for t in cum_t:
            while j < len(bounds) and t >= bounds[j][1]:
                done += bounds[j][2]
                j += 1
            extra = 0.0
            if j < len(bounds) and t > bounds[j][0]:
                b, e, n = bounds[j]
                extra = n * (t - b) / max(1e-6, e - b)
            out.append(int(round(done + extra)))
        out[-1] = total_steps
        cum_s = out
    if cum_s is None:
        cum_s = [int(round(total_steps * t / span)) for t in cum_t]
        if cum_s:
            cum_s[-1] = total_steps
    return cum_d, cum_t, cum_s


def conv_point(loc, start_ms, cum_d, cum_t, cum_s, prev) -> dict:
    """One 27-key 7.4.0 OBS point (no `steps`; `speed` is pace)."""
    if CONVERT_BD:
        glat, glng = bd09_to_gcj02(loc['lat'], loc['lng'])
    else:
        glat, glng = loc['lat'], loc['lng']     # already GCJ-02

    dt = max(1e-6, cum_t - prev[1])
    dd = max(0.0, cum_d - prev[0])
    mps = dd / dt
    pace = min(999.99, 16.6667 / mps) if mps >= 0.05 else 0.0
    return {
        'avgSpeed': round(mps, 4),
        'bdA': round(float(loc.get('alt', 0.0)), 2),
        'bdD': 0.0,
        'bdG': 1,
        'bdS': round(mps, 4),
        'coorType': 'gcj02',
        'count': 60,
        'dtr': 0.0,
        'flag': start_ms,
        'gLat': round(glat, 7),
        'gLng': round(glng, 7),
        'gainTime': safety.fmt_beijing(int(loc['ts'])),
        'id': int(loc.get('index', 0)) + 1,
        'lat': -1.0,
        'lng': -1.0,
        'locType': 0,
        'locationId': '',
        'queueNum': max(0, int(cum_t) - 1) // 30,
        'radius': 5.0,
        'speed': round(pace, 2),
        'state': 1,
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
            # MINUTES per km.  The official lap table formats this as
            # ``(int)(avgPace * 60)`` seconds (RunLapsFullAdapter.formatPace),
            # so storing seconds here made a 7'00"/km lap render as 420'00".
            'avgPace': round(lap_t / 60.0 / (lap_d / 1000.0), 4) if lap_d > 0 else None,
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
    * ``queueNum`` advances every 30 s (three 10 s windows), not every point;
    * the final window may be shorter than 10 s.  Its values are already for
      ``[beginTime, endTime]`` (``rundata`` builds them from the actual end
      second), so they must NOT be scaled by ``window_s / 10`` again.

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
        if d.get('distance') is not None:
            d['distance'] = round(float(d['distance']), 2)
        if d.get('stepsNum') is not None:
            # Guard against an impossible cadence, but keep the total steps:
            # the source already allocated only what fits in this window.
            max_steps = int(math.ceil(220.0 * window_s / 60.0))
            d['stepsNum'] = max(0, min(
                max_steps, int(round(float(d['stepsNum'])))))
        d['id'] = id_base + i
        # A real 7.2/7.3 record advances the batch every three 10 s windows
        # (0,0,0,1,1,1,...), matching the point column's 30 s queue.
        d['queueNum'] = i // 3
        d['flag'] = start_ms
        out.append(d)
    return out


def build_obs_object(run, rrid, uuid, uid, five_point_json='') -> dict:
    start_ms = run['allLocJson'][0]['ts']

    # Prefer the 27-key points built alongside the record column: they already
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
        # The 7.4.0 capture has 11 keys; `extension_json` decodes to an empty
        # string (same as `segment_json`).  Omitting it is a schema mismatch
        # even though the value carries no data.
        'extension_json': gz_str(''),
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
    """Legacy/third-party OBS key for one record.

    Some old objects only exist at ``run_data/{rrid // 1000000}/{rrid}.json``;
    the detail reader tries this after the official hour/uuid key.  Our upload
    does not write it (the 7.4.0 capture has a single PUT).
    """
    n = int(rrid)
    return 'run_data/%d/%d.json' % (n // 1000000, n)


def obs_keys(run, rrid, uuid) -> list[str]:
    """The single key the official 7.4.0 client PUTs.

    The 2026-10-26 capture has exactly one COS PUT: the UTC+8 hour bucket.
    ``rrid_key`` is kept for reading old/third-party objects that only wrote
    the rrid-shaped key; writing it ourselves was an extra fingerprint.
    """
    del rrid
    start_ms = run['allLocJson'][0]['ts']
    t0 = safety.to_beijing(start_ms).strftime('%Y%m%d%H')
    return ['run_data/%s/%s.json' % (t0, uuid)]


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


def _object_headers(user_agent: str = '', content_type: str = '') -> dict:
    """Headers for the OBS data plane.

    The signed URL covers method/key/expiry, not the UA, so adding one cannot
    invalidate the signature.  Without it urllib sends ``Python-urllib/x.y``:
    one run would then show a platform UA on the API calls and a bare Python
    UA on the two PUTs, which is the cheapest possible way to link them.  The
    reference client has the same hole (``ureq/x``), and the antiban audit
    calls it out as "同一次运行四条不同 UA".
    """
    headers = {}
    if content_type:
        headers['Content-Type'] = content_type
    if user_agent:
        headers['User-Agent'] = user_agent
    return headers


def put_object(signed_url: str, payload: bytes, timeout: float = 60.0,
               user_agent: str = '') -> int:
    import urllib.request
    req = urllib.request.Request(
        signed_url, data=payload, method='PUT',
        headers=_object_headers(user_agent, 'application/json'))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def get_object_bytes(signed_url: str, timeout: float = 60.0,
                     user_agent: str = '') -> tuple[int, bytes]:
    """GET a signed object. Returns (status, body)."""
    import urllib.request
    req = urllib.request.Request(signed_url, method='GET',
                                 headers=_object_headers(user_agent))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def get_object(signed_url: str, timeout: float = 60.0,
               user_agent: str = '') -> tuple[int, int]:
    """GET a signed object. Returns (status, byte length)."""
    status, body = get_object_bytes(signed_url, timeout=timeout,
                                    user_agent=user_agent)
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


def platform_user_agent(acct) -> str:
    """The same UA the API calls use (one run, one UA)."""
    try:
        from .realclient import user_agent
        return user_agent(acct)
    except Exception:  # noqa: BLE001 - a UA must never break an upload
        return ''


def upload_obs(client, acct, run, rrid, uuid, five_point_json='', log=None,
               verify: bool = True) -> dict:
    """Upload the trail to the official hour/uuid key. Returns a summary dict.

    `verify` re-reads each object with a signed GET and compares the byte
    length.  The reference client does not do this, but a silently failed PUT
    means the record exists with no trail and no checkpoints (issue #85), and
    nothing else in the flow would notice.
    """
    log = log or (lambda *_: None)
    ua = platform_user_agent(acct)
    obj = build_obs_object(run, rrid, uuid, acct.uid, five_point_json)
    payload = json.dumps(obj, ensure_ascii=False,
                         separators=(',', ':')).encode()
    payload_sha = hashlib.sha256(payload).hexdigest()
    keys = obs_keys(run, rrid, uuid)

    ok = 0
    verified = 0
    errors = []
    for key in keys:
        try:
            def _put(_key=key):
                u = sign_url(client, acct, _key, 'Put')
                st = put_object(u, payload, user_agent=ua)
                if not (200 <= st < 300):
                    raise RuntimeError('PUT status %s' % st)
                return st

            status = _retry(_put)
            ok += 1
            log('[obs] PUT %s -> %s (%d B sha256=%s)'
                % (key, status, len(payload), payload_sha[:12]))

            if verify:
                def _get(_key=key):
                    u = sign_url(client, acct, _key, 'Get')
                    st, body = get_object_bytes(u, user_agent=ua)
                    got_sha = hashlib.sha256(body).hexdigest()
                    if (not (200 <= st < 300) or len(body) != len(payload)
                            or got_sha != payload_sha):
                        raise RuntimeError(
                            '回读不一致 status=%s len=%d sha256=%s (want %s)'
                            % (st, len(body), got_sha[:12], payload_sha[:12]))
                    return st, len(body)

                gstatus, glen = _retry(_get)
                verified += 1
                log('[obs] GET %s -> %s (%d B sha256 ✓)' % (key, gstatus, glen))
        except Exception as e:  # noqa: BLE001
            log('[obs] %s 失败: %s' % (key, e))
            errors.append('%s: %s' % (key, e))
    return {'uploaded': ok, 'verified': verified, 'of': len(keys),
            'keys': keys, 'bytes': len(payload), 'sha256': payload_sha,
            'errors': errors}


# --------------------------------------------------------------------------
# self test
# --------------------------------------------------------------------------

def self_test() -> list[tuple[str, bool, str]]:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from ..domain.rundata import generate_run_data

    out = []
    # Self-test only.  Production passes the account's fence centre; there is
    # no built-in campus in generate_run_data any more.
    test_lat, test_lng = 22.4108, 112.9408
    run = generate_run_data(distance_km=2.5, pace_min_per_km=6.0,
                            center_lat=test_lat, center_lng=test_lng)
    obj = build_obs_object(run, 1333336472, 'UUID-TEST', 99999999, '{"a":1}')

    out.append(('11 keys', len(obj) == 11, str(sorted(obj.keys()))[:80]))

    decoded = json.loads(gzip.decompress(base64.b64decode(obj['run_data'])))
    out.append(('run_data unwraps',
                'allLocJson' in decoded and decoded['useZip'] is False,
                str(list(decoded.keys()))))

    pts = json.loads(decoded['allLocJson'])
    out.append(('27-key points', len(pts) > 0 and len(pts[0]) == 27,
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
    out.append(('avgPace in minutes/km',
                laps[0]['avgPace'] is not None and 4.0 < laps[0]['avgPace'] < 12.0,
                str(laps[0]['avgPace'])))

    keys = obs_keys(run, 1333336472, 'UUID-TEST')
    out.append(('one hour/uuid key', len(keys) == 1
                and keys[0].startswith('run_data/')
                and keys[0].endswith('/UUID-TEST.json'),
                ' | '.join(keys)))

    sp = json.loads(gzip.decompress(base64.b64decode(obj['speed_json'])))
    out.append(('speed windows compressed', len(sp) == len(run['speed_json']),
                '%d windows' % len(sp)))
    out.append(('speed window ids are sequential',
                all(sp[i]['id'] == sp[0]['id'] + i for i in range(len(sp))),
                '%s..%s' % (sp[0]['id'], sp[-1]['id'])))
    out.append(('speed window queueNum advances every 30s',
                all(w['queueNum'] == i // 3 for i, w in enumerate(sp)),
                '0..%d' % (len(sp) // 3)))
    out.append(('step window sentinels',
                all(w['maxDiff'] == 0.0 and w['minDiff'] == 1000.0
                    for w in json.loads(
                        gzip.decompress(base64.b64decode(obj['step_freq_json'])))),
                '0.0 / 1000.0'))

    # A duration that is not a multiple of 10 s leaves a short final window.
    # The generator already allocated only the distance/steps that fit in it;
    # scaling by window_s/10 here would shrink it a second time and make the
    # window totals disagree with juli/totalSteps.  Use 2.54 km @ 6'00" =
    # 914 s (last window 4 s).
    run2 = generate_run_data(distance_km=2.54, pace_min_per_km=6.0,
                             center_lat=test_lat, center_lng=test_lng)
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
    out.append(('window distance sum matches juli',
                abs(sum(w['distance'] for w in sp2) - run2['juli']) < 1.0,
                '%.2f vs %.2f' % (sum(w['distance'] for w in sp2),
                                  run2['juli'])))
    out.append(('window steps sum matches totalSteps',
                sum(w['stepsNum'] for w in st2) == run2['total_steps'],
                '%d vs %d' % (sum(w['stepsNum'] for w in st2),
                              run2['total_steps'])))

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

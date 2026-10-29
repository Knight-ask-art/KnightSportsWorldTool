"""Clean-room run-data generator, following the binary-evidence format.

Evidence (mock_db实测 + 反汇编局部变量表, see 逆向分析完整报告 §10):
- allLocJson: 25 track points {lat/lng/ts/alt/index}  (report: glat/glon/ts/alt/index)
- speed_json: 90 segments {startSecond/endSecond/speed/state} (10s each)
- step_freq_json: 90 segments (steps per 10s; display stepsNum*6 = cadence/min)
- segmentJson: 5 segments {180s/500m, speedColor=#4CAF50}
- fivePoint: 5 calibration points, every 6th track point
- juli/km, sportTime, sportunid
- sample: 2.5km/900s, speed ~2.7-2.9 m/s, steps ~150/min
- route: circular random route, drift +-0.0004deg, altitude 20-50m
"""
import bisect
import json
import math
import random
import re
import time
import uuid
from .crypto import jbool, official_presign, official_sign
from . import health
from . import coords
from . import safety

N_POINTS = 25
N_SEGMENTS = 90
SEG_SECONDS = 10

#: A loop shorter than this cannot be repeated into a believable run: the
#: samples end up zig-zagging on the spot and the measured geometry collapses.
MIN_LOOP_M = 80.0
#: Beyond this many laps the track reads as "someone shaking a pen".
MAX_LAPS = 30
FIVE_EVERY = 6

#: Pace deviation: an OU process with a 24 s memory and a 2% stationary sd.
#: Correlated on purpose -- two fixed sine frequencies used to sit at 3 and 7
#: cycles per run whatever the duration, which is a fingerprint.  See the pace
#: profile block in :func:`generate_run_data`.
PACE_TAU_S = 24.0
PACE_SIGMA = 0.020

#: GPS-level pace noise, on top of the physiological drift above.  The
#: account's own 4 km traces (2026-09/10) have a 10 s window distance CV of
#: 0.19-0.52, and it tracks pace almost exactly like ``0.20 * (pace/6)^1.5``
#: (6'05" -> 0.19, 7'25" -> 0.41, 8'57" -> 0.52): a fixed GPS position error
#: hurts the measured distance more the slower the window moves.  A smooth OU
#: with 2% sd produced 0.03-0.07, i.e. a mathematically perfect pace curve that
#: no phone ever records.
WINDOW_TAU_S = 10.0
WINDOW_SIGMA = 0.20

#: Cadence drifts on a slower clock than pace, so per-kilometre splits end up
#: with different average cadences (real laps differ by several spm).  Keep the
#: drift fast enough that it does not park on one side of a whole kilometre:
#: an earlier 45 s / 9% setting produced 120 and 183 spm laps in the same
#: 6'00" run.
CADENCE_TAU_S = 30.0
CADENCE_SIGMA = 0.035


def new_run_uuid(platform: str | None = None) -> str:
    """A run id shaped like the real client's.

    Android ``UUID.randomUUID().toString()`` is lower-case with dashes; iOS
    ``UUID().uuidString`` is upper-case with dashes.  A bare 32-hex string is
    neither, and it is visible on every record.
    """
    value = str(uuid.uuid4())
    if str(platform or '').strip().lower() == 'ios':
        return value.upper()
    return value


def _adjacent_ok(locs: list[dict], limit_m: float = 95.0) -> bool:
    for a, b in zip(locs, locs[1:]):
        dx = (b['lng'] - a['lng']) * 111320 * math.cos(math.radians(a['lat']))
        dy = (b['lat'] - a['lat']) * 110540
        if math.hypot(dx, dy) > limit_m:
            return False
    return True


def _polyline_len(poly: list) -> float:
    return sum(math.hypot((b[1] - a[1]) * 111320 * math.cos(math.radians(a[0])),
                          (b[0] - a[0]) * 110540)
               for a, b in zip(poly, poly[1:]))


def _trim_polyline(poly: list, target_m: float) -> list:
    """Cut the path at exactly `target_m` (the last segment is interpolated).

    `build_loop` returns whole laps, which is usually a few metres longer than
    the target.  Sampling only the first `target_m` of that path used to leave
    a tail unvisited; if a checkpoint sat there, `_snap_to_waypoints` inserted
    a detour and the final geometry overshot by ~5%.  Trimming first makes the
    sampled window cover the whole path.
    """
    if not poly or target_m <= 0:
        return poly
    out = [poly[0]]
    walked = 0.0
    for a, b in zip(poly, poly[1:]):
        seg = math.hypot((b[1] - a[1]) * 111320 * math.cos(math.radians(a[0])),
                         (b[0] - a[0]) * 110540)
        if walked + seg >= target_m:
            f = (target_m - walked) / seg if seg > 0 else 0.0
            out.append((a[0] + (b[0] - a[0]) * f,
                        a[1] + (b[1] - a[1]) * f))
            return out
        out.append(b)
        walked += seg
    return out


def _rotate_polyline(poly: list, start_m: float) -> list:
    """Start the closed path at `start_m`, keeping it continuous.

    Sampling at ``f % total`` would jump from the trimmed end back to the
    original start, which ``validate_run_data`` sees as a teleport.  Rotating
    the point list is seamless: the cut point is appended on both sides.
    """
    if not poly or start_m <= 0:
        return poly
    walked = 0.0
    for k, (a, b) in enumerate(zip(poly, poly[1:])):
        seg = math.hypot((b[1] - a[1]) * 111320 * math.cos(math.radians(a[0])),
                         (b[0] - a[0]) * 110540)
        if walked + seg >= start_m:
            f = (start_m - walked) / seg if seg > 0 else 0.0
            cut = (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
            return [cut] + poly[k + 1:] + poly[1:k + 1] + [cut]
        walked += seg
    return poly


def _loop_start_for_waypoints(loop: list, target_m: float, rng,
                              preferred_m: float = 0.0) -> float | None:
    """Pick a start on a closed loop that keeps every waypoint in the window.

    The old code always started at the first waypoint (plus 20-50 m), so a
    target window could cut off a checkpoint that sat on the far side of the
    loop.  `_snap_to_waypoints` then re-inserted it, adding a detour and
    inflating the measured geometry past the 4% server tolerance.

    Start just *before* the first waypoint after the longest gap: the window
    then runs across the shortest span that still contains every waypoint.
    Raises ValueError when even that span is longer than the target: snapping
    the missing points would otherwise inflate the geometry or teleport.
    """
    if not loop or len(loop) < 3 or target_m <= 0:
        return None
    pos = [0.0]
    for a, b in zip(loop, loop[1:]):
        pos.append(pos[-1] + math.hypot(
            (b[1] - a[1]) * 111320 * math.cos(math.radians(a[0])),
            (b[0] - a[0]) * 110540))
    lap = pos[-1]
    n = len(loop) - 1                      # the last vertex repeats the first
    if lap <= 0 or n < 2:
        return None
    gaps = []
    for i in range(n):
        nxt = pos[i + 1] if i + 1 < n else lap
        gaps.append(nxt - pos[i])
    cut = max(range(n), key=lambda i: gaps[i])
    span = lap - gaps[cut]
    slack = target_m - span
    if slack <= 1.0:
        raise ValueError('路线跨度 %.0fm 超过目标距离 %.0fm；请增大距离或缩小路线'
                         % (span, target_m))
    base = pos[cut + 1] if cut + 1 < n else 0.0
    want = preferred_m if preferred_m > 0 else rng.uniform(20.0, 50.0)
    off = min(want, slack)
    return (base - off) % lap


def build_loop(waypoints: list, target_m: float) -> list:
    """Close the polyline and repeat it until it covers `target_m`."""
    loop = list(waypoints)
    if len(loop) < 2:
        return loop
    if loop[0] != loop[-1]:
        loop.append(loop[0])
    lap = _polyline_len(loop)
    if lap <= 0 or target_m <= 0:
        return loop
    if lap < MIN_LOOP_M:
        raise ValueError('路线周长只有 %.0fm，至少要 %.0fm（画大一点）'
                         % (lap, MIN_LOOP_M))
    laps = max(1, int(math.ceil(target_m / lap)))
    if laps > MAX_LAPS:
        raise ValueError('路线太短：铺满 %.1fkm 要 %d 圈（上限 %d），请画长一点'
                         % (target_m / 1000.0, laps, MAX_LAPS))
    out = []
    for _ in range(laps):
        out.extend(loop[:-1])
    out.append(loop[-1])
    return out


def build_open(waypoints: list, target_m: float) -> list:
    """Keep a drawn route open; extend it by retracing, never by a jump.

    If the drawn path is shorter than the target, walk it forward then back
    along the same line (an out-and-back), stopping when there is enough
    geometry for ``_sample_loop`` to trim.  This avoids a closing chord and
    avoids scaling the whole route away from the user's waypoints.
    """
    path = list(waypoints)
    if len(path) < 2 or target_m <= 0:
        return path
    total = _polyline_len(path)
    if total <= 0:
        return path
    if total < MIN_LOOP_M:
        raise ValueError('路线只有 %.0fm，至少要 %.0fm（画大一点）'
                         % (total, MIN_LOOP_M))
    out = list(path)
    backwards = True
    # Bound the loop: 40 km target at 100 m route is already extreme; fail
    # clearly instead of allocating an unbounded list.
    max_points = max(10_000, len(path) * 4)
    while _polyline_len(out) < target_m:
        if backwards:
            out.extend(path[-2::-1])
        else:
            out.extend(path[1:])
        backwards = not backwards
        if len(out) > max_points:
            raise ValueError('路线太短，铺满目标距离需要重复太多次，请画长一点')
    return out


def _sample_loop(poly: list, n_points: int, target_m: float | None,
                 rng, dist_fractions: list[float] | None = None) -> list[tuple]:
    seg_lens = [math.hypot((b[1] - a[1]) * 111320 * math.cos(math.radians(a[0])),
                           (b[0] - a[0]) * 110540)
                for a, b in zip(poly, poly[1:])]
    total = sum(seg_lens)
    if total <= 0:
        return []
    span = min(target_m, total) if target_m else total
    if not dist_fractions:
        dist_fractions = [i / max(1, n_points - 1) for i in range(n_points)]

    # Cumulative lengths + bisect: walking the polyline from the start for
    # every one of the 400-700 samples was ~0.5 s per run (the loop can have
    # thousands of vertices), and the UI previews a run on every keystroke.
    cum = []
    acc = 0.0
    for seg in seg_lens:
        acc += seg
        cum.append(acc)
    last = len(seg_lens) - 1

    out = []
    for f in dist_fractions[:n_points]:
        target = span * max(0.0, min(1.0, f))
        k = min(bisect.bisect_right(cum, target), last)
        prev = cum[k - 1] if k else 0.0
        seg = seg_lens[k]
        frac = (target - prev) / seg if seg > 0 else 0.0
        frac = min(max(frac, 0.0), 1.0)
        a, b = poly[k], poly[k + 1]
        out.append((a[0] + (b[0] - a[0]) * frac,
                    a[1] + (b[1] - a[1]) * frac))
    return out


def _passes_near(pts: list[dict], lat: float, lng: float,
                 radius_m: float = 25.0) -> bool:
    """`RunRanLocationManager.passPoint` — the official pass radius is 25 m.

    Both the random and sequential managers use ``distance <= 25.0``; the old
    40 m here marked points as passed that the real client would never tick.
    """
    for p in pts:
        dy = (p['lat'] - lat) * 110540.0
        dx = (p['lng'] - lng) * 111320.0 * math.cos(math.radians(lat))
        if dx * dx + dy * dy <= radius_m * radius_m:
            return True
    return False


def _point_number(point: dict):
    """School's point number, from ``position``/``pointName``/``id``."""
    for key in ('seq', 'position'):
        try:
            return int(point.get(key))
        except (TypeError, ValueError):
            continue
    name = str(point.get('pointName') or point.get('name') or '')
    match = re.search(r'(\d+)\s*$', name) or re.search(r'(\d+)', name)
    if match:
        return int(match.group(1))
    return None


def _order_checkpoints(points: list[dict]) -> list[dict]:
    """Stable order by the school's point number (sequential schools)."""
    def key(point):
        number = _point_number(point)
        return (number is None, number if number is not None else 0)

    return sorted(points, key=key)


def _bearing_deg(a: dict, b: dict) -> float:
    """Compass bearing from ``a`` to ``b``, degrees clockwise from north.

    ``MyLocation.bdD`` is ``bdDirection`` -- what AMap hands the client in the
    location callback -- and it reaches the renderer as the fourth argument of
    ``LineBean(latLng, type, speed, bdD)``.  Writing 0.0 for every point means
    every record faces due north for its whole length.
    """
    dy = b['lat'] - a['lat']
    dx = (b['lng'] - a['lng']) * math.cos(
        math.radians((a['lat'] + b['lat']) / 2.0))
    return round(math.degrees(math.atan2(dx, dy)) % 360.0, 2)


def _nearest_index(pts: list[dict], lat: float, lng: float) -> int:
    """Index of the sample closest to a GCJ-02 point."""
    cos_lat = math.cos(math.radians(lat))

    def d2(p):
        dy = (p['lat'] - lat) * 110540.0
        dx = (p['lng'] - lng) * 111320.0 * cos_lat
        return dx * dx + dy * dy

    return min(range(len(pts)), key=lambda i: d2(pts[i]))


def _snap_to_waypoints(pts: list[dict], waypoints: list, limit_m: float = 20.0):
    """Guarantee every checkpoint is physically passed.

    The official client ticks a point at 25 m; snapping within 20 m leaves
    room for the GPS jitter added afterwards.
    """
    def d2(a, b):
        return math.hypot((b[1] - a[1]) * 111320 * math.cos(math.radians(a[0])),
                          (b[0] - a[0]) * 110540)

    for wp in waypoints:
        if not any(d2((p['lat'], p['lng']), wp) <= limit_m for p in pts):
            # insert just after the closest existing sample
            idx = min(range(len(pts)),
                      key=lambda i: d2((pts[i]['lat'], pts[i]['lng']), wp))
            ref = pts[idx]
            pts.insert(idx + 1, {
                'lat': round(wp[0], 6),
                'lng': round(wp[1], 6),
                'ts': ref.get('ts', 0),
                'alt': ref.get('alt', 25.0),
                'index': 0,
            })


def _altitudes(n: int, rng, base: float = 25.0,
               distance_km: float | None = None) -> list[float]:
    """Terrain-shaped barometric altitude around `base`.

    The account's real traces span 19-23 m over 4 km, and a 1.3 km trace spans
    10 m, so the target peak-to-peak grows with distance instead of being one
    fixed band.

    The shape is deliberately not a clean sum of sines (that reads as a
    generated wave on the elevation chart):

    * 3 waves with different periods (0.5-6 cycles per run);
    * a slow barometric drift;
    * 0-2 short ramps (bridge / underpass / stairs) that create a plateau and
      a shoulder instead of a smooth crest;
    * a gentle correlated sensor wander (~25 s), which is what makes a real
      altitude trace look "alive" at 30 m resolution.

    The terrain part is finally rescaled to the distance-derived peak-to-peak,
    so the requested span is exact before the (small) sensor wander is added.
    """
    n = max(1, int(n))
    km = float(distance_km) if distance_km else n * 0.0084
    km = max(0.3, min(20.0, km))
    span = (rng.uniform(7.5, 9.5)
            + rng.uniform(2.6, 3.6) * min(km, 8.0))

    waves = [
        (rng.uniform(0.30, 0.60), rng.uniform(0.5, 1.3),
         rng.uniform(0.0, 2.0 * math.pi)),
        (rng.uniform(0.15, 0.35), rng.uniform(1.6, 3.2),
         rng.uniform(0.0, 2.0 * math.pi)),
        (rng.uniform(0.05, 0.15), rng.uniform(4.0, 7.0),
         rng.uniform(0.0, 2.0 * math.pi)),
    ]
    drift = rng.uniform(-0.9, 0.9)
    ramps = [(rng.uniform(0.12, 0.88),
              rng.choice((-1.0, 1.0)) * rng.uniform(1.2, 3.0),
              rng.uniform(0.008, 0.022))
             for _ in range(rng.randint(0, 2))]

    phi = math.exp(-3.0 / 25.0)
    rough_sigma = rng.uniform(0.06, 0.14)
    rough = 0.0
    terrain = []
    for i in range(n):
        u = i / max(1, n - 1)
        v = sum(a * math.sin(2.0 * math.pi * c * u + p)
                for a, c, p in waves) + drift * u
        for pos, height, width in ramps:
            v += height / (1.0 + math.exp(-(u - pos) / width))
        terrain.append(v)

    lo, hi = min(terrain), max(terrain)
    if hi - lo < 1e-9:
        terrain = [0.0] * n
        mid, scale = 0.0, 0.0
    else:
        mid = (hi + lo) / 2.0
        scale = span / (hi - lo)

    # Roughness is added in metres AFTER the rescale.  Scaling it with the
    # terrain was a trap: when the waves nearly cancelled, the scale factor
    # blew the sensor wander up to +-20 m and the ascent column read 170-600 m.
    out = []
    for v in terrain:
        rough = (phi * rough
                 + rng.gauss(0.0, rough_sigma * math.sqrt(1.0 - phi * phi)))
        out.append(round(base + (v - mid) * scale + rough
                         + rng.gauss(0.0, 0.03), 1))
    return out


#: Altitude modes exposed by the CLI/UI.  `auto` is the synthetic terrain;
#: `fixed` is one value (zero climb); `range` maps the synthetic profile to a
#: chosen min/max; `template` follows a user-selected local GPX/JSON profile.
ALTITUDE_MODES = ('auto', 'fixed', 'range', 'template')


def _template_numbers(text: str) -> list[float]:
    """Extract elevations from a GPX/JSON template without touching the network."""
    import re

    text = (text or '').strip()
    if not text:
        raise ValueError('海拔模板为空')
    if len(text) > 8 * 1024 * 1024:
        raise ValueError('海拔模板超过 8 MiB')

    values: list[float] = []
    # GPX: trkpt / rtept / wpt, in document order.
    if '<' in text and re.search(r'<(?:trkpt|rtept|wpt)\b', text, re.I):
        for m in re.finditer(r'<(?:trkpt|rtept|wpt)\b[^>]*>(.*?)'
                             r'</(?:trkpt|rtept|wpt)>', text, re.S | re.I):
            ele = re.search(r'<ele>\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)'
                            r'(?:[eE][-+]?\d+)?)\s*</ele>',
                            m.group(1), re.I)
            if ele:
                values.append(float(ele.group(1)))
        if not values:
            raise ValueError('GPX 模板里没有 <ele> 海拔点')
        return values

    # JSON: support arrays of numbers and objects with common elevation keys.
    if text[:1] in '[{':
        obj = json.loads(text)

        def walk(node):
            if isinstance(node, list):
                for item in node:
                    walk(item)
            elif isinstance(node, dict):
                for key in ('ele', 'elevation', 'altitude', 'alt', 'bdA'):
                    if key in node:
                        try:
                            values.append(float(node[key]))
                        except (TypeError, ValueError):
                            pass
                        # The elevation value is a leaf; walking it again
                        # appended every number twice.
                        return
                for child in node.values():
                    walk(child)
            elif isinstance(node, (int, float)):
                values.append(float(node))

        walk(obj)
        if not values:
            raise ValueError('JSON 模板里没有海拔字段（ele/elevation/altitude）')
        return values

    # Plain CSV/txt: last numeric column is the elevation.
    for line in text.splitlines():
        parts = [p.strip() for p in line.replace(';', ',').split(',')]
        if len(parts) >= 2:
            try:
                values.append(float(parts[-1]))
            except ValueError:
                continue
    if not values:
        raise ValueError('模板格式不支持；请用 GPX 或含海拔字段的 JSON')
    return values


def parse_altitude_template(text: str) -> list[float]:
    """Parse + smooth a local elevation template.

    GPS elevation is noisy; a short moving average keeps the real climb while
    removing one-point spikes that would inflate `totalAscent`.
    """
    values = [v for v in _template_numbers(text) if math.isfinite(v)]
    if len(values) < 2:
        raise ValueError('海拔模板至少需要 2 个有效点')
    if len(values) > 200_000:
        values = values[:200_000]
    if any(v < -500.0 or v > 9000.0 for v in values):
        raise ValueError('海拔模板含超出 -500–9000 m 的值')
    if len(values) >= 5:
        half = 2
        smoothed = []
        for i in range(len(values)):
            lo, hi = max(0, i - half), min(len(values), i + half + 1)
            smoothed.append(sum(values[lo:hi]) / (hi - lo))
        values = smoothed
    return values


def _map_profile(values: list[float], lo: float, hi: float) -> list[float]:
    vmin, vmax = min(values), max(values)
    if vmax - vmin < 1e-9:
        mid = (lo + hi) / 2.0
        return [mid for _ in values]
    scale = (hi - lo) / (vmax - vmin)
    return [lo + (v - vmin) * scale for v in values]


def resample_profile(values: list[float], n: int) -> list[float]:
    """Linear resample of an elevation profile to exactly `n` points."""
    n = int(n)
    if n <= 0:
        return []
    values = list(values or [])
    if not values:
        raise ValueError('海拔模板为空')
    if len(values) == 1:
        return [float(values[0])] * n
    out = []
    last = len(values) - 1
    for i in range(n):
        pos = (i * last) / max(1, n - 1)
        j = int(math.floor(pos))
        k = min(last, j + 1)
        frac = pos - j
        out.append(values[j] + (values[k] - values[j]) * frac)
    return out


def make_altitudes(n: int, rng, base: float = 25.0,
                   mode: str = 'auto',
                   alt_range: list | tuple | None = None,
                   profile: list[float] | None = None,
                   distance_km: float | None = None) -> list[float]:
    """Altitude sequence for one run.

    ``auto``     – OU barometer around `base` (legacy behaviour).
    ``fixed``    – every point at `base`; zero ascent/descent.
    ``range``    – synthetic profile mapped into ``(lo, hi)``.
    ``template`` – local GPX/JSON profile, optionally mapped into a range.
    """
    n = int(n)
    if n <= 0:
        return []
    mode = (mode or 'auto').strip().lower()
    if mode in ('synthetic', 'ou'):
        mode = 'auto'
    if mode not in ALTITUDE_MODES:
        raise ValueError('海拔模式未知: %s' % mode)
    try:
        base = float(base)
    except (TypeError, ValueError):
        raise ValueError('海拔基准无效: %r' % (base,))
    if not math.isfinite(base) or not (-500.0 <= base <= 9000.0):
        raise ValueError('海拔基准超出 -500–9000 m')

    rng_range = None
    if alt_range is not None:
        if len(alt_range) != 2:
            raise ValueError('海拔区间需要两个数：MIN MAX')
        try:
            lo, hi = float(alt_range[0]), float(alt_range[1])
        except (TypeError, ValueError):
            raise ValueError('海拔区间无效')
        if not (math.isfinite(lo) and math.isfinite(hi)):
            raise ValueError('海拔区间无效')
        if lo > hi:
            lo, hi = hi, lo
        if lo < -500.0 or hi > 9000.0:
            raise ValueError('海拔区间超出 -500–9000 m')
        rng_range = (lo, hi)

    if mode == 'fixed':
        return [round(base, 1)] * n

    if mode == 'range':
        if rng_range is None:
            raise ValueError('海拔区间模式需要 MIN MAX')
        raw = _altitudes(n, rng, base=0.0, distance_km=distance_km)
        mapped = _map_profile(raw, *rng_range)
        lo, hi = rng_range
        return [min(hi, max(lo, round(v, 1))) for v in mapped]

    if mode == 'template':
        if not profile:
            raise ValueError('海拔模板模式需要 GPX/JSON 模板')
        vals = resample_profile(profile, n)
        if rng_range is not None:
            vals = _map_profile(vals, *rng_range)
        # Keep a tiny sensor jitter, but preserve the template's real shape.
        # Clamp after rounding so the requested min/max is never exceeded by
        # the 0.05 m jitter (the UI promises a band).
        out = []
        for v in vals:
            v = round(v + rng.gauss(0.0, 0.05), 1)
            if rng_range is not None:
                v = min(rng_range[1], max(rng_range[0], v))
            out.append(v)
        return out

    return _altitudes(n, rng, base=base, distance_km=distance_km)


def _apply_gps_jitter(pts: list[dict], rng, rho: float = 0.72,
                      sigma_m: float = 0.75) -> None:
    """Correlated GPS error, in metres.

    `src/track/generator.rs`:
        jx = 0.72 * jx + gauss(0, 0.75)
        jy = 0.72 * jy + gauss(0, 0.75)
        px = base_x + jx,  py = base_y + jy

    White noise (the old uniform(-4 m, 4 m)) both looks jagged and inflates the
    measured path length: consecutive samples get ~2.5 m of independent
    displacement added on top of the real step.
    """
    jx = jy = 0.0
    for p in pts:
        jx = rho * jx + rng.gauss(0.0, sigma_m)
        jy = rho * jy + rng.gauss(0.0, sigma_m)
        p['lat'] = round(p['lat'] + jy / 110540.0, 7)
        p['lng'] = round(p['lng'] + jx /
                         (111320.0 * math.cos(math.radians(p['lat']))), 7)


def _fit_length(pts: list[dict], target_m: float, rounds: int = 4) -> None:
    """Scale the sampled track about its centroid until it spans `target_m`.

    Sampling a polyline at a fixed interval measures **chords**, not arcs, so a
    route with sharp corners comes out shorter than the distance it was built
    for (a 4-point square lost 4%).  Stretching it back keeps `totalDis` and the
    coordinates consistent, which is the only property we can actually control.
    """
    if not pts or target_m <= 0 or len(pts) < 2:
        return
    cur = sum(math.hypot((b['lat'] - a['lat']) * 110540,
                         (b['lng'] - a['lng']) * 111320
                         * math.cos(math.radians(a['lat'])))
              for a, b in zip(pts, pts[1:]))
    if cur <= 0:
        return
    lat0 = sum(p['lat'] for p in pts) / len(pts)
    lng0 = sum(p['lng'] for p in pts) / len(pts)
    for _ in range(rounds):
        k = target_m / cur
        if abs(k - 1.0) < 0.002:
            break
        for p in pts:
            p['lat'] = lat0 + (p['lat'] - lat0) * k
            p['lng'] = lng0 + (p['lng'] - lng0) * k
        cur = sum(math.hypot((b['lat'] - a['lat']) * 110540,
                             (b['lng'] - a['lng']) * 111320
                             * math.cos(math.radians(a['lat'])))
                  for a, b in zip(pts, pts[1:]))


def generate_route(center_lat: float, center_lng: float, radius_deg: float = 0.0006,
                   rng: random.Random | None = None,
                   waypoints: list | None = None,
                   n_points: int | None = None,
                   target_m: float | None = None,
                   alt_base: float = 25.0,
                   alt_mode: str = 'auto',
                   alt_range: list | tuple | None = None,
                   alt_profile: list[float] | None = None,
                   closed: bool = True,
                   dist_fractions: list[float] | None = None,
                   start_offset_m: float = 0.0) -> list[dict]:
    """Track points.

    ``waypoints`` routes the track through the checkpoints.  ``closed`` keeps
    the legacy loop/repeat behaviour; false keeps the drawn start-to-end
    polyline open (it is stretched to the target distance, never joined back
    to the first point).  Every waypoint is snapped so it is passed within
    ~20 m (the official `isPass` threshold is 25 m).

    ``dist_fractions`` places each sample at that fraction of the distance
    (instead of even spacing), so a run can speed up and slow down.
    ``start_offset_m`` moves the start along a closed loop by 20-50 m so every
    run does not begin at the exact same coordinate.

    ``alt_mode`` / ``alt_range`` / ``alt_profile`` select the altitude source:
    synthetic OU (default), one fixed value, a synthetic profile mapped into a
    range, or a local GPX/JSON profile.
    """
    rng = rng or random
    n_points = int(n_points or N_POINTS)
    for _ in range(25):
        now_ms = int(time.time() * 1000)
        pts = []
        if waypoints and len(waypoints) >= 2:
            poly = (build_loop(waypoints, target_m or 0) if closed
                    else build_open(waypoints, target_m or 0))
            if not closed and target_m:
                route_len = _polyline_len(list(waypoints))
                if float(target_m) + 1.0 < route_len:
                    raise ValueError(
                        '路线长度 %.0fm 超过目标距离 %.0fm；请增大距离或缩短路线'
                        % (route_len, target_m))
            if closed:
                one_lap = build_loop(waypoints, 0)
                start = _loop_start_for_waypoints(
                    one_lap, float(target_m or 0), rng,
                    preferred_m=start_offset_m)
                if start is None:
                    start = start_offset_m
                if start and start > 0:
                    poly = _rotate_polyline(poly, start)
            poly = _trim_polyline(poly, float(target_m or 0))
            sampled = _sample_loop(poly, n_points, target_m, rng,
                                   dist_fractions=dist_fractions)
            if not sampled:
                return generate_route(center_lat, center_lng, radius_deg, rng,
                                      n_points=n_points, target_m=target_m,
                                      alt_base=alt_base, alt_mode=alt_mode,
                                      alt_range=alt_range,
                                      alt_profile=alt_profile,
                                      closed=closed,
                                      dist_fractions=dist_fractions,
                                      start_offset_m=start_offset_m)
            for i, (lat, lng) in enumerate(sampled):
                pts.append({
                    'lat': lat,
                    'lng': lng,
                    'ts': now_ms + i * 36_000,
                    'alt': 0.0,
                    'index': i,
                })
        else:
            # Fallback loop: a small circle repeated until it covers the target.
            # The old version drew exactly one circle of a fixed radius and
            # ignored target_m entirely, so a 5 km run had the same 2.1 km
            # geometry as a 2.5 km one.
            r_m = radius_deg * 110540.0
            coslat = math.cos(math.radians(center_lat))
            base = []
            for k in range(64):
                a = 2 * math.pi * k / 64
                base.append((center_lat + r_m * math.sin(a) / 110540.0,
                             center_lng + r_m * math.cos(a) / (111320.0 * coslat)))
            target = float(target_m or 0) or (2 * math.pi * r_m)
            poly = build_loop(base, target)
            if closed and start_offset_m > 0:
                poly = _rotate_polyline(poly, start_offset_m)
            poly = _trim_polyline(poly, target)
            sampled = _sample_loop(poly, n_points, target, rng,
                                   dist_fractions=dist_fractions)
            if not sampled:
                return generate_route(center_lat, center_lng, radius_deg, rng,
                                      n_points=n_points, target_m=target_m,
                                      alt_base=alt_base, alt_mode=alt_mode,
                                      alt_range=alt_range,
                                      alt_profile=alt_profile,
                                      closed=closed,
                                      dist_fractions=dist_fractions,
                                      start_offset_m=start_offset_m)
            for i, (lat, lng) in enumerate(sampled):
                pts.append({
                    'lat': lat,
                    'lng': lng,
                    'ts': now_ms + i * 36_000,
                    'alt': 0.0,
                    'index': i,
                })

        # stretch the sampled chords back to the requested distance, then add
        # GPS error, then re-snap the checkpoints so isPass still holds
        _fit_length(pts, float(target_m or 0))
        _apply_gps_jitter(pts, rng)
        if waypoints:
            _snap_to_waypoints(pts, waypoints)
            for i, p in enumerate(pts):
                p['index'] = i

        alts = make_altitudes(len(pts), rng, base=alt_base, mode=alt_mode,
                              alt_range=alt_range, profile=alt_profile,
                              distance_km=(float(target_m) / 1000.0
                                           if target_m else None))
        for p, a in zip(pts, alts):
            p['alt'] = a
        if _adjacent_ok(pts):
            return pts
    return pts


def _pace_keys(seg_m: list[float], seg_t: list[int]) -> list[int]:
    """Displayed pace in whole seconds per kilometre (what the table shows).

    ``fmtPace`` in the browser rounds half up (Math.round); Python's round()
    is half-to-even, so 360.5 s/km became 360 here but 6'01" on screen.
    """
    return [int(math.floor(seg_t[i] / max(1e-6, float(seg_m[i]) / 1000.0)
                            + 0.5))
            for i in range(len(seg_t))]


def _distinct_paces(seg_m: list[float], seg_t: list[int],
                    total: int | None = None) -> list[int]:
    """Give every lap a different displayed pace, without moving the duration.

    The old helper compared raw seconds, so a 0.5 km tail at 180 s and a full
    lap at 360 s both read 6'00"/km.  This walks duplicate groups and moves one
    lap by a whole number of *pace* seconds, taking the same split seconds back
    from the longest lap so the sum (and the run duration) never changes.

    The move is expressed in s/km rather than split seconds because a second
    on a 0.5 km tail is 2 s/km, and on a 0.1 km tail it is 10 s/km -- moving
    the tail by "one second" used to turn 6'00" into 12'02".

    A wider spread is nicer, but whole seconds are the only unit the split
    table shows, so the hard rule is that no two laps may display the same
    pace; asking for a 2 s/km gap is not always reachable.
    """
    out = [max(1, int(round(v))) for v in seg_t]
    n = len(out)
    if total is not None and n:
        # A 1 m tail rounds to 1 s by itself; taking the rounding error back
        # from the longest lap keeps sum(splits) == duration even then.
        diff = int(total) - sum(out)
        if diff:
            k = max(range(n), key=lambda i: out[i])
            out[k] = max(1, out[k] + diff)
    if n < 2:
        return out
    km = [max(1e-6, float(m) / 1000.0) for m in seg_m]
    for _ in range(n * 6 + 12):
        keys = _pace_keys(seg_m, out)
        seen = {}
        pair = None
        for i, k in enumerate(keys):
            if k in seen:
                pair = (seen[k], i)
                break
            seen[k] = i
        if pair is None:
            break
        i, j = pair
        # The shorter lap changes pace the fastest per split second, but a
        # very short tail cannot move by less than 1 s; try both members.
        order = [i, j] if km[i] <= km[j] else [j, i]
        moved = False
        for mag in range(1, n * 2 + 1):
            if moved:
                break
            for sign in (1, -1):
                dpace = sign * mag
                for k in order:
                    dsec = int(round(abs(dpace) * km[k])) or 1
                    dsec = dsec if dpace > 0 else -dsec
                    nk = out[k] + dsec
                    if nk < 1:
                        continue
                    for c in sorted(range(n), key=lambda x: -out[x]):
                        if c == k:
                            continue
                        nc = out[c] - dsec
                        if nc < 1:
                            continue
                        trial = list(out)
                        trial[k] = nk
                        trial[c] = nc
                        if len(set(_pace_keys(seg_m, trial))) > len(set(keys)):
                            out = trial
                            moved = True
                            break
                    if moved:
                        break
                if moved:
                    break
        if not moved:
            break
    return out


def _km_cadence_factors(n_km: int, rng) -> list[float]:
    """One multiplicative cadence level per kilometre.

    Steps used to be a pure function of distance, so every lap divided back to
    the same 150.0 spm even though the stride column moved.  Adjacent
    kilometres are forced at least 2% apart so the table never looks copied.
    """
    n_km = max(1, int(n_km))
    out = []
    prev = 1.0
    for k in range(n_km):
        f = min(1.04, max(0.96, 1.0 + rng.gauss(0.0, 0.025)))
        if k:
            gap = f - prev
            if abs(gap) < 0.008:
                step = 0.008 if gap >= 0.0 else -0.008
                f = prev + step
                if not (0.96 <= f <= 1.04):
                    f = prev - step
        out.append(f)
        prev = f
    return out


def _km_pace_factors(n_km: int, rng) -> list[float]:
    """One multiplicative pace factor per kilometre.

    Four laps that all read exactly 6'00" are the clearest "generated" tell in
    the record.  Real splits wander: warm-up, a hill, a light, a push at the
    end.  The walk is AR(1)-ish around 1.0 (adjacent kilometres still
    correlate) with a hard floor on the neighbour gap, then renormalised so
    the mean stays 1.0.  The caller rescales the draws to the requested total,
    so a wide spread costs nothing in the header.
    """
    n_km = max(1, int(n_km))
    out = []
    prev = 1.0
    for k in range(n_km):
        f = min(1.12, max(0.88, 1.0 + rng.gauss(0.0, 0.060)))
        if n_km >= 3 and k == 0:
            f += rng.uniform(0.035, 0.080)       # warm-up lap
        if n_km >= 3 and k == n_km - 1:
            f -= rng.uniform(0.015, 0.055)       # push / fade at the end
        if k:
            gap = f - prev
            if abs(gap) < 0.030:
                step = 0.030 if gap >= 0.0 else -0.030
                f = prev + step
                if not (0.88 <= f <= 1.12):
                    f = prev - step
        out.append(f)
        prev = f
    mean = sum(out) / len(out)
    return [f / mean for f in out]


def generate_run_data(distance_km: float = 2.5, pace_min_per_km: float = 6.0,
                      cadence: int = 150, center_lat: float | None = None,
                      center_lng: float | None = None,
                      rng: random.Random | None = None,
                      waypoints: list | None = None,
                      n_points: int | None = None,
                      sample_seconds: float = 3.0,
                      checkpoints: list | None = None,
                      weight_kg: float = health.DEFAULT_WEIGHT_KG,
                      altitude_base: float = 25.0,
                      altitude_mode: str = 'auto',
                      altitude_range: list | tuple | None = None,
                      altitude_profile: list[float] | None = None,
                      start_ms: int | None = None,
                      closed: bool = True,
                      run_uuid: str | None = None,
                      km_times: list[float] | None = None,
                      run_area_id: int | None = None,
                      sequential: bool = False) -> dict:
    """Full forged record. 2.5km default mirrors the verified mock sample.

    waypoints=[(lat,lng),...] routes through those checkpoints; ``closed``
    repeats a loop until the target distance is covered, while false keeps the
    drawn start-to-end polyline open.
    n_points defaults to one sample every `sample_seconds` (the app's own
    cadence), floored at 25.

    ``start_ms`` pins the run to an explicit Beijing timestamp (the backfill
    feature).  When omitted the historical behaviour is kept: the run ends
    1-5 s before `now`, which is what the reference client does.

    ``altitude_mode``:
      * ``auto`` – synthetic OU barometer around ``altitude_base``;
      * ``fixed`` – every point at ``altitude_base`` (zero climb);
      * ``range`` – synthetic profile mapped into ``altitude_range``;
      * ``template`` – ``altitude_profile`` (parsed from local GPX/JSON),
        optionally mapped into ``altitude_range``.

    ``km_times`` is the edited split table: seconds per kilometre, one entry
    per full kilometre plus the remainder (``ceil(distance_km)`` entries).
    When present it fixes the duration at the sum of the entries.  Without it
    each kilometre is drawn independently around ``pace_min_per_km`` and the
    draws are rescaled so the duration is exactly ``distance * pace``.

    ``sequential`` keeps the checkpoints in the school's point-number order;
    the official ``RunSeqLocationManager`` consumes them from index 0, so
    reordering them to put mandatory points first would break the run.
    """
    # There is deliberately no built-in campus here.  Callers either pass the
    # account's real fence centre or a route whose first point is the centre;
    # silently defaulting to 江门/厦门 wrote accepted records in the wrong city.
    if center_lat is None or center_lng is None:
        if waypoints:
            center_lat = float(waypoints[0][0])
            center_lng = float(waypoints[0][1])
        else:
            raise ValueError(
                'center_lat/center_lng required: no built-in campus')
    center_lat, center_lng = float(center_lat), float(center_lng)
    if not (-90.0 <= center_lat <= 90.0 and -180.0 <= center_lng <= 180.0):
        raise ValueError('center_lat/center_lng out of range')
    if abs(center_lat) < 1e-6 and abs(center_lng) < 1e-6:
        raise ValueError('center_lat/center_lng is the 0/0 sentinel')
    rng = rng or random
    if not (distance_km > 0):
        raise ValueError('distance_km must be > 0 (got %r)' % (distance_km,))
    if not (pace_min_per_km > 0):
        raise ValueError('pace_min_per_km must be > 0 (got %r)'
                         % (pace_min_per_km,))
    if not (cadence > 0):
        raise ValueError('cadence must be > 0 (got %r)' % (cadence,))
    total_meters = int(distance_km * 1000)

    # ---- per-kilometre targets -------------------------------------------
    # ``km_times`` (seconds per kilometre, one entry per full km plus the
    # remainder) is what the UI's split editor sends back.  Without it each
    # kilometre gets its own random draw centred on the requested pace.  Either
    # way the run duration is the sum of the split times, and the distance is
    # the sum of the segment lengths.
    n_km = max(1, int(math.ceil(distance_km - 1e-9)))
    seg_m = [1000.0] * n_km
    seg_m[-1] = max(1.0, float(total_meters) - 1000.0 * (n_km - 1))
    if km_times is not None:
        vals = [float(x) for x in km_times]
        if len(vals) != n_km:
            raise ValueError(
                'km_times needs %d entries for %.2f km (got %d)'
                % (n_km, distance_km, len(vals)))
        if any(not math.isfinite(v) or v <= 0 for v in vals):
            raise ValueError('km_times must be positive seconds')
        total_seconds = int(round(sum(vals)))
        if total_seconds <= 0:
            raise ValueError('km_times sum rounds to 0 s')
        seg_t = [max(1, int(round(v))) for v in vals]
        seg_t[-1] = max(1, seg_t[-1] + total_seconds - sum(seg_t))
        seg_t = _distinct_paces(seg_m, seg_t, total_seconds)
    else:
        target = int(distance_km * pace_min_per_km * 60)
        if target <= 0:
            raise ValueError('duration rounds to 0 s (distance*pace too small)')
        base_t = [m / 1000.0 * pace_min_per_km * 60.0 for m in seg_m]
        raw_t = [t * f for t, f in zip(base_t, _km_pace_factors(n_km, rng))]
        # Each kilometre is its own random draw (up to +-12%, so the splits
        # visibly differ); they are then scaled together so the sum is exactly
        # the duration the pace field promises.
        scale = target / max(1e-9, sum(raw_t))
        seg_t = [max(1, int(round(t * scale))) for t in raw_t]
        seg_t[-1] = max(1, seg_t[-1] + target - sum(seg_t))
        seg_t = _distinct_paces(seg_m, seg_t, target)
    total_seconds = sum(seg_t)
    pace_min_per_km = total_seconds / 60.0 / max(1e-9, distance_km)
    speed = round(float(total_meters) / total_seconds, 2)
    total_steps = int(total_seconds / 60 * cadence)

    if n_points is None:
        n_points = max(N_POINTS, int(round(total_seconds / max(0.5, sample_seconds))))
    n_points = int(n_points)

    # ---- pace profile (1 s clock, 10 s windows) ---------------------------
    # Built BEFORE the track: the sampled points must follow the same
    # acceleration / fatigue curve.  The old order produced a perfectly
    # constant per-point speed (6.5941 km/h for all 444 points), which is a
    # stronger synthetic signal than any single header field.
    #
    # The deviation is an OU process, not two fixed sine waves.  The sines
    # were at 3 and 7 cycles per run no matter how long the run was, which put
    # 19% and 11% of the spectrum on two exact frequencies -- a fingerprint
    # that survives any duration.  Upstream PR #134 uses a 24 s correlation
    # target for pace; at 10 s buckets that is phi = exp(-10/24) = 0.66.
    n_buckets = max(1, int(math.ceil(total_seconds / float(SEG_SECONDS))))
    avg_mps = total_meters / max(1, total_seconds)
    phi = math.exp(-SEG_SECONDS / PACE_TAU_S)
    innovation = PACE_SIGMA * math.sqrt(1.0 - phi * phi)
    win_phi = math.exp(-SEG_SECONDS / WINDOW_TAU_S)
    win_sigma = min(0.55, max(0.15, WINDOW_SIGMA
                                  * (pace_min_per_km / 6.0) ** 1.5))
    win_innovation = win_sigma * math.sqrt(1.0 - win_phi * win_phi)

    seg_bounds = [0]
    for t in seg_t:
        seg_bounds.append(seg_bounds[-1] + t)
    k_of = [0] * total_seconds
    for k in range(n_km):
        for j in range(seg_bounds[k], seg_bounds[k + 1]):
            k_of[j] = k

    shapes = [1.0] * total_seconds
    pace_dev = 0.0
    window_dev = 0.0
    for j in range(total_seconds):
        if j % SEG_SECONDS == 0:
            pace_dev = phi * pace_dev + rng.gauss(0.0, innovation)
            window_dev = (win_phi * window_dev
                          + rng.gauss(0.0, win_innovation))
        t = j / max(1, total_seconds - 1)
        ramp = 0.86 + 0.14 * min(1.0, t / 0.06)
        fatigue = 1.0 - 0.06 * t
        # A negative window can otherwise push (1 + window_dev) below zero at
        # slow paces (sigma grows with pace); a negative speed second made
        # totalDis go backwards and silently forced laps back to a time-linear
        # distance.
        shapes[j] = max(0.0, ramp * fatigue
                        * (1.0 + pace_dev) * (1.0 + window_dev))

    # ---- stops -----------------------------------------------------------
    # Real traces are bimodal: either no pause at all, or ~6% of the 10 s
    # windows under 1 m (a light or a crossing).  The generator used to emit a
    # run that never stopped.
    if total_seconds >= 80 and rng.random() < 0.55:
        for _ in range(rng.randint(1, 3)):
            start = rng.randint(max(1, total_seconds // 10),
                                max(1, total_seconds - 30))
            for j in range(start, min(total_seconds,
                                      start + rng.randint(12, 30))):
                shapes[j] *= rng.uniform(0.0, 0.04)

    # Normalise inside each kilometre so it covers exactly ``seg_m[k]`` metres
    # in ``seg_t[k]`` seconds.  A pause is absorbed by the rest of its own
    # split instead of shifting the requested total, and the split table the
    # operator edits is what the record actually reports.
    norm = [0.0] * n_km
    for j in range(total_seconds):
        norm[k_of[j]] += shapes[j]
    for k in range(n_km):
        span = max(1, seg_bounds[k + 1] - seg_bounds[k])
        norm[k] = max(1e-9, norm[k] / span)

    cum_before = [0.0]
    for j in range(total_seconds):
        k = k_of[j]
        v = (seg_m[k] / seg_t[k]) * shapes[j] / norm[k]
        cum_before.append(cum_before[-1] + v)
    drift = total_meters - cum_before[-1]
    if abs(drift) > 1e-6:
        # Float accumulation only; spread it evenly so the kilometre knots
        # move by a fraction of a millimetre, never by a step.
        for j in range(1, total_seconds + 1):
            cum_before[j] += drift * j / total_seconds

    bucket_m = []
    for i in range(n_buckets):
        a = i * SEG_SECONDS
        b = min(a + SEG_SECONDS, total_seconds)
        bucket_m.append(cum_before[b] - cum_before[a])

    def dist_at(sec):
        if sec <= 0:
            return 0.0
        if sec >= total_seconds:
            return cum_before[-1]
        i = int(sec)
        frac = sec - i
        return cum_before[i] + (cum_before[i + 1] - cum_before[i]) * frac

    def frac_at(sec):
        return dist_at(sec) / max(0.001, cum_before[-1])

    # ---- sample times: 2-4 s apart, normalised to the run duration --------
    # Real device samples land every 2.2-3.4 s; the old fixed 5 s grid left
    # roughly half the points a phone would have recorded.
    intervals = [rng.uniform(2.2, 3.8) for _ in range(max(0, n_points - 1))]
    if intervals:
        k = total_seconds / max(1e-6, sum(intervals))
        times = [0.0]
        for w in intervals:
            times.append(times[-1] + w * k)
        times[-1] = float(total_seconds)
    else:
        times = [0.0]
    fracs = [frac_at(t) for t in times]
    fracs[0], fracs[-1] = 0.0, 1.0

    locs = generate_route(
        center_lat, center_lng, rng=rng, waypoints=waypoints,
        n_points=n_points, target_m=total_meters,
        alt_base=altitude_base, alt_mode=altitude_mode,
        alt_range=altitude_range, alt_profile=altitude_profile,
        closed=closed,
        dist_fractions=fracs,
        start_offset_m=(rng.uniform(20.0, 50.0) if closed else 0.0))
    n_locs = len(locs)

    # geometry -> cumulative distance -> time; this survives checkpoint
    # snapping inserting an extra point and keeps distance/time consistent.
    geo = [0.0]
    for a, b in zip(locs, locs[1:]):
        geo.append(geo[-1] + math.hypot(
            (b['lat'] - a['lat']) * 110540.0,
            (b['lng'] - a['lng']) * 111320.0
            * math.cos(math.radians(a['lat']))))
    total_geo = max(0.001, geo[-1])
    grid = [i * 0.5 for i in range(int(total_seconds / 0.5) + 1)]
    grid_d = [dist_at(t) for t in grid]

    def time_at_frac(f):
        target = max(0.0, min(1.0, f)) * total_meters
        lo, hi = 0, len(grid) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if grid_d[mid] < target:
                lo = mid + 1
            else:
                hi = mid
        if lo <= 0:
            return 0.0
        d0, d1 = grid_d[lo - 1], grid_d[lo]
        t0g, t1g = grid[lo - 1], grid[lo]
        if d1 <= d0:
            return t1g
        return t0g + (t1g - t0g) * (target - d0) / (d1 - d0)

    fracs_geo = [g / total_geo for g in geo]
    fracs_geo[-1] = 1.0
    if len(locs) == len(times):
        # The sample clock is the truth.  Re-deriving it from the jittered
        # geometry collapsed every stop (all the points share one position, so
        # they all mapped to the same instant) and left 1 s / 23 s gaps around
        # a traffic light.  ``times`` already carries the 2-4 s device grid.
        point_times = list(times)
    else:
        # A checkpoint snap inserted a point: fall back to the geometric
        # mapping so the inserted sample still lands between its neighbours.
        point_times = [time_at_frac(f) for f in fracs_geo]

    # retime points across the run duration; last point lands EXACTLY on
    # t0 + total_ms so that stopTime == last ten-second bucket's endTime.
    #
    # The run ends 1-5 s before the submission, not at the exact instant of it:
    # the reference starts at `now - dur - 5 s` and then adds a 0-4 s jitter
    # (src/api/flow.rs L184), which puts stopTime 1-5 s in the past.  A stopTime
    # identical to the request timestamp is a fingerprint in itself.
    total_ms_run = total_seconds * 1000
    if start_ms is None:
        t0 = int(time.time() * 1000) - total_ms_run - rng.randint(1000, 5000)
    else:
        t0 = int(start_ms)
        if t0 <= 0:
            raise ValueError('start_ms must be a positive epoch (got %r)'
                             % (start_ms,))
    for i, p in enumerate(locs):
        p['ts'] = t0 + int(round(point_times[i] * 1000))
    for i in range(1, n_locs):
        if locs[i]['ts'] <= locs[i - 1]['ts']:
            locs[i]['ts'] = locs[i - 1]['ts'] + 1000
        elif i < n_locs - 1 and locs[i]['ts'] - locs[i - 1]['ts'] < 2000:
            # A snapped checkpoint can land less than a second after its
            # neighbour; real samples are 2-4 s apart, so keep a 2 s floor.
            locs[i]['ts'] = locs[i - 1]['ts'] + 2000
    locs[-1]['ts'] = t0 + total_ms_run
    # The 2 s floor is best-effort: when the last samples are crowded, it can
    # push a point past the fixed stopTime and leave the final gap negative.
    # Pull the interior points back instead of moving the end of the run.
    for i in range(n_locs - 2, -1, -1):
        if locs[i]['ts'] >= locs[i + 1]['ts']:
            locs[i]['ts'] = locs[i + 1]['ts'] - 1000

    # SpeedPerTenSec: {beginTime,distance,endTime,flag,id,queueNum,state}
    speed_json = []
    # StepsPerTenSec: {stepsNum,beginTime,endTime,flag,id,maxDiff,minDiff,avgDiff,state,queueNum}
    step_freq_json = []
    # Steps follow the same distance profile (a stopped window has no steps)
    # plus a slow cadence drift.  The old +-2 steps per window made the step
    # column a rescale of the distance column with almost no variance of its
    # own (CV 0.06-0.11; real traces sit at 0.18-0.36).
    per_seg_steps = cadence * SEG_SECONDS / 60.0
    mean_bucket = max(1e-6, sum(bucket_m) / n_buckets)
    cad_phi = math.exp(-SEG_SECONDS / CADENCE_TAU_S)
    cad_innovation = CADENCE_SIGMA * math.sqrt(1.0 - cad_phi * cad_phi)
    cad_dev = 0.0
    # Each kilometre gets its own cadence level, and stride follows the pace:
    # speed = cadence * stride, and real runners take most of a pace change in
    # stride, not cadence.  With stride ~ speed^0.8 the lap cadence only moves
    # as speed^0.2, plus the independent per-km flavour.  Steps used to be a
    # pure function of distance, so every lap divided back to exactly 150.0 spm
    # while the stride column still moved.
    avg_speed = total_meters / max(1, total_seconds)
    cad_flavour = _km_cadence_factors(n_km, rng)
    stride_km = [((seg_m[k] / seg_t[k]) / avg_speed) ** 0.8
                 for k in range(n_km)]
    raw_s = []
    for i in range(n_buckets):
        cad_dev = cad_phi * cad_dev + rng.gauss(0.0, cad_innovation)
        ratio = bucket_m[i] / mean_bucket
        k = k_of[min(total_seconds - 1, i * SEG_SECONDS)]
        step = (per_seg_steps * ratio / max(0.5, stride_km[k])
                * cad_flavour[k] * (1.0 + cad_dev))
        step *= 1.0 + rng.gauss(0.0, 0.05)
        raw_s.append(max(0.0, step))
    s_scale = total_steps / max(1e-6, sum(raw_s))
    stop_ms = t0 + total_seconds * 1000
    for i in range(n_buckets):
        b = t0 + i * SEG_SECONDS * 1000
        # upstream clamps the last window to totalTime (wire.rs build_windows)
        e = min(b + SEG_SECONDS * 1000, stop_ms)
        speed_json.append({
            'beginTime': b,
            'endTime': e,
            'distance': round(bucket_m[i], 4),
            'flag': t0,
            'id': 0,
            'queueNum': 0,
            'state': 0,
        })
        step_freq_json.append({
            'stepsNum': int(round(raw_s[i] * s_scale)),
            'beginTime': b,
            'endTime': e,
            'flag': t0,
            'id': 0,
            # these are the reference's sentinels for "not computed"
            'maxDiff': 0.0,
            'minDiff': 1000.0,
            'avgDiff': 0.0,
            'state': 0,
            'queueNum': 0,
        })
    # absorb rounding drift so sum(stepsNum) == totalSteps exactly
    if step_freq_json:
        # A stop window (a light, a breather) still picked up one or two
        # steps from the old distribution, which the chart drew as a 6 spm
        # cliff.  Zero the step counter where the phone did not move; the
        # drift loop below puts those steps back on the running windows.
        dist_by_begin = {int(s['beginTime']): float(s.get('distance') or 0.0)
                         for s in speed_json}
        for item in step_freq_json:
            if dist_by_begin.get(int(item['beginTime']), 0.0) < 0.6:
                item['stepsNum'] = 0
        # Real phone step counters occasionally drop a window (the official
        # chart has 36 spm valleys) and occasionally spike.  Cap the spike
        # first, then punch 2-4 dropouts and spread the removed steps over the
        # other windows so the total does not move.
        for item in step_freq_json:
            item['stepsNum'] = min(34, max(0, int(item['stepsNum'])))
        if len(step_freq_json) >= 12:
            picks = rng.sample(range(2, len(step_freq_json) - 2),
                               k=min(len(step_freq_json) // 40 + 2, 4))
            for idx in picks:
                old = int(step_freq_json[idx]['stepsNum'])
                if old <= 2:
                    continue
                drop = max(1, min(old - 1, int(round(old * rng.uniform(0.35, 0.55)))))
                step_freq_json[idx]['stepsNum'] = old - drop
        drift = total_steps - sum(x['stepsNum'] for x in step_freq_json)
        if drift:
            # Rounding loses a fraction per window; with 100+ windows that is
            # dozens of steps.  The old round-robin handed them back one per
            # window from index 0, including the stop windows we just zeroed --
            # which is exactly how a 10 s stop turned into 1 step / 6 spm.
            # Spread the remainder over windows that actually moved instead.
            moving = [i for i in range(len(step_freq_json))
                      if dist_by_begin.get(int(step_freq_json[i]['beginTime']),
                                           0.0) >= 0.6]
            if not moving:
                moving = list(range(len(step_freq_json)))
            if drift > 0:
                for n in range(drift):
                    step_freq_json[moving[n % len(moving)]]['stepsNum'] += 1
            else:
                order = sorted(moving,
                               key=lambda i: -step_freq_json[i]['stepsNum'])
                for n in range(-drift):
                    i = order[n % len(order)]
                    if step_freq_json[i]['stepsNum'] > 0:
                        step_freq_json[i]['stepsNum'] -= 1
    #                   intervalStepModel,originAvgStep,speedStandard,startTime,state}
    seg_s = max(1, total_seconds // 5)
    segment_json = [{
        'avgSpeed': round(speed, 3),
        'avgStep': cadence,
        'distance': round(total_meters / 5.0, 3),
        'startTime': t0 + i * seg_s * 1000,
        'endTime': t0 + (i + 1) * seg_s * 1000,
        'flag': 0,
        'id': 0,
        'intervalStepModel': 0,
        'originAvgStep': cadence,
        'speedStandard': 0,
        'state': 0,
    } for i in range(5)]

    # FivePoint: {flag,id,isFixed,isPass,lat,lon,pointName,state,glat,glon,position}
    #
    # When the live checkpoints are known they become the five points, mandatory
    # ones first: that is what makes the app replay draw green ticks at the
    # right places instead of an empty map (see issue #85 upstream).
    if checkpoints:
        # Upstream sends one entry per live checkpoint (wire.rs::five_point_payload
        # maps the whole list) — the name "FivePoint" describes the usual campus,
        # not a fixed count.  Truncating to 5 used to drop points silently and
        # made schools with any other count fail validation.
        #
        # A sequential school must keep the school's numbering: the official
        # `SportRecord.getFivePoint()` sorts by `position` and
        # `RunSeqLocationManager` consumes index 0.  Only the random mode may
        # put mandatory points first.
        if sequential:
            chosen = _order_checkpoints(checkpoints)
        else:
            chosen = ([c for c in checkpoints if c.get('isMustPoint')]
                      + [c for c in checkpoints if not c.get('isMustPoint')])
        five_point = []
        for k, c in enumerate(chosen):
            gcj = (float(c['lat']), float(c['lon']))
            bd = (float(c.get('bd_lat') if c.get('bd_lat') is not None
                        else coords.gcj02_to_bd09(*gcj)[0]),
                  float(c.get('bd_lon') if c.get('bd_lon') is not None
                        else coords.gcj02_to_bd09(*gcj)[1]))
            raw_fixed = c.get('isFixed')
            if isinstance(raw_fixed, str):
                fixed = 1 if raw_fixed.strip().lower() in (
                    '1', 'true', 'yes', 'y', '是') else 0
            else:
                try:
                    fixed = int(raw_fixed) if raw_fixed is not None else (
                        1 if c.get('isMustPoint') else 0)
                except (TypeError, ValueError):
                    fixed = 1 if c.get('isMustPoint') else 0
            five_point.append({
                'flag': locs[0]['ts'],
                'id': c.get('pid') if c.get('pid') is not None else k + 1,
                'isFixed': fixed,
                # Official `RunRanLocationManager.passPoint()` ticks a point at
                # 25 m; the track must actually come that close, not 40 m.
                'isPass': _passes_near(locs, gcj[0], gcj[1], 25.0),
                'lat': round(bd[0], 6),
                'lon': round(bd[1], 6),
                'pointName': c.get('name') or '',
                'state': 0,
                'glat': round(gcj[0], 6),
                'glon': round(gcj[1], 6),
                # Real-device fivePointJson uses the array index (0,1,2,…),
                # not the server's checkpoint position and never 999
                # (upstream PR #106).
                'position': k,
            })
    else:
        step = max(1, (n_locs - 1) // 5)
        idxs = sorted({min(n_locs - 1, k * step) for k in range(5)})
        five_point = []
        for k, i in enumerate(idxs[:5]):
            gcj = (locs[i]['lat'], locs[i]['lng'])
            bd = coords.gcj02_to_bd09(*gcj)
            five_point.append({
                'flag': locs[0]['ts'],
                'id': k + 1,
                'isFixed': 0,
                'isPass': _passes_near(locs, gcj[0], gcj[1], 25.0),
                'lat': round(bd[0], 6),
                'lon': round(bd[1], 6),
                'pointName': '',
                'state': 0,
                'glat': round(gcj[0], 6),
                'glon': round(gcj[1], 6),
                'position': k,
            })

    # Checkpoint passes, recorded the way the official client records them.
    #
    # `passPointAndBackType()` in RunRandomPImpl / RunSequencePImpl returns 1
    # or 2 only when the track actually enters a checkpoint ring
    # (`isFixed == 1 ? 2 : 1`); BaseRunPImpl and BaseDueRunPImpl always return
    # 0.  Nothing else writes a type: the complete domain this client emits is
    # {0 normal, 1/2 checkpoint, 5 start, 6 end, -1 dropped by the history
    # page}.
    #
    # The old code scattered [1, 1, 2, 8] over the last third on a 6% coin
    # flip.  That asserted "checkpoint passed" at places the track never
    # visited, and 8 is not a value any implementation produces -- an unknown
    # type falls through the map SDK's fallback and paints grey (the "灰线"
    # upstream issue #103).
    checkpoint_marks = {}
    for fp in five_point:
        if not fp.get('isPass'):
            continue
        idx = _nearest_index(locs, fp['glat'], fp['glon'])
        if idx <= 1 or idx >= len(locs) - 1:     # start/end sentinels win
            continue
        checkpoint_marks[idx] = 2 if fp.get('isFixed') else 1

    # PointJsonEntity: {fivePointJson,freedomShowFence,geoFencesJson,runAreaId,useZip}
    point_json_entity = {
        'useZip': False,
        'fivePointJson': json.dumps(five_point, separators=(',', ':')),
        'runAreaId': int(run_area_id) if run_area_id is not None else -1,
        'geoFencesJson': None,
        'freedomShowFence': False,
    }

    # MyLocation track points — the full 28-key shape of wire.rs::conv_point.
    # Two things that used to be wrong here:
    #   * totalTime / validTime are SECONDS (GenPoint.totalTime = t_acc), not ms
    #   * lat/lng are BD-09 while gLat/gLng are GCJ-02 (they differ by ~950 m)
    deltas_m, deltas_s = [], []
    for i in range(1, len(locs)):
        deltas_m.append(max(0.0, geo[i] - geo[i - 1]))
        deltas_s.append(max(0.5, (locs[i]['ts'] - locs[i - 1]['ts']) / 1000.0))
    raw_steps = [max(0.0, cadence / 60.0 * dt + rng.gauss(0.0, 0.35))
                 for dt in deltas_s]
    st_scale = total_steps / max(1e-6, sum(raw_steps))
    cum_steps = [0.0]
    for x in raw_steps:
        cum_steps.append(cum_steps[-1] + x * st_scale)

    loc_points = []
    for i, p in enumerate(locs):
        # Distance comes from the profile at this point's sample time, not from
        # the jittered geometry: the GPS error would otherwise be counted as
        # progress and the stop windows would disappear from totalDis.
        dis = round(dist_at(point_times[i]), 3)
        # Derive the stats clock from the final (possibly compressed) track
        # timestamp: after the 2 s floor is pulled back, point_times is no
        # longer the value the server will read from allLocJson.
        tsec = max(0, int(round((p['ts'] - t0) / 1000.0)))
        steps = int(round(cum_steps[i]))
        if i == len(locs) - 1:
            dis, tsec, steps = total_meters, total_seconds, total_steps
        if i > 0:
            mps = deltas_m[i - 1] / deltas_s[i - 1]
        elif deltas_m:
            mps = deltas_m[0] / deltas_s[0]
        else:
            mps = avg_mps
        # A snapped checkpoint can insert a point a few metres after the
        # previous one; without the upper clamp that looks like 90 km/h.  The
        # lower bound stays at 0: a stop window really does report ~0 m/s, and
        # flattening it to 60% of the average is what made every record look
        # like it was run by a metronome.
        mps = min(mps, avg_mps * 1.6)
        bd_lat, bd_lng = coords.gcj02_to_bd09(p['lat'], p['lng'])
        # Bearing of travel, the way the client gets it from AMap.  The last
        # sample has no successor, so it keeps the heading it arrived with.
        if i + 1 < len(locs):
            heading = _bearing_deg(p, locs[i + 1])
        elif i:
            heading = _bearing_deg(locs[i - 1], p)
        else:
            heading = 0.0
        loc_points.append({
            'avgSpeed': round(mps, 4),
            'bdA': round(float(p.get('alt', 0.0)), 2),
            'bdD': heading,
            'bdG': 0,
            'bdS': round(mps, 4),
            'coorType': 'gcj02',
            'count': rng.randint(20, 88),
            'dtr': 0.0,
            'flag': locs[0]['ts'],
            'gLat': round(p['lat'], 7),
            'gLng': round(p['lng'], 7),
            'gainTime': safety.fmt_beijing(p['ts']),
            'id': 0,
            'lat': round(bd_lat, 7),
            'lng': round(bd_lng, 7),
            'locType': 0,
            'locationId': '',
            'queueNum': i,
            'radius': round(rng.uniform(1.4, 2.4), 2),
            'speed': round(mps * 3.6, 4),
            'state': rng.choices([1, 2, 3], weights=[145, 45, 164])[0],
            'steps': steps,
            'stepDistance': 0.0,
            'totalDis': dis,
            'totalTime': tsec,
            'type': checkpoint_marks.get(i, 0),
            'validDis': dis,
            'validTime': tsec,
        })

    # ---- sentinels + tail deceleration (track/postfix.rs) ------------------
    if len(loc_points) > 2:
        # tail: still moving but slowing to a stop, inside the valid pace band
        stop_ms = rng.uniform(2.0, 2.6)
        last = loc_points[-1]
        last['avgSpeed'] = round(stop_ms, 4)
        last['speed'] = round(stop_ms * 3.6, 4)
        last['bdS'] = round(rng.uniform(0.05, 0.15), 3)
        last['type'] = 6
        last['locType'] = 1
        last['radius'] = round(rng.uniform(1.5, 3.0), 2)

        # start sentinel: index 1 is the all-zero "setStartPoint" marker
        sp = loc_points[1]
        sp['type'] = 5
        sp['state'] = 1
        sp['locType'] = 1
        sp['radius'] = round(rng.uniform(2.0, 3.5), 2)
        sp['speed'] = 0.0
        sp['avgSpeed'] = 0.0
        sp['bdS'] = 0.0
        sp['bdD'] = 0.0        # the all-zero setStartPoint marker has no heading
        sp['totalDis'] = 0.0
        sp['validDis'] = 0.0
        sp['totalTime'] = 0
        sp['validTime'] = 0

        # index 2 is the first real point; keep its own speed, only bound it.
        # (The old `totalDis / 1.0` treated metres as km/s => every record
        # reported 21.6 km/h on this one point.)
        if len(loc_points) > 3:
            p2 = loc_points[2]
            p2['avgSpeed'] = round(min(6.0, max(1.8, p2['avgSpeed'])), 4)
            p2['speed'] = round(p2['avgSpeed'] * 3.6, 4)

    # Keep the domain closed.  The history page hands `type` straight to the
    # map SDK, and a value the client never emits (3, 7, 8, ...) falls through
    # to its fallback colour -- that is the grey segment.  Failing here is
    # cheaper than shipping a record nobody can explain later.
    legal_types = {0, 1, 2, 5, 6}
    bad_types = sorted({p['type'] for p in loc_points} - legal_types)
    if bad_types:
        raise ValueError('track point type outside the client domain: %s'
                         % bad_types)

    # The record's allLocJson column is itself Gson(LocJsonEntity):
    #   {"useZip":false,"allLocJson":"<List<MyLocation> as a string>"}
    # OBS uses the same points with the BD pair zeroed (wire.rs sets -1/-1),
    # so the replay and the record column can never drift apart.
    wire_points = [dict(q, lat=-1.0, lng=-1.0) for q in loc_points]
    loc_json_entity = {
        'useZip': False,
        'allLocJson': json.dumps(loc_points, separators=(',', ':')),
    }

    # What the record will actually report per kilometre.  The split editor
    # sends this list back as ``km_times``, so the table the operator edits and
    # the laps in the record are the same numbers.
    km_splits = [{
        'index': k + 1,
        'km': round(seg_m[k] / 1000.0, 3),
        'seconds': seg_t[k],
        'pace': round(seg_t[k] / max(1e-9, seg_m[k] / 1000.0), 1),
    } for k in range(n_km)]

    return {
        'allLocJson': locs,
        'locPoints': loc_points,
        'wire_points': wire_points,
        'locJsonEntity': loc_json_entity,
        'speed_json': speed_json,
        'step_freq_json': step_freq_json,
        'segmentJson': segment_json,
        'segment_json': segment_json,
        'fivePoint': five_point,
        'fivePointJson': point_json_entity,
        'fixed_point_json': point_json_entity,
        'km_splits': km_splits,
        'km_times': [s['seconds'] for s in km_splits],
        'juli': total_meters,
        'km': distance_km,
        'sportTime': total_seconds,
        'sporttime': total_seconds,
        'sportunid': run_uuid or new_run_uuid(),
        'total_steps': total_steps,
        # official formulas instead of distance * 0.06 and hard zeros
        'calorie': health.calorie(weight_kg, total_seconds, total_meters),
        'avgPower': health.avg_power(weight_kg, total_meters, total_seconds),
        'totalAscent': health.total_ascent([p.get('alt', 0.0) for p in locs]),
        'weight_kg': weight_kg,
    }


def _series_stats(series: list[dict], total_distance_m: float,
                  total_seconds: int, total_steps: int,
                  ascent: float | None = None) -> dict:
    """Summary numbers shared by the pre-submit preview and record detail."""
    cadence = [s['cadence'] for s in series if s.get('cadence') is not None]
    stride = [s['stride'] for s in series if s.get('stride') is not None]
    alt = [s['alt'] for s in series if s.get('alt') is not None]
    pace = [s['pace'] for s in series if s.get('pace') is not None]
    distance_km = total_distance_m / 1000.0
    avg_pace = (total_seconds / 60.0) / distance_km if distance_km > 0 else None
    avg_stride = (total_distance_m / total_steps * 100.0
                  if total_steps > 0 else None)
    return {
        'duration_s': int(total_seconds),
        'distance_m': round(float(total_distance_m), 1),
        'avg_pace': round(avg_pace, 3) if avg_pace else None,
        'min_pace': round(min(pace), 3) if pace else None,
        'max_pace': round(max(pace), 3) if pace else None,
        'avg_cadence': round(sum(cadence) / len(cadence), 1) if cadence else None,
        'max_cadence': round(max(cadence), 1) if cadence else None,
        'min_cadence': round(min(cadence), 1) if cadence else None,
        'avg_stride': round(avg_stride, 1) if avg_stride else None,
        'max_stride': round(max(stride), 1) if stride else None,
        'min_stride': round(min(stride), 1) if stride else None,
        'alt_min': round(min(alt), 1) if alt else None,
        'alt_max': round(max(alt), 1) if alt else None,
        'ascent': round(float(ascent), 1) if ascent is not None else None,
        'total_steps': int(total_steps or 0),
    }


def _downsample_series(series: list[dict], limit: int = 240) -> list[dict]:
    """Keep the shape (and both ends) when a long run has too many windows."""
    if len(series) <= limit:
        return series
    step = max(1, math.ceil(len(series) / limit))
    out = series[::step]
    if out[-1] is not series[-1]:
        out.append(series[-1])
    return out


def preview_series(run: dict, limit: int = 240) -> dict:
    """Official-style 10 s chart series for a generated run.

    This is display-only: it never changes the record that is submitted.  The
    numbers come from the same ``speed_json`` / ``step_freq_json`` /
    ``allLocJson`` the upload uses, so the preview and the record cannot drift
    apart just because the UI recomputed pace differently.
    """
    locs = [p for p in (run.get('allLocJson') or []) if isinstance(p, dict)]
    speeds = [s for s in (run.get('speed_json') or []) if isinstance(s, dict)]
    steps = {int(s.get('beginTime') or 0): s
             for s in (run.get('step_freq_json') or [])
             if isinstance(s, dict)}
    alt_points = [(int(p.get('ts') or 0), float(p.get('alt') or 0.0))
                  for p in locs if p.get('alt') is not None]

    def alt_at(ms: int, start: int, end: int):
        vals = [v for t, v in alt_points if start <= t <= end]
        if vals:
            return sum(vals) / len(vals)
        if not alt_points:
            return None
        return min(alt_points, key=lambda x: abs(x[0] - ms))[1]

    first_ms = int(speeds[0]['beginTime']) if speeds else 0
    series = []
    for s in speeds:
        begin = int(s.get('beginTime') or 0)
        end = int(s.get('endTime') or begin)
        dt = max(0.001, (end - begin) / 1000.0)
        # The last bucket is clamped to stop_ms, so it can be 1-3 s long.
        # Dividing a couple of steps by 2 s produced 6 spm valleys that looked
        # like a GPS dropout; those tail windows are display noise, not data.
        if dt < 6.0:
            continue
        dist = float(s.get('distance') or 0.0)
        st = steps.get(begin) or {}
        n_steps = float(st.get('stepsNum') or 0.0)
        pace = (dt / 60.0) / (dist / 1000.0) if dist > 0 else None
        cadence = n_steps / (dt / 60.0) if n_steps > 0 else None
        stride = dist / n_steps * 100.0 if n_steps > 0 else None
        alt = alt_at(end, begin, end)
        series.append({
            't': round((begin - first_ms) / 1000.0, 1),
            'pace': round(pace, 3) if pace is not None else None,
            'cadence': round(cadence, 1) if cadence is not None else None,
            'stride': round(stride, 1) if stride is not None else None,
            'alt': round(alt, 1) if alt is not None else None,
        })

    splits = []
    cumulative = 0
    for item in (run.get('km_splits') or []):
        cumulative += int(item.get('seconds') or 0)
        splits.append({
            'km': round(float(item.get('km') or 0), 3),
            'seconds': int(item.get('seconds') or 0),
            'cumulative': cumulative,
            'pace': round(float(item.get('pace') or 0), 3),
        })

    total_distance = float(run.get('juli') or 0)
    total_seconds = int(run.get('sportTime') or 0)
    total_steps = int(run.get('total_steps') or 0)
    stats = _series_stats(series, total_distance, total_seconds, total_steps,
                          run.get('totalAscent'))
    stats['kcal'] = run.get('calorie')
    stats['avg_power'] = run.get('avgPower')
    return {
        **stats,
        'series': _downsample_series(series, limit),
        'splits': splits,
    }


def track_series(track: list[dict], limit: int = 240) -> dict | None:
    """Derive the same chart series from a server record's parsed track.

    ``parse_track`` already normalises ``totalTime`` / ``totalDis`` / ``steps``
    / ``bdA``; this function only resamples at 10 s so an old record looks like
    the app's own record page.  It returns ``None`` when the server did not
    send enough fields to compute an honest series.
    """
    pts = []
    for p in (track or []):
        if not isinstance(p, dict):
            continue
        t = p.get('duration_s')
        d = p.get('distance_m')
        if t is None or d is None:
            continue
        try:
            pts.append({
                't': max(0.0, float(t)),
                'd': max(0.0, float(d)),
                's': max(0.0, float(p.get('steps') or 0)),
                'alt': (float(p.get('altitude'))
                        if p.get('altitude') is not None else None),
            })
        except (TypeError, ValueError):
            continue
    if len(pts) < 2:
        return None
    pts.sort(key=lambda x: x['t'])
    total_seconds = int(round(pts[-1]['t']))
    total_distance = max(x['d'] for x in pts)
    total_steps = int(round(max(x['s'] for x in pts)))
    if total_seconds <= 0 or total_distance <= 0:
        return None

    def interp(key: str, t: float):
        prev = pts[0]
        for cur in pts[1:]:
            if cur['t'] >= t:
                span = cur['t'] - prev['t']
                k = (t - prev['t']) / span if span > 0 else 0.0
                a, b = prev.get(key), cur.get(key)
                if a is None or b is None:
                    return b if b is not None else a
                return a + (b - a) * k
            prev = cur
        return pts[-1].get(key)

    series = []
    prev_t = prev_d = prev_s = 0.0
    for t in range(10, total_seconds + 1, 10):
        d = interp('d', t)
        n = interp('s', t)
        alt = interp('alt', t)
        dt = t - prev_t
        wd = max(0.0, float(d or 0) - prev_d)
        wn = max(0.0, float(n or 0) - prev_s)
        pace = (dt / 60.0) / (wd / 1000.0) if wd > 0 else None
        cadence = wn / (dt / 60.0) if wn > 0 else None
        stride = wd / wn * 100.0 if wn > 0 else None
        series.append({
            't': float(t),
            'pace': round(pace, 3) if pace is not None else None,
            'cadence': round(cadence, 1) if cadence is not None else None,
            'stride': round(stride, 1) if stride is not None else None,
            'alt': round(float(alt), 1) if alt is not None else None,
        })
        prev_t, prev_d, prev_s = t, float(d or 0), float(n or 0)

    splits = []
    prev_t = 0.0
    cumulative_d = 0.0
    while cumulative_d + 1000.0 <= total_distance + 0.5:
        cumulative_d += 1000.0
        t = None
        prev = pts[0]
        for cur in pts[1:]:
            if cur['d'] >= cumulative_d:
                span = cur['d'] - prev['d']
                k = (cumulative_d - prev['d']) / span if span > 0 else 0.0
                t = prev['t'] + (cur['t'] - prev['t']) * k
                break
            prev = cur
        if t is None:
            break
        splits.append({
            'km': 1.0,
            'seconds': int(round(t - prev_t)),
            'cumulative': int(round(t)),
            'pace': round(t - prev_t, 1),
        })
        prev_t = t
    if total_distance - cumulative_d > 1.0:
        rest = total_distance - cumulative_d
        sec = max(0, total_seconds - int(round(prev_t)))
        splits.append({
            'km': round(rest / 1000.0, 3),
            'seconds': sec,
            'cumulative': total_seconds,
            'pace': round(sec / (rest / 1000.0), 1) if rest else 0,
        })

    stats = _series_stats(series, total_distance, total_seconds, total_steps)
    return {
        **stats,
        'series': _downsample_series(series, limit),
        'splits': splits,
    }


def validate_run_data(d: dict) -> list[str]:
    """Pre-submit invariants.  Everything here is something we learned the hard
    way and do not want to ship a violation of."""
    errs = []
    for key in ('allLocJson', 'speed_json', 'step_freq_json', 'segmentJson',
                'fivePoint', 'juli', 'km', 'sportTime', 'sportunid'):
        if key not in d:
            errs.append(f'missing:{key}')
    locs = d.get('allLocJson', [])
    if len(locs) < 2:
        errs.append(f'allLocJson.len={len(locs)}')
        return errs

    ts = [p.get('ts', 0) for p in locs]
    if any(b - a <= 0 for a, b in zip(ts, ts[1:])):
        errs.append('timestamps-not-monotonic')

    # anti-teleport: adjacent points <= 100m (rough equirectangular, meters)
    for a, b in zip(locs, locs[1:]):
        dx = (b['lng'] - a['lng']) * 111320 * math.cos(math.radians(a['lat']))
        dy = (b['lat'] - a['lat']) * 110540
        if math.hypot(dx, dy) > 100:
            errs.append('teleport-detected')
            break

    # ---- geometry must match the claimed distance -----------------------
    # A 5 km run that only draws 2 km of path is a giveaway; this used to
    # happen because the fallback route ignored target_m.
    geo = 0.0
    for a, b in zip(locs, locs[1:]):
        geo += math.hypot((b['lng'] - a['lng']) * 111320
                          * math.cos(math.radians(a['lat'])),
                          (b['lat'] - a['lat']) * 110540)
    nominal = float(d.get('juli') or 0)
    if nominal > 0 and abs(geo - nominal) > max(25.0, nominal * 0.04):
        errs.append('geometry %.0fm vs claimed %.0fm' % (geo, nominal))

    want_buckets = max(1, int(math.ceil(int(d.get('sportTime', 0))
                                         / float(SEG_SECONDS))))
    sp = d.get('speed_json', [])
    st = d.get('step_freq_json', [])
    if len(sp) != want_buckets:
        errs.append(f'speed_json.len={len(sp)} (want {want_buckets})')
    if len(st) != want_buckets:
        errs.append(f'step_freq_json.len={len(st)}')
    if sp and abs(sum(x['distance'] for x in sp) - nominal) > max(5.0, nominal * 0.01):
        errs.append('bucket distance sum %.1f != %.1f'
                    % (sum(x['distance'] for x in sp), nominal))
    if st and abs(sum(x['stepsNum'] for x in st) - d.get('total_steps', 0)) > 2:
        errs.append('bucket steps sum %d != %d'
                    % (sum(x['stepsNum'] for x in st), d.get('total_steps', 0)))

    # ---- both coordinate frames must be present and different -----------
    # lat/lng are BD-09, gLat/gLng are GCJ-02; here they differ by ~0.006 deg.
    # If they are identical, one of them is wrong.
    try:
        wrap = d['locJsonEntity']
        pts = json.loads(wrap['allLocJson']) if isinstance(wrap.get('allLocJson'), str) \
            else wrap['allLocJson']
        p0 = pts[0]
        if abs(float(p0['lat']) - float(p0['gLat'])) < 0.001:
            errs.append('track lat/gLat identical (BD and GCJ must differ)')
    except Exception as e:  # noqa: BLE001
        errs.append('locJsonEntity unreadable: %s' % e)

    # ---- five point -----------------------------------------------------
    fps = d.get('fivePoint') or []
    if not fps:
        # The count is whatever the campus returns (3, 5, 7...); requiring
        # exactly 5 rejected every school that is not 5.
        errs.append('fivePoint empty')
    for k, fp in enumerate(fps):
        if abs(float(fp.get('lat', 0)) - float(fp.get('glat', 0))) < 0.001:
            errs.append('fivePoint[%d] lat/glat identical' % k)
            break
        if float(fp.get('lat', 0)) == 0.0 or float(fp.get('glat', 0)) == 0.0:
            errs.append('fivePoint[%d] has zero coordinate' % k)
            break
    missing_pass = [k for k, fp in enumerate(fps) if not fp.get('isPass')]
    if missing_pass:
        errs.append('fivePoint not passed: %s' % missing_pass)

    # ---- laps must cover the distance -----------------------------------
    try:
        from ..transport import obs as _obs
        laps = _obs.build_laps(d, locs[0]['ts'])
        if not laps:
            errs.append('laps empty')
        elif abs(sum(l['distance'] for l in laps) - nominal) > max(5.0, nominal * 0.01):
            errs.append('lap distance sum %.1f != %.1f'
                        % (sum(l['distance'] for l in laps), nominal))
        elif any(l['avgPace'] is None for l in laps):
            errs.append('lap avgPace is None')
    except Exception as e:  # noqa: BLE001
        errs.append('laps unbuildable: %s' % e)

    return errs


def _submit_windows(values: list) -> list:
    """`src/api/submit.rs::submit_windows` — submit ids differ from OBS ids."""
    out = []
    for i, v in enumerate(values):
        d = dict(v)
        d['id'] = 60000 + i
        d['queueNum'] = i
        out.append(d)
    return out


def build_upload_entity(run: dict, ctx: dict) -> dict:
    """Map forged run_data + submit context to the official 47-field
    UploadFormatEntity shape (Coaixy 7.2.70 source). JSON-string fields
    (allLocJson/fivePointJson/segmentJson) are stringified like Gson does.
    """
    import json as _json
    locs = run['allLocJson']
    t0 = locs[0]['ts']
    t1 = locs[-1]['ts']
    # UploadFormatEntity.speed is the AVERAGE PACE in minutes/km, stored as
    # round(pace, 2) * 1000  -- see UploadFormatEntity.setSpeed(double) and
    # Utils.formatSpeed(double) which renders it back as i'rr".
    min_per_km = (run['sportTime'] / 60.0) / max(0.001, run['juli'] / 1000.0)
    ent = {
        'address': ctx.get('address', ''),
        'allLocJson': _json.dumps(run['locJsonEntity'], separators=(',', ':')),
        'avgPower': int(run.get('avgPower', 0) or 0),
        # upstream: 1i64.max(round(total_steps / total_time * 60))
        'avgStepFreq': max(1, int(round(
            run.get('total_steps', 0) / max(1, run.get('sportTime', 1)) * 60.0))),
        'calorie': int(run.get('calorie', 0) or 0),
        'complete': True,
        'errorCode': 0,
        'faceCheck': ctx.get('faceCheck', 0),
        'fivePointJson': _json.dumps(run['fivePointJson'], separators=(',', ':')),
        'geeToken': ctx.get('geeToken', ''),
        'getPrize': False,
        'goalId': None,
        'isUpload': False,
        'latitude': locs[0]['lat'],
        'longitude': locs[0]['lng'],
        'maxRunTime': 0,
        'minSteps': 0,
        'more': False,
        'originalSign': '',
        # policy selects the server-side completion rule set, and it is decided
        # CLIENT-side: HomeViewModel.isSequence() sets 1 when the run carries a
        # first FivePoint with position < 5, else 0.  It is then copied verbatim
        # into the upload (ExerciseRunPImpl L397: setPolicy(sportRecord.getPolicy())).
        #   0 -> "经过必经点位" + "至少通过2个普通点位"   (never fully satisfiable
        #        without the school's real waypoints -> stuck at 5/6)
        #   1 -> "按顺序通过所有点位"                    (satisfied by the track)
        # Shipping 1 is what real 学分跑 records carry; verified 6/6 on read-back.
        'policy': ctx.get('policy', 1),
        # prizeName / recordUrl / segmentJson do not appear anywhere in the
        # reference client's submit body (src/api/submit.rs), and the
        # `Segment` struct in src/track/model.rs has a different shape than
        # what we used to emit.  Dropped rather than invented.
        'selDistance': ctx.get('selDistance', run['juli']),
        'selRunTime': run['sportTime'],
        'selectedUnid': ctx.get('selectedUnid', 0),
        'signature': '',
        'speed': int(round(round(min_per_km, 2) * 1000)),
        'speedPerTenSec': _submit_windows(run['speed_json']),
        'sportType': ctx.get('sportType', 1),
        'startTime': t0,
        'status': 0,
        'stepsPerTenSec': _submit_windows(run['step_freq_json']),
        'stopTime': t1,
        'themeId': 0,
        'totalAscent': int(run.get('totalAscent', 0) or 0),
        'totalDis': run['juli'],
        'totalSteps': run.get('total_steps', 0),
        'totalTime': run['sportTime'],
        'uid': ctx.get('uid', ''),
        'unCompleteReason': 0,
        'unauthorized': 0,
        'useMobilityTools': 0,
        'uuid': run['sportunid'],
        'validDis': run['juli'],
        'validTime': run['sportTime'],
    }
    # roomId only exists for indoor runs (src/crypto/sign.rs: sportType=3)
    if ent['sportType'] == 3:
        ent['roomId'] = ctx.get('roomId', 0)
    return ent


def sign_upload_entity(ent: dict, salt: str | None = None) -> dict:
    """Apply official two-phase signing (HttpUtil.signRunRecord):
    originalSign = getPreSign(31-field map); signature = getSign(map)."""
    from .crypto import UPLOAD_SIGN_FIELDS
    m: dict = {}
    for k in UPLOAD_SIGN_FIELDS:
        v = ent.get(k)
        if v is None:
            # Java: String.valueOf((Object) null) == "null" (a 4-char string),
            # and SignCode.paraFilter only drops real nulls, so the literal
            # "null" DOES participate in the signature (e.g. goalId).
            m[k] = 'null'
            continue
        m[k] = v if isinstance(v, str) else (
            jbool(v) if isinstance(v, bool) else str(v))
    ent = dict(ent)
    ent['originalSign'] = official_presign(m)
    ent['signature'] = (official_sign(m) if salt is None
                        else official_sign(m, salt))
    return ent

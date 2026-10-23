"""Run-profile variation and a multi-day rhythm planner.

Where the numbers come from
---------------------------
Only two of the bands below are taken from the reference implementation; the
rest are explicit configuration choices, and are labelled as such.  Nothing here
is population data — there is no public dataset of this platform's runs, and
anyone claiming otherwise is guessing.

Grounded (reference implementation, `YanamiNeko/NekoSportsWorldTool`):

* **pace band 360-480 s/km** — `src/api/model.rs`, `default_pace_min = 360.0`,
  `default_pace_max = 480.0` (i.e. 6'00"–8'00"), sampled **uniformly** by
  `sample_shape()` in `src/ui/run.rs`.
* **start window 07:00-20:00, uniform** — `src/ui/run.rs`,
  `RAND_HOUR_LO = 7`, `RAND_HOUR_HI = 20`.
* **cadence/stride** — `crates/route-planner/src/biomech.rs`,
  `cadence = (150 + 12·v)·(1 − 0.05·fatigue)` clamped to 110–220, with
  `fatigue = 0` below 0.5 km else `min((km−0.5)·1.5, 0.6)`.  The identity
  `v = cadence/60 · stride` is enforced, stride clamped to 0.5–1.7 m.

Our additions (not upstream):

* distance band (the reference exposes `dist_min`/`dist_max` with no built-in
  default, so any band is a choice);
* the "no twin neighbours" constraint;
* the rest-day planner and the weekly soft cap;
* the history similarity warnings.

What this does not do
---------------------
It does not make a record undetectable, and it says nothing about how the
platform decides what is suspicious — that is not documented anywhere.
"""

from __future__ import annotations

import datetime as dt
import json
import random

from . import safety

# --------------------------------------------------------------------------
# bands
# --------------------------------------------------------------------------

#: seconds per km — reference UI defaults (6'00"–8'00"), sampled uniformly
PACE_MIN_S = 360.0
PACE_MAX_S = 480.0

#: start-of-run window, minutes of day — reference random-time window
START_LO_MIN = 7 * 60
START_HI_MIN = 20 * 60

#: distance band, km.  CONFIGURATION CHOICE, not measured data.
DIST_LO_KM = 2.0
DIST_HI_KM = 3.0

#: route style; "free" (no campus checkpoints) is excluded on purpose — a
#: record that skips every checkpoint is incomplete server-side, not "varied".
ROUTE_STYLES = (('points', 0.55), ('stadium', 0.45))

#: how close two records may be before we call them twins (our rule)
SAME_DISTANCE_KM = 0.12
SAME_PACE_S = 8.0

#: weekly volume ceiling used by the warnings (our rule)
SOFT_WEEKLY_CAP = 5


def _weighted(rng: random.Random, pairs) -> tuple:
    total = sum(w for _, w in pairs)
    x = rng.random() * total
    for value, w in pairs:
        x -= w
        if x <= 0:
            return value
    return pairs[-1][0]


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


# --------------------------------------------------------------------------
# biomechanics (reference formula)
# --------------------------------------------------------------------------

def fatigue_from_km(km: float) -> float:
    """`biomech::fatigue_from_km`."""
    return 0.0 if km <= 0.5 else min((km - 0.5) * 1.5, 0.6)


def cadence_for(pace_min_per_km: float, distance_km: float) -> int:
    """Cadence from the reference gait model, evaluated mid-run."""
    v = 1000.0 / (pace_min_per_km * 60.0)          # m/s
    fat = fatigue_from_km(distance_km / 2.0)       # average over the run
    cad = (150.0 + 12.0 * v) * (1.0 - 0.05 * fat)
    cad = _clamp(cad, 110.0, 220.0)
    stride = v * 60.0 / cad
    if stride > 1.7:
        cad = v * 60.0 / 1.7
    elif stride < 0.5:
        cad = v * 60.0 / 0.5
    return int(round(cad))


def stride_for(pace_min_per_km: float, cadence: int) -> float:
    v = 1000.0 / (pace_min_per_km * 60.0)
    return round(v * 60.0 / max(1, cadence), 3)


# --------------------------------------------------------------------------
# one profile
# --------------------------------------------------------------------------

def make_profile(day: dt.date | None = None, rng: random.Random | None = None,
                 avoid: dict | None = None,
                 weight_kg: float | None = None,
                 dist_band: tuple[float, float] | None = None) -> dict:
    """One believable run for `day` (defaults to today, Beijing)."""
    rng = rng or random.Random()
    if day is None:
        day = safety.to_beijing(safety.now_ms()).date()

    lo_km, hi_km = dist_band or (DIST_LO_KM, DIST_HI_KM)
    if lo_km > hi_km:
        lo_km, hi_km = hi_km, lo_km

    for _ in range(12):
        # uniform, exactly like the reference sampler
        distance = round(rng.uniform(lo_km, hi_km), 2)
        pace_s = round(rng.uniform(PACE_MIN_S, PACE_MAX_S), 1)
        start_min = int(rng.uniform(START_LO_MIN, START_HI_MIN + 1))

        cadence = cadence_for(pace_s / 60.0, distance)

        if avoid and _too_similar({'distance_km': distance,
                                   'pace_min_per_km': pace_s / 60.0,
                                   'start_min': start_min}, avoid):
            continue

        when = dt.datetime.combine(day, dt.time(start_min // 60,
                                                start_min % 60),
                                   tzinfo=safety.BEIJING)
        style = _weighted(rng, ROUTE_STYLES)
        return {
            'day': day.isoformat(),
            'weekday': '一二三四五六日'[day.weekday()],
            'start_ms': int(when.timestamp() * 1000),
            'start_hhmm': '%02d:%02d' % (start_min // 60, start_min % 60),
            'start_min': start_min,
            'distance_km': distance,
            'pace_min_per_km': round(pace_s / 60.0, 3),
            'pace_seconds': pace_s,
            'pace_hhmmss': _pace_text(pace_s),
            'duration_s': int(round(distance * pace_s)),
            'cadence': cadence,
            'stride_m': stride_for(pace_s / 60.0, cadence),
            'route': style,
            'weight_kg': weight_kg,
        }

    # give up on variety rather than spin forever
    return make_profile(day, random.Random(rng.random()), None, weight_kg,
                        dist_band)


def _pace_text(pace_s: float) -> str:
    total = int(round(pace_s))
    return "%d'%02d\"" % (total // 60, total % 60)


def _too_similar(a: dict, b: dict) -> bool:
    if abs(a['distance_km'] - b.get('distance_km', -1)) < SAME_DISTANCE_KM:
        return True
    if abs(a['pace_min_per_km'] * 60
           - b.get('pace_min_per_km', -1) * 60) < SAME_PACE_S:
        return True
    return False


# --------------------------------------------------------------------------
# multi-day rhythm (our rule, not upstream)
# --------------------------------------------------------------------------

def plan(days: int = 14, start_day: dt.date | None = None,
         rng: random.Random | None = None,
         runs_per_week: float = 3.8,
         dist_band: tuple[float, float] | None = None) -> list[dict | None]:
    """Return `days` slots; None means a rest day.

    Gap model: 1 day is most common, 2 next, 3 occasionally, back-to-back rare
    and never three in a row.  Weekly volume is soft-capped.
    """
    rng = rng or random.Random()
    if start_day is None:
        start_day = safety.to_beijing(safety.now_ms()).date()

    target_runs = max(1, int(round(days * runs_per_week / 7.0)))
    slots: list[dict | None] = []
    last_run_idx = None
    streak = 0
    previous = None

    for i in range(days):
        day = start_day + dt.timedelta(days=i)
        week_start = i - (i % 7)
        runs_this_week = sum(1 for s in slots[week_start:] if s)

        blocked = False
        if last_run_idx is None:
            p = 0.85 if i == 0 else 0.35
        else:
            gap = i - last_run_idx
            p = {1: 0.44, 2: 0.66, 3: 0.80}.get(gap, 0.88)
            if gap == 1 and streak >= 2:
                p = 0.0
                blocked = True          # hard rule: never three days in a row

        if runs_this_week >= SOFT_WEEKLY_CAP:
            p = 0.0
            blocked = True

        remaining_days = days - i
        remaining_runs = target_runs - sum(1 for s in slots if s)
        if remaining_runs <= 0:
            p = 0.0
        elif remaining_runs >= remaining_days and not blocked:
            p = 1.0

        if rng.random() < p:
            profile = make_profile(day, rng, previous, None, dist_band)
            slots.append(profile)
            previous = profile
            streak = streak + 1 if last_run_idx == i - 1 else 1
            last_run_idx = i
        else:
            slots.append(None)
            streak = 0
    return slots


def render_plan(slots: list[dict | None], today: dt.date | None = None,
                start_day: dt.date | None = None,
                compact: bool = False) -> str:
    """Text table.  `compact` drops the year and the stride column so the whole
    line fits a ~300 px sidebar without horizontal scrolling."""
    today = today or safety.to_beijing(safety.now_ms()).date()
    if start_day is None:
        # Infer from the FIRST run and its index, not from slot 0: if the plan
        # opens with rest days, using the first run's own date would shift every
        # earlier row and print duplicate days.
        start_day = today
        for i, s in enumerate(slots):
            if s:
                start_day = dt.date.fromisoformat(s['day']) - dt.timedelta(days=i)
                break

    if not compact:
        lines = ['日期        周  时段   距离      配速      时长   步频  步幅  路线',
                 '-' * 68]
    else:
        lines = ['日期   周 时段  距离  配速   时长   步频 路线',
                 '-' * 47]

    runs = 0
    for i, s in enumerate(slots):
        day = start_day + dt.timedelta(days=i)
        wd = '一二三四五六日'[day.weekday()]
        if day != today:
            mark = ''
        else:
            mark = ' *' if compact else '  <- 今天'
        if s is None:
            if compact:
                lines.append('  %s  %s  --:--  休息%s'
                             % (day.strftime('%m-%d'), wd, mark))
            else:
                lines.append('  %s  %s  %-5s  %-9s%s'
                             % (day.isoformat(), wd, '--:--', '休息', mark))
            continue
        runs += 1
        if compact:
            lines.append('  %s  %s  %s  %.2f  %-6s %2dm%02ds %3d  %s%s'
                         % (day.strftime('%m-%d'), wd, s['start_hhmm'],
                            s['distance_km'], s['pace_hhmmss'],
                            s['duration_s'] // 60, s['duration_s'] % 60,
                            s['cadence'], s['route'], mark))
        else:
            lines.append('  %s  %s  %s  %4.2f km  %-7s  %2dm%02ds  %d  %.2fm  %s%s'
                         % (s['day'], s['weekday'], s['start_hhmm'],
                            s['distance_km'], s['pace_hhmmss'],
                            s['duration_s'] // 60, s['duration_s'] % 60,
                            s['cadence'], s['stride_m'], s['route'], mark))
    lines.append('-' * (47 if compact else 68))
    lines.append('共 %d 天，跑步 %d 次，休息 %d 天'
                 % (len(slots), runs, len(slots) - runs))
    return '\n'.join(lines)


# --------------------------------------------------------------------------
# history awareness (our rule)
# --------------------------------------------------------------------------

def fetch_recent(client, acct, pages: int = 2) -> list[dict]:
    """Recent records, flattened to what the similarity checks need."""
    out: list[dict] = []
    for page in range(1, pages + 1):
        try:
            res = client.request(acct, 'api/v70230/runnings/records',
                                 {'pageNum': page, 'pageSize': 20}, retries=2)
        except Exception:  # noqa: BLE001
            break
        biz = res.get('json') or {}
        if biz.get('error') not in (None, 10000):
            break
        data = biz.get('data')
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:  # noqa: BLE001
                data = None
        if isinstance(data, dict):
            data = data.get('list') or data.get('records') or []
        if not isinstance(data, list) or not data:
            break
        for r in data:
            out.append(_flatten(r))
        if len(data) < 20:
            break
    return out


def _flatten(r: dict) -> dict:
    dist = r.get('totalDis') or r.get('juli') or 0
    try:
        dist = float(dist)
    except Exception:  # noqa: BLE001
        dist = 0.0
    # `totalDis` is metres; the old >1000 guard misread any sub-kilometre run
    # (800 m stayed 800 -> treated as 800 km).
    if dist > 100:
        dist /= 1000.0

    start = r.get('startTime') or r.get('start_time') or 0
    try:
        start = int(start)
    except Exception:  # noqa: BLE001
        start = 0

    dur = r.get('sportTime') or r.get('sporttime') or r.get('totalTime') or 0
    pace = 0.0
    try:
        dur = float(dur)
        pace = (dur / 60.0) / dist if dist > 0 and dur > 0 else 0.0
    except Exception:  # noqa: BLE001
        dur = 0.0

    return {'rrid': r.get('rrid') or r.get('id'),
            'start_ms': start, 'distance_km': round(dist, 3),
            'pace_min_per_km': round(pace, 3), 'duration_s': int(dur),
            'is_cheat': r.get('isCheat')}


def similarity_warnings(profile: dict, history: list[dict],
                        now_ms: int | None = None) -> list[str]:
    """What a reviewer would notice about *this* record next to the old ones."""
    warns: list[str] = []
    now_ms = now_ms or safety.now_ms()

    for h in history[:6]:
        if not h.get('start_ms'):
            continue
        if abs(h['distance_km'] - profile['distance_km']) < SAME_DISTANCE_KM:
            warns.append('距离 %.2fkm 与最近记录 %s 几乎相同'
                         % (profile['distance_km'], h.get('rrid')))
            break

    for h in history[:6]:
        if h.get('pace_min_per_km') and abs(
                h['pace_min_per_km'] - profile['pace_min_per_km']) * 60 < SAME_PACE_S:
            warns.append('配速 %s 与最近记录几乎相同'
                         % profile.get('pace_hhmmss', ''))
            break

    hour = safety.to_beijing(profile['start_ms']).hour
    same_hour = [h for h in history[:10]
                 if h.get('start_ms')
                 and safety.to_beijing(h['start_ms']).hour == hour]
    if len(same_hour) >= 2:
        warns.append('最近 %d 条记录都在 %02d 点开始，时段过于固定'
                     % (len(same_hour), hour))

    week_ago = now_ms - 7 * 86400_000
    recent = [h for h in history if h.get('start_ms', 0) >= week_ago]
    if len(recent) >= SOFT_WEEKLY_CAP:
        warns.append('最近 7 天已有 %d 条记录，超过建议上限 %d'
                     % (len(recent), SOFT_WEEKLY_CAP))

    today = safety.to_beijing(now_ms).date()
    same_day = [h for h in history
                if h.get('start_ms')
                and safety.to_beijing(h['start_ms']).date() == today]
    if same_day:
        warns.append('今天已有 %d 条记录，同一天多条比每天一条更容易被注意'
                     % len(same_day))

    flagged = [h for h in history if h.get('is_cheat') not in (None, 0)]
    if flagged:
        warns.append('历史记录里有 %d 条 isCheat 非 0 —— 账号可能已被标记'
                     % len(flagged))

    return warns


# --------------------------------------------------------------------------
# self test
# --------------------------------------------------------------------------

def self_test() -> list[tuple[str, bool, str]]:
    out = []
    rng = random.Random(20261020)
    day0 = dt.date(2026, 10, 20)

    profiles = []
    prev = None
    for i in range(30):
        p = make_profile(day0 + dt.timedelta(days=i), rng, prev)
        profiles.append(p)
        prev = p

    dists = [p['distance_km'] for p in profiles]
    paces = [p['pace_seconds'] for p in profiles]
    starts = [p['start_min'] for p in profiles]
    cads = [p['cadence'] for p in profiles]

    out.append(('distance in band', all(DIST_LO_KM <= d <= DIST_HI_KM
                                        for d in dists),
                '%.2f..%.2f' % (min(dists), max(dists))))
    out.append(('pace in 360-480s', all(360 <= p <= 480 for p in paces),
                '%.0f..%.0f s/km' % (min(paces), max(paces))))
    out.append(('start in 07:00-20:00', all(START_LO_MIN <= s <= START_HI_MIN + 1
                                            for s in starts),
                '%02d:%02d..%02d:%02d'
                % (min(starts) // 60, min(starts) % 60,
                   max(starts) // 60, max(starts) % 60)))
    out.append(('cadence 110-220 (ref clamp)', all(110 <= c <= 220 for c in cads),
                '%d..%d' % (min(cads), max(cads))))

    # biomech identity: v = cad/60 * stride
    worst = 0.0
    for p in profiles:
        v = 1000.0 / (p['pace_min_per_km'] * 60.0)
        worst = max(worst, abs(v - p['cadence'] / 60.0 * p['stride_m']))
    out.append(('v = cad/60 * stride', worst < 0.02, 'max err %.4f m/s' % worst))

    # no twin neighbours
    twins = 0
    for a, b in zip(profiles, profiles[1:]):
        if (abs(a['distance_km'] - b['distance_km']) < SAME_DISTANCE_KM
                and abs(a['pace_seconds'] - b['pace_seconds']) < SAME_PACE_S):
            twins += 1
    out.append(('no twin neighbours', twins == 0, '%d twins' % twins))

    out.append(('distance variety', len({round(d, 1) for d in dists}) >= 8,
                '%d distinct' % len({round(d, 1) for d in dists})))
    out.append(('pace variety', len({round(p, 0) for p in paces}) >= 8,
                '%d distinct' % len({round(p, 0) for p in paces})))

    bad = [p for p in profiles if not (12 * 60 <= p['duration_s'] <= 45 * 60)]
    out.append(('duration plausible', not bad,
                '%d..%d min' % (min(p['duration_s'] for p in profiles) // 60,
                                max(p['duration_s'] for p in profiles) // 60)))

    slots = plan(14, day0, random.Random(7))
    runs = [s for s in slots if s]
    out.append(('14d -> 5..10 runs', 5 <= len(runs) <= 10,
                '%d runs / %d rest' % (len(runs), 14 - len(runs))))
    longest = cur = 0
    for s in slots:
        cur = cur + 1 if s else 0
        longest = max(longest, cur)
    out.append(('never 3+ consecutive', longest <= 2, 'max streak %d' % longest))

    # every rendered day must appear once, in order, even when the plan opens
    # with rest days (that used to shift the rows and duplicate dates)
    import re as _re
    for seed in range(6):
        txt = render_plan(plan(14, day0, random.Random(seed)))
        days = _re.findall(r'^\s+(\d{4}-\d{2}-\d{2})', txt, _re.M)
        if days != sorted(days) or len(set(days)) != len(days):
            out.append(('plan dates monotonic', False, txt.splitlines()[:4]))
            break
    else:
        out.append(('plan dates monotonic', True, '6 seeds ok'))

    hist = [{'rrid': 1, 'start_ms': safety.now_ms() - 86400_000,
             'distance_km': 2.5, 'pace_min_per_km': 6.0, 'is_cheat': 0},
            {'rrid': 2, 'start_ms': safety.now_ms() - 2 * 86400_000,
             'distance_km': 2.5, 'pace_min_per_km': 6.0, 'is_cheat': 0}]
    dup = {'distance_km': 2.51, 'pace_min_per_km': 6.02,
           'start_ms': safety.now_ms(), 'pace_hhmmss': "6'01\""}
    w = similarity_warnings(dup, hist)
    out.append(('duplicate detected', any('几乎相同' in x for x in w), str(len(w))))

    clean = similarity_warnings(
        {'distance_km': 4.31, 'pace_min_per_km': 5.62, 'start_ms': safety.now_ms(),
         'pace_hhmmss': "5'37\""}, [])
    out.append(('clean history silent', clean == [], str(clean)))

    bad_hist = [{'rrid': 9, 'start_ms': safety.now_ms() - 86400_000,
                 'distance_km': 3.1, 'pace_min_per_km': 6.2, 'is_cheat': 1}]
    out.append(('isCheat flagged', any('已被标记' in x
                                       for x in similarity_warnings(profiles[0],
                                                                    bad_hist)),
                'flagged'))

    # distance units: the records endpoint reports metres, including sub-km runs
    f1 = _flatten({'totalDis': 800, 'sportTime': 300})
    out.append(('sub-km record scaled', abs(f1['distance_km'] - 0.8) < 1e-6,
                f1['distance_km']))
    f2 = _flatten({'totalDis': 2.5, 'sportTime': 900})
    out.append(('km-valued record untouched',
                abs(f2['distance_km'] - 2.5) < 1e-6, f2['distance_km']))

    # history payloads can be a JSON string, a dict-of-list, or a bare list
    class _C:
        def __init__(self, payload):
            self.payload = payload

        def request(self, *a, **k):
            return {'json': self.payload}

    r1 = fetch_recent(_C({'error': 10000,
                          'data': '[{"totalDis":800,"sportTime":300}]'}), None)
    out.append(('fetch_recent unwraps string data',
                len(r1) == 1 and abs(r1[0]['distance_km'] - 0.8) < 1e-6,
                [x['distance_km'] for x in r1]))
    r2 = fetch_recent(_C({'error': 10000,
                          'data': {'list': [{'totalDis': 2500,
                                             'sportTime': 900}]}}), None)
    out.append(('fetch_recent reads dict-of-list',
                len(r2) == 1 and abs(r2[0]['distance_km'] - 2.5) < 1e-6,
                [x['distance_km'] for x in r2]))
    r3 = fetch_recent(_C({'error': 401, 'message': 'nope'}), None)
    out.append(('fetch_recent quiet on error', r3 == [], str(r3)))
    return out


if __name__ == '__main__':
    ok = True
    for name, passed, detail in self_test():
        print('%-30s %s   %s' % (name, 'PASS' if passed else 'FAIL', detail))
        ok = ok and passed
    print('\nRESULT:', 'PASS' if ok else 'FAIL')

    print('\n--- 示例：未来 14 天 ---')
    print(render_plan(plan(14, rng=random.Random(20261020))))
    raise SystemExit(0 if ok else 1)

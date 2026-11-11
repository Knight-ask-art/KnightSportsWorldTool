"""Offline P0 benchmark for route memory.

Two stages:

1. **Clustering** — an abstract 1.4 km stage validates that four route families
   stay apart, direction is invariant, laps collapse to one, and the medoid is
   a real input line.
2. **Campus** — the real fence/checkpoint fixture validates that a learned
   campus loop can be suggested inside the fence, snapped through all
   checkpoints, and applied without forging a run.

The script is deterministic and never touches the network in synthetic mode.

    python tools/route_memory_spike.py --fixture synthetic \
        --report docs/plans/route-memory/P0-REPORT.md
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), 'app')
if APP not in sys.path:
    sys.path.insert(0, APP)

from replica.application import route_memory as RM  # noqa: E402
from replica.application import readonly as RO  # noqa: E402
from replica.domain import routes as ROUTES  # noqa: E402
from replica.domain import trajectory as T  # noqa: E402

ORIGIN = (23.0, 113.5)
CAMPUS_CENTER = (22.4108, 112.9408)
LAUNCH = random.Random(20261112)


# ---------------------------------------------------------------------------
# geometry helpers (local metres -> lat/lng)
# ---------------------------------------------------------------------------

def _ll(lat0: float, lng0: float, x_m: float, y_m: float):
    m_lat = 110540.0
    m_lng = 111320.0 * math.cos(math.radians(lat0))
    return (lat0 + y_m / m_lat, lng0 + x_m / m_lng)


def _stadium(a_m: float, b_m: float, n: int = 96):
    return [_ll(ORIGIN[0], ORIGIN[1], a_m * math.cos(2 * math.pi * i / n),
                b_m * math.sin(2 * math.pi * i / n)) for i in range(n)]


def _straight(length_m: float, step_m: float = 15.0):
    n = max(2, int(length_m / step_m))
    return [_ll(ORIGIN[0], ORIGIN[1], length_m * i / (n - 1), 0.0) for i in range(n)]


def _figure8(a_m: float, n: int = 160):
    out = []
    for i in range(n):
        t = 2 * math.pi * i / n
        out.append(_ll(ORIGIN[0], ORIGIN[1],
                       a_m * math.cos(t), a_m * math.sin(2 * t) / 2.0))
    return out


def _oval(a_m: float, b_m: float, n: int = 90):
    return _stadium(a_m, b_m, n)


def _densify(line, step_m: float = 15.0):
    out = [line[0]]
    for a, b in zip(line, line[1:]):
        n = max(1, int(T.haversine_m(a, b) / step_m))
        for k in range(1, n + 1):
            f = k / n
            out.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
    return out


def _rotate_start(ring, jitter_m: float, rng: random.Random):
    ring = list(ring)
    if len(ring) < 3 or jitter_m <= 0:
        return ring
    target = rng.uniform(0.0, jitter_m)
    acc = 0.0
    for i in range(1, len(ring)):
        acc += T.haversine_m(ring[i - 1], ring[i])
        if acc >= target:
            return ring[i:-1] + ring[:i] + [ring[i]]
    return ring


def _laps(ring, laps: int):
    core = list(ring)
    if len(core) > 1 and T.haversine_m(core[0], core[-1]) < 1e-3:
        core = core[:-1]
    out = core * max(1, laps)
    out.append(core[0])
    return out


def _smooth(line):
    if len(line) < 5:
        return list(line)
    out = []
    for i in range(len(line)):
        lo = max(0, i - 2)
        hi = min(len(line), i + 3)
        count = hi - lo
        out.append((sum(p[0] for p in line[lo:hi]) / count,
                    sum(p[1] for p in line[lo:hi]) / count))
    return out


def _noise(line, sigma_m: float, rng: random.Random):
    if sigma_m <= 0:
        return list(line)
    m_lat = 110540.0
    m_lng = 111320.0 * math.cos(math.radians(line[0][0]))
    noisy = [(la + rng.gauss(0.0, sigma_m) / m_lat,
              ln + rng.gauss(0.0, sigma_m) / m_lng) for la, ln in line]
    return _smooth(noisy)


def _track(line, rrid: str, distance_m=None, **extra):
    item = {
        'rrid': rrid,
        'line': line,
        'distance_m': (round(T.polyline_length_m(line), 1)
                       if distance_m is None else float(distance_m)),
        'duration_s': 600,
        'start_ms': 1700000000000 + abs(hash(rrid)) % 100000,
        'is_cheat': 0,
        'complete': True,
        'source': 'server',
    }
    item.update(extra)
    return item


# ---------------------------------------------------------------------------
# stage 1: clustering on an abstract 1.4 km stage
# ---------------------------------------------------------------------------

def _stage1() -> tuple[dict, list[dict], dict]:
    stadium = _densify(_stadium(260.0, 200.0) + [_stadium(260.0, 200.0)[0]])
    open_line = _densify(_straight(2100.0))
    fig8 = _densify(_figure8(180.0) + [_figure8(180.0)[0]])
    oval = _densify(_oval(75.0, 50.0) + [_oval(75.0, 50.0)[0]])

    tracks: list[dict] = []
    a_lines: list[list[tuple[float, float]]] = []
    for i in range(8):
        ring = _rotate_start(stadium, 80.0, LAUNCH)
        laps = 2 if i < 6 else 3
        line = _noise(_laps(ring, laps), 8.0, LAUNCH)
        a_lines.append(line)
        tracks.append(_track(line, f'A{i}'))
    for i in range(4):
        line = _noise(open_line, 7.0, LAUNCH)
        tracks.append(_track(line, f'B{i}', distance_m=2100.0))
    for i in range(3):
        line = _noise(_laps(fig8, 1), 8.0, LAUNCH)
        tracks.append(_track(line, f'C{i}', distance_m=1100.0))
    for i in range(3):
        line = _noise(_laps(oval, 4 + i), 6.0, LAUNCH)
        tracks.append(_track(line, f'D{i}'))

    # Junk that must be rejected before clustering.
    tracks += [
        _track(stadium, 'junk-tool', source='tool'),
        _track(stadium, 'junk-cheat', is_cheat=1),
        _track(stadium, 'junk-incomplete', complete=False),
        _track(stadium[:12], 'junk-sparse'),
        _track(stadium, 'junk-far', distance_m=25000.0),
    ]
    spiked = list(stadium[:20]) + [
        _ll(stadium[20][0], stadium[20][1], 2000.0, 0.0), stadium[20],
    ] + list(stadium[21:])
    tracks.append(_track(spiked, 'junk-spike', distance_m=1500.0))

    t0 = time.perf_counter()
    doc = RM.learn_from_tracks(tracks, owner='p0:p0',
                               campus={'center': list(ORIGIN)})
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return doc, a_lines, {'ms': elapsed_ms}


def _stage2() -> dict:
    with open(os.path.join(APP, 'fixtures', 'campus_fence_47203.json'),
              encoding='utf-8') as f:
        fence_doc = json.load(f)
    with open(os.path.join(APP, 'fixtures', 'campus_points_47203.json'),
              encoding='utf-8') as f:
        points_doc = json.load(f)
    ring = [(float(p['glat']), float(p['glon'])) for p in fence_doc['polygon']]
    cps = [{'lat': float(p['lat']), 'lon': float(p['lon']),
            'isMustPoint': bool(p.get('isMustPoint')),
            'name': p.get('name', '')} for p in points_doc['points']]
    ordered = sorted(cps, key=lambda p: math.atan2(
        p['lat'] - CAMPUS_CENTER[0], p['lon'] - CAMPUS_CENTER[1]))
    base = [(p['lat'], p['lon']) for p in ordered]
    base.append(base[0])
    loop = _densify(base)

    tracks = []
    for i in range(6):
        line = _noise(_laps(loop, 3), 3.0, LAUNCH)
        tracks.append(_track(line, f'campus{i}'))
    tracks += [
        _track(loop, 'junk-tool', source='tool'),
        _track(loop, 'junk-cheat', is_cheat=1),
        _track(loop, 'junk-incomplete', complete=False),
        _track(loop[:4], 'junk-sparse'),
        _track(loop, 'junk-far', distance_m=25000.0),
    ]
    doc = RM.learn_from_tracks(tracks, owner='p0:campus',
                               campus={'center': list(CAMPUS_CENTER)})
    cands = RM.suggest(doc, 2.5, cps, fence=[ring], center=CAMPUS_CENTER)
    cands3 = RM.suggest(doc, 3.0, cps, fence=[ring], center=CAMPUS_CENTER)
    applied = (RM.apply_template(doc, cands[0]['template_id'], 2.5, points=cps)
               if cands else {'ok': False, 'error': 'no candidate'})
    return {
        'doc': doc,
        'points': cps,
        'candidates': cands,
        'candidates_3k': cands3,
        'applied': applied,
        'fence': [ring],
        'loop': loop,
    }


# ---------------------------------------------------------------------------
# assertions
# ---------------------------------------------------------------------------

def _assert(checks: list[dict], name: str, ok: bool, actual, expected=''):
    checks.append({'name': name, 'ok': bool(ok),
                   'actual': actual, 'expected': expected})


def _run() -> tuple[list[dict], dict, dict]:
    checks: list[dict] = []
    doc, a_lines, timing = _stage1()

    _assert(checks, 'seen/readable/skipped',
            (doc['records_seen'], doc['records_readable'], doc['records_skipped'])
            == (24, 18, 6),
            (doc['records_seen'], doc['records_readable'], doc['records_skipped']),
            (24, 18, 6))
    _assert(checks, 'four templates', len(doc['templates']) == 4,
            len(doc['templates']), 4)
    samples = sorted([t['samples'] for t in doc['templates']], reverse=True)
    _assert(checks, 'samples 8/4/3/3', samples == [8, 4, 3, 3], samples,
            [8, 4, 3, 3])

    by_samples = {t['samples']: t for t in doc['templates']}
    d_tpl = by_samples.get(3)
    # There are two 3-sample templates; pick the one closest to 400 m.
    three = [t for t in doc['templates'] if t['samples'] == 3]
    d_tpl = min(three, key=lambda t: abs(t['lap_m'] - 400.0)) if three else None
    _assert(checks, '400 m lap 380-430',
            bool(d_tpl and 380.0 <= d_tpl['lap_m'] <= 430.0),
            d_tpl['lap_m'] if d_tpl else None, '380-430')

    a_tpl = by_samples.get(8)
    _assert(checks, 'A medoid distance < 40 m',
            bool(a_tpl and a_tpl['medoid_distance_m'] < 40.0),
            a_tpl['medoid_distance_m'] if a_tpl else None, '<40')

    # medoid is a real input line, not an averaged line
    real_hits = 0
    if a_tpl:
        for line in a_lines:
            simp = T.simplify(line, 2.5, 400)
            single = RM._extract_single_lap(simp)
            if single is None:
                continue
            canon = T.canonical_loop(single)
            if [list(p) for p in canon] == a_tpl['line']:
                real_hits += 1
    _assert(checks, 'template line equals a real input lap',
            real_hits >= 1, real_hits, '>=1')

    # direction invariance: same source lines reversed -> same shape
    rev_tracks = [_track(list(reversed(line)), f'rev{i}')
                  for i, line in enumerate(a_lines[:8])]
    rev_doc = RM.learn_from_tracks(rev_tracks, owner='p0:rev',
                                   campus={'center': list(ORIGIN)})
    rev_a = max(rev_doc['templates'], key=lambda t: t['samples'])
    same_shape = T.route_distance(
        a_tpl['line'], rev_a['line'], closed=True) < 10.0
    lap_close = abs(a_tpl['lap_m'] - rev_a['lap_m']) / a_tpl['lap_m'] < 0.02
    _assert(checks, 'direction invariant shape', same_shape,
            round(T.route_distance(a_tpl['line'], rev_a['line'], closed=True), 3), '<10 m')
    _assert(checks, 'direction invariant lap', lap_close,
            (a_tpl['lap_m'], rev_a['lap_m']), 'within 2%')

    doc2 = RM.learn_from_tracks(
        [_track(line, f'A{i}') for i, line in enumerate(a_lines)], owner='p0:p0',
        campus={'center': list(ORIGIN)})
    a2 = max(doc2['templates'], key=lambda t: t['samples']) if doc2['templates'] else None
    _assert(checks, 'stable template ids', bool(a2 and a2['id'] == a_tpl['id']),
            (a_tpl['id'] if a_tpl else None, a2['id'] if a2 else None), 'same')

    c25 = RM.suggest(doc, 2.5, [])
    c30 = RM.suggest(doc, 3.0, [])
    same_id = bool(
        a_tpl
        and any(c['template_id'] == a_tpl['id'] for c in c25)
        and any(c['template_id'] == a_tpl['id'] for c in c30)
    )
    _assert(checks, '2.5/3.0 both suggest the A template', same_id,
            ([c['template_id'] for c in c25], [c['template_id'] for c in c30]),
            'contains A id')

    stage2 = _stage2()
    sdoc = stage2['doc']
    cps = stage2['points']
    ring = stage2['fence']
    top = (stage2['candidates'][0] if stage2['candidates'] else None)
    _assert(checks, 'campus summary ok', RM.summarize(sdoc)['status'] == 'ok',
            RM.summarize(sdoc)['status'], 'ok')
    _assert(checks, 'campus candidate exists', top is not None,
            len(stage2['candidates']), '>=1')
    if top:
        covered = len(cps) - len(top['missed'])
        _assert(checks, 'top candidate >=4/5 points', covered >= 4, covered, '>=4')
        _assert(checks, 'top candidate snapped 5/5', len(top['missed']) == 0,
                len(top['missed']), 0)
        _assert(checks, 'top candidate fence 0 outside',
                ROUTES.outside_fences(top['line'], ring)['count'] == 0,
                ROUTES.outside_fences(top['line'], ring)['count'], 0)
    applied = stage2['applied']
    _assert(checks, 'apply ok', applied.get('ok') is True,
            applied.get('ok'), True)
    if applied.get('ok'):
        _assert(checks, 'apply waypoints <=400',
                len(applied.get('waypoints') or []) <= 400,
                len(applied.get('waypoints') or []), '<=400')
        _assert(checks, 'apply laps 1-30',
                1 <= int(applied.get('laps') or 0) <= 30,
                applied.get('laps'), '1-30')
        _assert(checks, 'apply missed 0',
                len(applied.get('missed') or []) == 0,
                len(applied.get('missed') or []), 0)

    _assert(checks, 'learning time <300 ms', timing['ms'] < 300.0,
            round(timing['ms'], 1), '<300 ms')

    return checks, doc, {'stage2': stage2, 'timing': timing, 'a_tpl': a_tpl,
                         'd_tpl': d_tpl}


# ---------------------------------------------------------------------------
# real account (optional; read-only)
# ---------------------------------------------------------------------------

def _find_account(raw, key):
    if not isinstance(raw, dict):
        return None
    if key in raw and not isinstance(raw[key], (dict, list)):
        return raw[key]
    for value in raw.values():
        if isinstance(value, dict):
            hit = _find_account(value, key)
            if hit is not None:
                return hit
    return None


def _run_account(path: str, limit: int) -> dict:
    import types
    from replica.transport import realclient as R

    with open(path, encoding='utf-8') as f:
        raw = json.load(f)
    token = _find_account(raw, 'token')
    uid = _find_account(raw, 'uid')
    unid = _find_account(raw, 'unid')
    if not token or not uid or not unid:
        raise RuntimeError('session 文件里没有 uid/unid/token')
    acct = types.SimpleNamespace(uid=uid, unid=unid, token=token,
                                 center_lat=0.0, center_lng=0.0)
    rc = R.RealClient()
    listing = RO.recent_records(rc, acct, limit=limit)
    if not listing.get('ok'):
        raise RuntimeError('记录列表查询失败: %s'
                           % (listing.get('error') or listing.get('message')))
    tracks = []
    for row in listing.get('records') or []:
        detail = RO.record_detail(rc, acct, row.get('rrid'))
        if not detail.get('ok'):
            continue
        tracks.append({
            'rrid': str(row.get('rrid') or ''),
            'line': RM.track_from_detail(detail),
            'distance_m': (detail.get('detail') or {}).get('distance_m', 0.0),
            'duration_s': (detail.get('detail') or {}).get('duration_s', 0),
            'start_ms': (detail.get('detail') or {}).get('start_ms', 0),
            'is_cheat': (detail.get('detail') or {}).get('is_cheat', 0),
            'complete': bool((detail.get('detail') or {}).get('complete')),
            'source': 'server',
        })
    doc = RM.learn_from_tracks(tracks, owner='real:real')
    return {'seen': len(tracks), 'summary': RM.summarize(doc),
            'templates': len(doc['templates'])}


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def _write_report(path: str, checks: list[dict], doc: dict,
                  extra: dict, real: dict | None) -> str:
    lines = [
        '# Route Memory — P0 合成验证报告',
        '',
        '- 日期：2026-11-12',
        '- 脚本：`tools/route_memory_spike.py --fixture synthetic`',
        '- 目的：在不使用真实轨迹的前提下，验证单圈提取、聚类、方向不变、',
        '  真实围栏/点位适配与审计前适配。',
        '',
        '## 1. 聚类阶段（抽象 1.4 km 场景）',
        '',
        '| 模板 | closed | lap_m | samples | medoid_m |',
        '|---|---|---|---|---|',
    ]
    for tpl in doc['templates']:
        lines.append('| %s | %s | %.1f | %d | %.1f |' % (
            tpl['name'], tpl['closed'], tpl['lap_m'], tpl['samples'],
            tpl['medoid_distance_m']))
    lines += ['', '> 该阶段离真实校区很远，不参与围栏判断；只验证几何。', '',
              '## 2. 真实校区阶段', '',
              '使用 `campus_fence_47203.json` 与 `campus_points_47203.json`。',
              '']
    s2 = extra['stage2']
    if s2['candidates']:
        top = s2['candidates'][0]
        lines.append('- top1：%s，%.1f m/圈 × %d 圈，漏点 %d' % (
            top['name'], top['lap_m'], top['laps'], len(top['missed'])))
        lines.append('- top1 围栏外点数：%d' % ROUTES.outside_fences(
            top['line'], s2['fence'])['count'])
    else:
        lines.append('- 没有候选路线')
    lines += ['', '## 3. 断言', '', '| 断言 | 结果 | 实际 | 期望 |', '|---|---|---|---|']
    passed = 0
    for c in checks:
        if c['ok']:
            passed += 1
        lines.append('| %s | %s | %s | %s |' % (
            c['name'], 'PASS' if c['ok'] else 'FAIL',
            json.dumps(c['actual'], ensure_ascii=False, default=str),
            c['expected']))
    lines += ['', '## 4. 性能', '',
              '- 学习 24 条（18 有效）耗时：%.1f ms（门限 300 ms）' % extra['timing']['ms'],
              '', '## 5. 真实数据复验', '']
    if real is None:
        lines += [
            '- **未验证**。本机备份 token 返回 `Unauthorized token有误或缓存中token不存在`，',
            '  且没有 GPX/FIT/TCX 轨迹；`--account` 模式未运行或失败。',
            '- 不能把本报告的合成结果当成真实数据结论。',
        ]
    else:
        lines.append('- `--account` 结果：%s' % json.dumps(real, ensure_ascii=False))
    lines += ['', '## 6. 结论', '',
              '%d/%d 项断言通过。' % (passed, len(checks)), '',
              '真实记录验证仍阻塞；产品内用弱数据门槛兜底。', '']
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--fixture', default='synthetic', choices=['synthetic'])
    ap.add_argument('--account', default=None)
    ap.add_argument('--limit', type=int, default=20)
    ap.add_argument('--report', default=None)
    args = ap.parse_args()

    checks, doc, extra = _run()
    real = None
    if args.account:
        try:
            real = _run_account(args.account, args.limit)
        except Exception as exc:  # noqa: BLE001
            real = {'error': str(exc)}
    report = args.report or os.path.join(
        os.path.dirname(HERE), 'docs', 'plans', 'route-memory', 'P0-REPORT.md')
    _write_report(report, checks, doc, extra, real)
    summary = {
        'templates': len(doc['templates']),
        'assertions': [{'name': c['name'], 'ok': c['ok'],
                        'actual': c['actual']} for c in checks],
        'passed': sum(1 for c in checks if c['ok']),
        'total': len(checks),
        'report': report,
        'real': real,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if summary['passed'] == summary['total'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

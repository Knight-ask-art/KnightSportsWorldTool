"""Pre-submit and post-record audit for the 7.4.0 shape.

The audit is intentionally independent of the UI.  It answers one question:
"does this object still look like the evidence we have?"  It never submits and
never writes a file.
"""

from __future__ import annotations

import base64
import gzip
import json
import math

from .. import build_info
from ..domain import safety

OFFICIAL_POINT_KEYS = {
    'avgSpeed', 'bdA', 'bdD', 'bdG', 'bdS', 'coorType', 'count', 'dtr',
    'flag', 'gLat', 'gLng', 'gainTime', 'id', 'lat', 'lng', 'locType',
    'locationId', 'queueNum', 'radius', 'speed', 'state', 'stepDistance',
    'totalDis', 'totalTime', 'type', 'validDis', 'validTime',
}
OFFICIAL_SEGMENT_KEYS = {
    'avgSpeed', 'avgStep', 'distance', 'endTime', 'flag', 'id',
    'intervalStepModel', 'originAvgStep', 'speedStandard', 'startTime',
    'state',
}
OFFICIAL_OBS_KEYS = {
    'rrid', 'uuid', 'uid', 'run_data', 'extension_json', 'fixed_point_json',
    'segment_json', 'speed_json', 'step_freq_json', 'laps_json', 'runFaceCheck',
}


def _check(checks, cid, label, ok, detail='', level='error'):
    checks.append({
        'id': cid,
        'label': label,
        'ok': bool(ok),
        'level': level,
        'detail': str(detail)[:500],
    })


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _b64_json(value):
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return json.loads(gzip.decompress(base64.b64decode(value)).decode())
    except Exception:  # noqa: BLE001
        try:
            return json.loads(value)
        except Exception:  # noqa: BLE001
            return None


def _fence_list(raw):
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:  # noqa: BLE001
            return []
    return raw if isinstance(raw, list) else []


def _summary(checks):
    errors_n = sum(1 for c in checks if not c['ok'] and c['level'] == 'error')
    warns = sum(1 for c in checks if not c['ok'] and c['level'] == 'warn')
    passed = sum(1 for c in checks if c['ok'])
    return {'ok': errors_n == 0, 'passed': passed,
            'failed': errors_n, 'warned': warns}


def _pack(checks, record=None):
    return {
        'summary': _summary(checks),
        'checks': checks,
        'record': record or {},
        'at': safety.fmt_beijing(safety.now_ms()),
        'build': build_info.version_string(False),
    }


def audit_generated(run: dict, key_mode: str = 'single') -> dict:
    """Audit one object returned by ``generate_run_data``.

    ``key_mode`` mirrors the saved OBS compatibility setting, so a legacy
    install is not told that its intentional second PUT is a failure.
    """
    checks = []
    if not isinstance(run, dict):
        _check(checks, 'run.object', '生成数据是对象', False, type(run).__name__)
        return _pack(checks)

    points = run.get('locPoints') or []
    sport_time = int(_num(run.get('sportTime')))
    total_dis = _num(run.get('juli'))
    total_steps = int(_num(run.get('total_steps')))
    first = (run.get('locPoints') or [{}])[0]
    t0 = int(_num(first.get('flag')))

    # ---- track points ---------------------------------------------------
    keys_ok = bool(points) and all(
        isinstance(p, dict) and set(p) == OFFICIAL_POINT_KEYS for p in points)
    _check(checks, 'points.keys', '轨迹点 27 字段',
           keys_ok,
           '' if keys_ok else '第一点: %s' % sorted(points[0]) if points else '无点')
    _check(checks, 'points.no-steps', '轨迹点没有 steps',
           bool(points) and all('steps' not in p for p in points))
    _check(checks, 'points.lat-sentinel', 'lat/lng=-1，gLat/gLng 有效',
           bool(points) and all(
               _num(p.get('lat'), 999) == -1.0
               and _num(p.get('lng'), 999) == -1.0
               and _num(p.get('gLat')) != 0
               and _num(p.get('gLng')) != 0 for p in points))
    ids_ok = bool(points) and [p.get('id') for p in points] == list(
        range(1, len(points) + 1))
    _check(checks, 'points.ids', '点 id 从 1 连续递增', ids_ok,
           '' if ids_ok else 'id 序列不连续')
    queue_ok = bool(points) and all(
        int(_num(p.get('queueNum'))) == max(0, (int(_num(p.get('totalTime'))) - 1) // 30)
        for p in points)
    _check(checks, 'points.queue', 'queueNum 每 30 s 一批', queue_ok)
    speed_ok = True
    speed_bad = None
    for p in points:
        pace = _num(p.get('speed'))
        mps = _num(p.get('avgSpeed'))
        if pace > 0 and mps > 0:
            expected = 16.6667 / pace
            if abs(expected - mps) > 0.05:
                speed_ok = False
                speed_bad = {'speed': pace, 'avgSpeed': mps, 'expected': expected}
                break
    _check(checks, 'points.pace', 'speed 是配速，avgSpeed=m/s',
           speed_ok, speed_bad or '')
    _check(checks, 'points.type', 'type 在客户端域内',
           bool(points) and all(int(_num(p.get('type'))) in {-1, 0, 1, 2, 5, 6}
                                for p in points))
    _check(checks, 'points.start-end', '起点 type=5，末点 type=6',
           bool(points) and int(_num(points[0].get('type'))) == 5
           and int(_num(points[-1].get('type'))) == 6)
    gain_ok = all(isinstance(p.get('gainTime'), str)
                  and len(p['gainTime']) == 19 for p in points)
    _check(checks, 'points.gain-time', 'gainTime 是墙钟字符串', gain_ok,
           points[0].get('gainTime') if points else '')

    # ---- 10 s windows ---------------------------------------------------
    want_win = max(1, int(math.ceil(sport_time / 10.0)))
    speed = run.get('speed_json') or []
    steps = run.get('step_freq_json') or []
    _check(checks, 'windows.speed-count', 'speed_json 窗口数',
           len(speed) == want_win, '%d / %d' % (len(speed), want_win))
    _check(checks, 'windows.step-count', 'step_freq_json 窗口数',
           len(steps) == want_win, '%d / %d' % (len(steps), want_win))
    dist_sum = sum(_num(w.get('distance')) for w in speed)
    _check(checks, 'windows.distance', '窗口距离合计 = juli',
           abs(dist_sum - total_dis) <= max(1.0, total_dis * 0.001),
           '%.3f / %.3f' % (dist_sum, total_dis))
    step_sum = sum(int(_num(w.get('stepsNum'))) for w in steps)
    _check(checks, 'windows.steps', '窗口步数合计 = total_steps',
           step_sum == total_steps, '%d / %d' % (step_sum, total_steps))
    _check(checks, 'windows.queue', '窗口 queueNum 每 3 窗一批',
           all(int(_num(w.get('queueNum'))) == i // 3
               for i, w in enumerate(speed))
           and all(int(_num(w.get('queueNum'))) == i // 3
                   for i, w in enumerate(steps)))

    # ---- 30 s segments --------------------------------------------------
    seg = run.get('segment_json') or []
    want_seg = max(1, int(math.ceil(sport_time / 30.0)))
    _check(checks, 'segments.count', '分段数 = ceil(sportTime/30s)',
           len(seg) == want_seg, '%d / %d' % (len(seg), want_seg))
    _check(checks, 'segments.keys', '分段 11 字段',
           bool(seg) and all(isinstance(s, dict) and set(s) == OFFICIAL_SEGMENT_KEYS
                             for s in seg))
    seg_dist = sum(_num(s.get('distance')) for s in seg)
    _check(checks, 'segments.distance', '分段距离合计 = juli',
           abs(seg_dist - total_dis) <= max(1.0, total_dis * 0.001),
           '%.3f / %.3f' % (seg_dist, total_dis))
    _check(checks, 'segments.speed-standard', 'speedStandard=900',
           bool(seg) and all(int(_num(s.get('speedStandard'))) == 900 for s in seg))
    seg_pace_ok = True
    for s in seg:
        d = _num(s.get('distance'))
        sec = max(1.0, (_num(s.get('endTime')) - _num(s.get('startTime'))) / 1000.0)
        pace = _num(s.get('avgSpeed'))
        if d > 0 and abs(sec / 60.0 / (d / 1000.0) - pace) > 0.02:
            seg_pace_ok = False
            break
    _check(checks, 'segments.pace', '分段 avgSpeed 是配速', seg_pace_ok)

    # ---- five point / fence --------------------------------------------
    five = run.get('fivePoint') or []
    _check(checks, 'five.count', 'fivePoint 非空', bool(five), len(five))
    _check(checks, 'five.fields', 'fivePoint 必填字段',
           bool(five) and all(
               {'lat', 'lon', 'glat', 'glon', 'isPass'} <= set(p)
               for p in five if isinstance(p, dict)))
    entity = run.get('fivePointJson') or {}
    fences = _fence_list(entity.get('geoFencesJson'))
    _check(checks, 'fence.run-area', 'runAreaId 是整数',
           isinstance(entity.get('runAreaId'), int), entity.get('runAreaId'))
    if fences:
        fence_ok = all(
            isinstance(f, dict) and isinstance(f.get('points'), list)
            and all(_num(p.get('glat')) != 0 and _num(p.get('glng')) != 0
                    and _num(p.get('lat')) == 0 and _num(p.get('lng')) == 0
                    for p in f.get('points') or []) for f in fences)
        _check(checks, 'fence.points', '围栏点 lat/lng=0，glat/glng 有值', fence_ok)
    else:
        _check(checks, 'fence.points', '没有围栏时 geoFencesJson 为空', True,
               'null/[] 均可', 'info')

    # ---- OBS ------------------------------------------------------------
    obs_obj = None
    try:
        from ..transport import obs as OBS
        obs_obj = OBS.build_obs_object(
            run, 1, 'audit-uuid', 1,
            json.dumps(entity, separators=(',', ':')))
    except Exception as e:  # noqa: BLE001
        _check(checks, 'obs.build', 'OBS 对象可构造', False, e)
    if isinstance(obs_obj, dict):
        _check(checks, 'obs.keys', 'OBS 11 个键',
               set(obs_obj) == OFFICIAL_OBS_KEYS,
               sorted(obs_obj))
        _check(checks, 'obs.extension', 'extension_json 解码为空串',
               _b64_json(obs_obj.get('extension_json')) in ('', None)
               or not obs_obj.get('extension_json'))
        _check(checks, 'obs.segment-empty', 'segment_json 解码为空串',
               not obs_obj.get('segment_json')
               or _b64_json(obs_obj.get('segment_json')) in ('', None))
        run_data = _b64_json(obs_obj.get('run_data')) or {}
        raw_points = run_data.get('allLocJson') if isinstance(run_data, dict) else None
        try:
            raw_points = json.loads(raw_points) if isinstance(raw_points, str) else raw_points
        except Exception:  # noqa: BLE001
            raw_points = None
        _check(checks, 'obs.points', 'OBS run_data 27 字段点',
               isinstance(raw_points, list) and bool(raw_points)
               and set(raw_points[0]) == OFFICIAL_POINT_KEYS)
        try:
            want_keys = 2 if str(key_mode).lower() == 'legacy' else 1
            got_keys = len(OBS.obs_keys(run, 1, 'audit-uuid', mode=key_mode))
            _check(checks, 'obs.key-count',
                   'OBS 写 %d 个 key（%s）' % (want_keys, key_mode),
                   got_keys == want_keys, 'got %d' % got_keys)
        except Exception as e:  # noqa: BLE001
            _check(checks, 'obs.key-count', 'OBS key 数量', False, e)

    # ---- laps -----------------------------------------------------------
    try:
        from ..transport import obs as OBS
        laps = OBS.build_laps(run, t0)
        lap_sum = sum(_num(x.get('distance')) for x in laps)
        _check(checks, 'laps.distance', 'laps 距离合计 = juli',
               abs(lap_sum - total_dis) <= max(1.0, total_dis * 0.001),
               '%.3f / %.3f' % (lap_sum, total_dis))
        _check(checks, 'laps.pace', 'laps avgPace 分钟/公里',
               bool(laps) and all(x.get('avgPace') is not None for x in laps))
    except Exception as e:  # noqa: BLE001
        _check(checks, 'laps.distance', 'laps 可构造', False, e)

    # ---- built-in invariant checker ------------------------------------
    try:
        from ..domain.rundata import validate_run_data
        errs = validate_run_data(run)
        _check(checks, 'validate', 'validate_run_data 通过', not errs,
               '; '.join(errs))
    except Exception as e:  # noqa: BLE001
        _check(checks, 'validate', 'validate_run_data 可运行', False, e)

    return _pack(checks, {
        'points': len(points),
        'seconds': sport_time,
        'distance_m': round(total_dis, 1),
        'steps': total_steps,
        'windows': len(speed),
        'segments': len(seg),
    })


def audit_detail(detail: dict) -> dict:
    """Audit the normalised object returned by ``readonly.record_detail``."""
    checks = []
    if not isinstance(detail, dict) or not detail.get('ok'):
        _check(checks, 'detail.ok', '详情读取成功', False,
               (detail or {}).get('error') if isinstance(detail, dict) else type(detail).__name__)
        return _pack(checks)

    d = detail.get('detail') or {}
    track = detail.get('track') or []
    stats = detail.get('stats') or {}
    analysis = detail.get('analysis') or {}
    _check(checks, 'detail.track', '轨迹点 >= 2', len(track) >= 2, len(track))
    _check(checks, 'detail.source', '轨迹来源明确',
           stats.get('track_source') in ('inline', 'obs'),
           stats.get('track_source'))
    total_dis = _num(d.get('distance_m'))
    total_time = _num(d.get('duration_s'))
    _check(checks, 'detail.distance', '详情距离 > 0', total_dis > 0, total_dis)
    _check(checks, 'detail.duration', '详情时长 > 0', total_time > 0, total_time)
    if track:
        last = track[-1]
        _check(checks, 'detail.last-dis', '末点累计距离接近详情距离',
               abs(_num(last.get('distance_m')) - total_dis)
               <= max(5.0, total_dis * 0.02),
               '%s / %s' % (last.get('distance_m'), total_dis))
        _check(checks, 'detail.last-time', '末点累计时间接近详情时长',
               abs(_num(last.get('duration_s')) - total_time) <= 3,
               '%s / %s' % (last.get('duration_s'), total_time))
        ts = [_num(p.get('duration_s')) for p in track]
        _check(checks, 'detail.monotonic', '轨迹时间单调',
               all(b >= a for a, b in zip(ts, ts[1:])))
    _check(checks, 'detail.analysis', '图表序列可用',
           bool(analysis.get('series')), len(analysis.get('series') or []))
    _check(checks, 'detail.state', '平台判定不是 issue',
           d.get('state') != 'issue', d.get('state'))
    _check(checks, 'detail.cheat', 'isCheat 为 0/未知',
           d.get('is_cheat') in (0, None), d.get('is_cheat'))
    _check(checks, 'detail.obs', 'OBS 无错误或已用内联轨迹',
           not d.get('obs_error') or stats.get('track_source') == 'inline',
           d.get('obs_error'))
    return _pack(checks, {
        'rrid': d.get('rrid'),
        'uuid': d.get('uuid'),
        'points': len(track),
        'source': stats.get('track_source'),
    })


def audit_run(run: dict | None = None, detail: dict | None = None) -> dict:
    if detail is not None:
        return audit_detail(detail)
    return audit_generated(run or {})

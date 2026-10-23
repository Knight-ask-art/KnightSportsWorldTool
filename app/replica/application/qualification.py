"""User-defined school rules + pre-submit plausibility model.

The model deliberately does **not** guess school rules.  A school's valid
distance, pace band, time window and daily cap are entered by the user (see
``data/school_rules.json`` and the 设置 → 学校要求 page).  Every field is
optional: blank means “the school has no rule for this item” and is shown as
``未要求`` rather than treated as a failure.  The model reports
``configured: false`` only when the user has entered no rule at all.

What it checks after configuration:

Hard gates (a failure blocks submission unless explicitly ignored):
  * distance inside [min_km, max_km]
  * average pace inside [pace_min, pace_max] min/km
  * duration inside [min_minutes, max_minutes]
  * start time inside the configured window
  * start date between today and 3 days ago (Beijing calendar days)
  * every checkpoint marked ``isPass``

Soft checks (warnings, score deductions):
  * runs already on that date at/above the school's ``max_per_day``.  This is
    a reminder only — the school/server decides whether an extra run counts,
    so it never blocks submission.  An unreadable count is also a warning
    instead of being dropped: silence would look like "0 runs today".
  * cadence 140–200 spm
  * stride 0.45–1.75 m
  * 10-second speed range 1.6–6.5 m/s
  * pace coefficient of variation ≤ 12 %
  * GPS jump between adjacent points ≤ 100 m
  * total ascent ≤ 50 m on a campus run
  * per-km pace consistency

The output is a plain dict so the CLI and the web UI can share it:

    {'configured': True, 'score': 92, 'verdict': 'warn',
     'checks': [{'id': 'distance', 'label': '距离', 'status': 'pass',
                 'value': '2.41 km', 'detail': '要求 2.0–5.0 km',
                 'hard': True, 'penalty': 0}, ...],
     'warnings': ['配速波动 13.2% 偏高'], 'rules': {...}}
"""

from __future__ import annotations

import json
import math
import os
import statistics
import tempfile

from ..domain import safety

#: Every key is optional.  A non-empty key is enough to enable the model.
RULE_KEYS = (
    'min_km', 'max_km', 'pace_min', 'pace_max',
    'min_minutes', 'max_minutes', 'max_per_day',
    'step_min', 'step_max',
    'window_start', 'window_end',
)
REQUIRED = RULE_KEYS  # backward-compatible name for older callers

DEFAULT_RULES = {
    'min_km': None, 'max_km': None,
    'pace_min': None, 'pace_max': None,
    'min_minutes': None, 'max_minutes': None,
    'max_per_day': None,
    'step_min': None, 'step_max': None,
    'window_start': '', 'window_end': '',
    'school': '', 'campus': '',
    # True means: if the operator has never configured rules, the UI may fill
    # them from the server.  Explicitly saving an empty form sets it False so
    # auto-fill does not fight a deliberate “model off”.
    'auto_fetch': True,
}

#: Soft-check weights.  A warning subtracts this from 100.
PENALTY = {
    'cadence': 4, 'stride': 5, 'speed': 6, 'pace_cv': 5,
    'gps': 4, 'ascent': 2, 'lap': 4,
}


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def _base_dir() -> str:
    base = os.environ.get('JHP_DATA_DIR')
    if not base:
        base = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
    return base


def rules_path() -> str:
    return os.path.join(_base_dir(), 'school_rules.json')


def load_rules() -> dict:
    try:
        with open(rules_path(), encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_rules(rules: dict) -> dict:
    clean, errors = normalize_rules(rules)
    if errors:
        raise ValueError('; '.join(errors))
    os.makedirs(_base_dir(), exist_ok=True)
    path = rules_path()
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(clean, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return clean


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def _num(value, name, errors, integer=False):
    # Optional field: blank means “no school rule”, not a validation error.
    if value is None or str(value).strip() == '':
        return None
    try:
        v = float(str(value).strip())
    except (TypeError, ValueError):
        errors.append('%s 不是数字' % name)
        return None
    if integer:
        if abs(v - round(v)) > 1e-6:
            errors.append('%s 必须是整数' % name)
            return None
        v = int(round(v))
    if v < 0:
        errors.append('%s 不能为负' % name)
        return None
    return v


def _clock(value, name, errors) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    try:
        hh, mm = text.split(':')
        hh, mm = int(hh), int(mm)
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return '%02d:%02d' % (hh, mm)
    except (ValueError, TypeError):
        pass
    errors.append('%s 需要 HH:MM' % name)
    return ''


def normalize_rules(raw: dict) -> tuple[dict, list[str]]:
    """Return a clean rules dict and a list of validation errors."""
    raw = dict(raw or {})
    errors: list[str] = []
    out = dict(DEFAULT_RULES)
    out['school'] = str(raw.get('school') or '').strip()
    out['campus'] = str(raw.get('campus') or '').strip()
    flag = raw.get('auto_fetch', True)
    out['auto_fetch'] = not (
        flag is False or str(flag).strip().lower() in ('0', 'false', 'no', 'off'))

    out['min_km'] = _num(raw.get('min_km'), '最低距离', errors)
    out['max_km'] = _num(raw.get('max_km'), '最高距离', errors)
    out['pace_min'] = _num(raw.get('pace_min'), '配速下限', errors)
    out['pace_max'] = _num(raw.get('pace_max'), '配速上限', errors)
    out['min_minutes'] = _num(raw.get('min_minutes'), '最短时长', errors)
    out['max_minutes'] = _num(raw.get('max_minutes'), '最长时长', errors)
    out['max_per_day'] = _num(raw.get('max_per_day'), '每日上限', errors,
                             integer=True)
    out['step_min'] = _num(raw.get('step_min'), '步频下限', errors)
    out['step_max'] = _num(raw.get('step_max'), '步频上限', errors)
    out['window_start'] = _clock(raw.get('window_start'), '时间窗开始', errors)
    out['window_end'] = _clock(raw.get('window_end'), '时间窗结束', errors)

    for key, label in (
        ('min_km', '最低距离'), ('max_km', '最高距离'),
        ('pace_min', '配速下限'), ('pace_max', '配速上限'),
        ('min_minutes', '最短时长'), ('max_minutes', '最长时长'),
        ('step_min', '步频下限'), ('step_max', '步频上限'),
    ):
        if out[key] is not None and out[key] <= 0:
            errors.append('%s 必须大于 0' % label)
    if out['min_km'] is not None and out['max_km'] is not None:
        if out['max_km'] < out['min_km']:
            errors.append('距离范围不合法')
    if out['pace_min'] is not None and out['pace_max'] is not None:
        if out['pace_max'] < out['pace_min']:
            errors.append('配速范围不合法')
    if out['min_minutes'] is not None and out['max_minutes'] is not None:
        if out['max_minutes'] < out['min_minutes']:
            errors.append('时长范围不合法')
    if out['max_per_day'] is not None and out['max_per_day'] < 1:
        errors.append('每日上限至少 1 次')
    for key, label in (('step_min', '步频下限'), ('step_max', '步频上限')):
        if out[key] is not None and not (30 <= out[key] <= 300):
            errors.append('%s 需要在 30–300 spm 之间' % label)
    if (out['step_min'] is not None and out['step_max'] is not None
            and out['step_max'] < out['step_min']):
        errors.append('步频范围不合法')
    return out, errors


def is_configured(rules: dict | None) -> bool:
    """Any non-empty rule enables the model; blank fields are “未要求”."""
    if not isinstance(rules, dict):
        return False
    return any(
        rules.get(key) is not None
        and not (isinstance(rules.get(key), str) and not rules.get(key).strip())
        for key in RULE_KEYS
    )


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------

def _haversine_m(a, b) -> float:
    lat1, lng1 = math.radians(a[0]), math.radians(a[1])
    lat2, lng2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlng = lat2 - lat1, lng2 - lng1
    h = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2)
    return 6371000.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def _mins(hhmm: str) -> int:
    hh, mm = str(hhmm).split(':')
    return int(hh) * 60 + int(mm)


def _in_window(hhmm: str, start: str, end: str) -> bool:
    now, a, b = _mins(hhmm), _mins(start), _mins(end)
    if a == b:
        return True
    if a < b:
        return a <= now < b
    return now >= a or now < b


def _fmt_pace(pace: float | None) -> str:
    if pace is None or not math.isfinite(pace):
        return '—'
    return "%d'%02d\"" % (int(pace), int(round((pace % 1) * 60)) % 60)


def metrics(run: dict) -> dict:
    km = float(run.get('juli') or 0) / 1000.0
    seconds = float(run.get('sportTime') or 0)
    minutes = seconds / 60.0
    pace = (minutes / km) if km > 0 else None
    steps = int(run.get('total_steps') or 0)
    cadence = (steps / minutes) if minutes > 0 else None
    stride = (float(run.get('juli') or 0) / steps) if steps > 0 else None

    buckets = [float(b.get('distance') or 0) / 10.0
               for b in (run.get('speed_json') or []) if b.get('distance')]
    speeds = [s for s in buckets if s > 0]
    speed_min = min(speeds) if speeds else None
    speed_max = max(speeds) if speeds else None
    pace_cv = None
    if len(speeds) >= 3:
        mean = statistics.fmean(speeds)
        if mean > 0:
            pace_cv = statistics.pstdev(speeds) / mean

    locs = run.get('locPoints') or run.get('allLocJson') or []
    jumps = []
    for a, b in zip(locs, locs[1:]):
        try:
            jumps.append(_haversine_m(
                (float(a.get('gLat', a.get('lat'))), float(a.get('gLng', a.get('lng')))),
                (float(b.get('gLat', b.get('lat'))), float(b.get('gLng', b.get('lng'))))))
        except (TypeError, ValueError):
            pass
    max_jump = max(jumps) if jumps else None

    alts = []
    for p in locs:
        try:
            alts.append(float(p.get('bdA')))
        except (TypeError, ValueError):
            pass
    ascent = float(run.get('totalAscent') or 0)
    if not ascent and alts:
        ascent = sum(max(0.0, b - a) for a, b in zip(alts, alts[1:]))

    start_ms = None
    if locs:
        try:
            start_ms = int(locs[0].get('flag'))
        except (TypeError, ValueError):
            start_ms = None
    start_hhmm = None
    start_date = None
    days_ago = None
    if start_ms:
        start_bj = safety.to_beijing(start_ms)
        start_hhmm = start_bj.strftime('%H:%M')
        start_date = start_bj.strftime('%Y-%m-%d')
        today = safety.to_beijing(safety.now_ms()).date()
        days_ago = (today - start_bj.date()).days

    five = run.get('fivePoint') or []
    passed = sum(1 for p in five if p.get('isPass'))
    return {
        'km': km, 'seconds': seconds, 'minutes': minutes, 'pace': pace,
        'steps': steps, 'cadence': cadence, 'stride': stride,
        'speed_min': speed_min, 'speed_max': speed_max, 'pace_cv': pace_cv,
        'max_jump': max_jump, 'ascent': ascent,
        'start_ms': start_ms, 'start_hhmm': start_hhmm,
        'start_date': start_date, 'days_ago': days_ago,
        'five_total': len(five), 'five_passed': passed,
    }


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------

def evaluate(run: dict, rules: dict, checkpoints: list | None = None,
             daily_runs: int | None = None,
             daily_source: str | None = None) -> dict:
    rules, errors = normalize_rules(rules or {})
    if not is_configured(rules):
        return {
            'configured': False, 'score': None, 'verdict': 'unconfigured',
            'checks': [], 'warnings': [], 'rules': rules,
            'summary': '未配置学校要求',
            'errors': errors,
        }

    m = metrics(run)
    checks: list[dict] = []

    def add(cid, label, status, value, detail, hard=False, penalty=0):
        checks.append({
            'id': cid, 'label': label, 'status': status, 'value': value,
            'detail': detail, 'hard': bool(hard), 'penalty': penalty,
        })

    # ---- hard gates (only for rules the user actually entered) --------
    lo, hi = rules['min_km'], rules['max_km']
    if lo is None and hi is None:
        add('distance', '距离', 'skip', '—', '学校未填写距离要求')
    else:
        ok = ((lo is None or m['km'] >= lo)
              and (hi is None or m['km'] <= hi))
        if lo is not None and hi is not None:
            detail = '要求 %.1f–%.1f km' % (lo, hi)
        elif lo is not None:
            detail = '要求 ≥ %.1f km' % lo
        else:
            detail = '要求 ≤ %.1f km' % hi
        add('distance', '距离', 'pass' if ok else 'fail',
            '%.2f km' % m['km'], detail, True)

    lo, hi = rules['pace_min'], rules['pace_max']
    if lo is None and hi is None:
        add('pace', '配速', 'skip', '—', '学校未填写配速要求')
    else:
        pace_ok = (m['pace'] is not None
                   and (lo is None or m['pace'] >= lo)
                   and (hi is None or m['pace'] <= hi))
        if lo is not None and hi is not None:
            detail = '要求 %s–%s /km' % (_fmt_pace(lo), _fmt_pace(hi))
        elif lo is not None:
            detail = '要求 ≥ %s /km' % _fmt_pace(lo)
        else:
            detail = '要求 ≤ %s /km' % _fmt_pace(hi)
        add('pace', '配速', 'pass' if pace_ok else 'fail',
            _fmt_pace(m['pace']) + '/km', detail, True)

    lo, hi = rules['min_minutes'], rules['max_minutes']
    if lo is None and hi is None:
        add('duration', '时长', 'skip', '—', '学校未填写时长要求')
    else:
        time_ok = ((lo is None or m['minutes'] >= lo)
                   and (hi is None or m['minutes'] <= hi))
        if lo is not None and hi is not None:
            detail = '要求 %d–%d 分' % (lo, hi)
        elif lo is not None:
            detail = '要求 ≥ %d 分' % lo
        else:
            detail = '要求 ≤ %d 分' % hi
        add('duration', '时长', 'pass' if time_ok else 'fail',
            '%.0f 分' % m['minutes'], detail, True)

    ws, we = rules['window_start'], rules['window_end']
    if not ws and not we:
        add('window', '开始时间', 'skip', '—', '学校未填写时间窗要求')
    else:
        if not m['start_hhmm']:
            window_ok = False
        elif ws and we:
            window_ok = _in_window(m['start_hhmm'], ws, we)
        elif ws:
            window_ok = _mins(m['start_hhmm']) >= _mins(ws)
        else:
            window_ok = _mins(m['start_hhmm']) < _mins(we)
        if ws and we:
            detail = '要求 %s–%s' % (ws, we)
        elif ws:
            detail = '要求 ≥ %s' % ws
        else:
            detail = '要求 < %s' % we
        add('window', '开始时间', 'pass' if window_ok else 'fail',
            m['start_hhmm'] or '—', detail, True)

    # Backfill freshness: the operator may pick today .. 3 days ago.  Anything
    # older (or in the future) is outside what the reference client offers, so
    # it is a hard fail rather than a silent coefficient.
    days_ago = m['days_ago']
    if days_ago is None:
        add('date', '日期', 'fail', '—', '轨迹缺少开始时间', True)
    else:
        date_ok = 0 <= days_ago <= safety.BACKFILL_DAYS
        weekday = ('周一', '周二', '周三', '周四', '周五', '周六', '周日')[
            safety.to_beijing(m['start_ms']).weekday()]
        add('date', '日期', 'pass' if date_ok else 'fail',
            '%s · %s' % (m['start_date'], weekday),
            '可选最近 %d 天（含今天，北京时间）' % safety.BACKFILL_DAYS,
            True)

    passed = m['five_passed']
    total = len(checkpoints) if checkpoints else m['five_total']
    if total:
        # fivePoint is generated from the same checkpoint list; when a caller
        # supplies the authoritative list, the isPass flags still decide.
        points_ok = passed >= total and m['five_passed'] == m['five_total']
        add('points', '打卡点', 'pass' if points_ok else 'fail',
            '%d/%d' % (passed, total), '全部点位需要 isPass', True)

    # Daily cap: this is the school's `max_per_day`, not the tool's local gate.
    # It must stay visible even when the count is unknown -- a missing check
    # reads as "0 runs today", which is exactly the wrong message.
    day_label = '今日' if days_ago == 0 else '当日'
    day_name = m['start_date'] or '所选日期'
    cap = rules['max_per_day']
    if cap is None:
        add('daily', day_label + '次数', 'skip', '—',
            '学校未填写每日次数上限（本地工具闸门仍会检查）')
    elif daily_runs is None:
        add('daily', day_label + '次数', 'warn', '—',
            '无法确认 %s 已有几次；提交前会再查服务器记录' % day_name)
    else:
        cap = int(cap)
        day_ok = daily_runs < cap
        if day_ok:
            detail = '%s 已有 %d 次，上限 %d 次；这次是第 %d 次' % (
                day_name, daily_runs, cap, daily_runs + 1)
        else:
            detail = ('%s 已有 %d 次，已达学校上限 %d 次；'
                      '再提交就超了（仅提醒，不阻止提交）'
                      % (day_name, daily_runs, cap))
        if daily_source == 'server':
            detail += '（服务器记录）'
        elif daily_source == 'local':
            detail += '（本地回执）'
        # Reminder, not a gate: the school/server decides whether an extra run
        # counts.  Keep the check visible as a warning, but never hard-fail.
        add('daily', day_label + '次数', 'pass' if day_ok else 'warn',
            '%d/%d' % (daily_runs, cap), detail, False)

    # ---- soft checks --------------------------------------------------
    if m['cadence'] is not None:
        step_lo, step_hi = rules['step_min'], rules['step_max']
        if step_lo is None and step_hi is None:
            ok = 140 <= m['cadence'] <= 200
            add('cadence', '步频', 'pass' if ok else 'warn',
                '%.0f spm' % m['cadence'], '常见 140–200 spm',
                penalty=PENALTY['cadence'])
        else:
            # A server-provided school range is a hard rule, like distance and
            # pace.  It is only present when the policy endpoint returned it.
            ok = ((step_lo is None or m['cadence'] >= step_lo)
                  and (step_hi is None or m['cadence'] <= step_hi))
            if step_lo is not None and step_hi is not None:
                detail = '学校要求 %d–%d spm' % (step_lo, step_hi)
            elif step_lo is not None:
                detail = '学校要求 ≥ %d spm' % step_lo
            else:
                detail = '学校要求 ≤ %d spm' % step_hi
            add('cadence', '步频', 'pass' if ok else 'fail',
                '%.0f spm' % m['cadence'], detail, True)

    if m['stride'] is not None:
        ok = 0.5 <= m['stride'] <= 1.7
        add('stride', '步幅', 'pass' if ok else 'warn',
            '%.2f m' % m['stride'], '常见 0.5–1.7 m',
            penalty=PENALTY['stride'])

    if m['speed_min'] is not None and m['speed_max'] is not None:
        ok = 1.9 <= m['speed_min'] and m['speed_max'] <= 6.3
        add('speed', '10 秒速度', 'pass' if ok else 'warn',
            '%.1f–%.1f m/s' % (m['speed_min'], m['speed_max']),
            '常见 1.9–6.3 m/s', penalty=PENALTY['speed'])

    if m['pace_cv'] is not None:
        ok = m['pace_cv'] <= 0.12
        add('pace_cv', '配速波动', 'pass' if ok else 'warn',
            '%.1f%%' % (m['pace_cv'] * 100), '建议 ≤ 12%',
            penalty=PENALTY['pace_cv'])

    if m['max_jump'] is not None:
        ok = m['max_jump'] <= 100
        add('gps', 'GPS 跳点', 'pass' if ok else 'warn',
            '最大 %.0f m' % m['max_jump'], '相邻点建议 ≤ 100 m',
            penalty=PENALTY['gps'])

    ok = m['ascent'] <= 50
    add('ascent', '累计爬升', 'pass' if ok else 'warn',
        '%.1f m' % m['ascent'], '校园跑建议 ≤ 50 m',
        penalty=PENALTY['ascent'])

    # ---- score --------------------------------------------------------
    warnings = [c['label'] + ' ' + c['value'] + ('：' + c['detail'])
                for c in checks if c['status'] == 'warn']
    skipped = [c for c in checks if c['status'] == 'skip']
    hard_fail = any(c['status'] == 'fail' for c in checks if c['hard'])
    score = 100 - sum(c['penalty'] for c in checks if c['status'] == 'warn')
    if hard_fail:
        score = min(score, 40)
    score = max(0, min(100, int(round(score))))
    verdict = 'fail' if hard_fail else ('warn' if warnings else 'pass')
    summary = {'pass': '合格', 'warn': '合格 · %d 项注意' % len(warnings),
               'fail': '不合格'}[verdict]
    if skipped:
        summary += ' · %d 项未要求' % len(skipped)
    return {
        'configured': True, 'score': score, 'verdict': verdict,
        'checks': checks, 'warnings': warnings, 'rules': rules,
        'summary': summary,
    }


# ---------------------------------------------------------------------------
# self test
# ---------------------------------------------------------------------------

def self_test() -> list[tuple[str, bool, str]]:
    from ..domain import rundata

    out = []
    rules = {
        'min_km': 2.0, 'max_km': 5.0, 'pace_min': 3.0, 'pace_max': 10.0,
        'min_minutes': 10, 'max_minutes': 90, 'max_per_day': 2,
        'window_start': '00:00', 'window_end': '23:59',
    }
    run = rundata.generate_run_data(2.5, 6.0, 172,
                                    center_lat=22.4108, center_lng=112.9408)
    model = evaluate(run, rules)
    out.append(('configured model runs', model['configured'] is True,
                model['verdict']))
    out.append(('score in range', 0 <= model['score'] <= 100, model['score']))
    out.append(('distance check present',
                any(c['id'] == 'distance' for c in model['checks']), ''))

    fail = evaluate(run, dict(rules, min_km=3.0))
    out.append(('short distance fails', fail['verdict'] == 'fail', fail['score']))

    pace_fail = evaluate(run, dict(rules, pace_max=5.0))
    out.append(('slow pace fails', pace_fail['verdict'] == 'fail',
                pace_fail['warnings']))

    fast = rundata.generate_run_data(2.5, 6.0, 230,
                                     center_lat=22.4108, center_lng=112.9408)
    cad = evaluate(fast, rules)
    out.append(('cadence warning', any(c['id'] == 'cadence' and
                                       c['status'] == 'warn'
                                       for c in cad['checks']), cad['score']))

    empty = evaluate(run, {})
    out.append(('unconfigured is explicit', empty['configured'] is False,
                empty['summary']))

    broken = rundata.generate_run_data(2.5, 6.0, 172,
                                       center_lat=22.4108, center_lng=112.9408)
    broken['fivePoint'][0]['isPass'] = False
    pts = evaluate(broken, rules)
    out.append(('missed checkpoint fails', pts['verdict'] == 'fail', pts['score']))

    partial, partial_errors = normalize_rules({'min_km': 2})
    out.append(('partial rules accepted',
                not partial_errors and is_configured(partial), partial_errors))
    partial_model = evaluate(run, {'min_km': 2})
    out.append(('partial rules evaluate only what is set',
                partial_model['configured'] is True
                and any(c['id'] == 'distance' and c['status'] == 'pass'
                        for c in partial_model['checks'])
                and any(c['status'] == 'skip' for c in partial_model['checks']),
                partial_model['summary']))

    # ---- backfill date + daily cap -----------------------------------
    day_ok = evaluate(run, rules, daily_runs=1, daily_source='server')
    dchk = [c for c in day_ok['checks'] if c['id'] == 'daily']
    out.append(('daily count present',
                bool(dchk) and dchk[0]['status'] == 'pass', dchk[:1]))

    over = evaluate(run, rules, daily_runs=2)
    ochk = [c for c in over['checks'] if c['id'] == 'daily']
    out.append(('daily cap is a reminder, not a hard fail',
                over['verdict'] != 'fail' and ochk
                and ochk[0]['status'] == 'warn'
                and '仅提醒' in ochk[0]['detail'], ochk[:1]))

    unknown = evaluate(run, rules)
    uchk = [c for c in unknown['checks'] if c['id'] == 'daily']
    out.append(('unknown daily count is a warning, not silence',
                bool(uchk) and uchk[0]['status'] == 'warn', uchk[:1]))

    yesterday_ms = safety.now_ms() - 24 * 3600 * 1000
    yrun = rundata.generate_run_data(2.5, 6.0, 172,
                                     center_lat=22.4108, center_lng=112.9408,
                                     start_ms=yesterday_ms)
    ymodel = evaluate(yrun, rules, daily_runs=0)
    ydchk = [c for c in ymodel['checks'] if c['id'] == 'date']
    out.append(('yesterday passes the date gate',
                ymodel['verdict'] == 'pass' and ydchk
                and ydchk[0]['status'] == 'pass', ydchk[:1]))

    old_ms = safety.now_ms() - 4 * 24 * 3600 * 1000
    orun = rundata.generate_run_data(2.5, 6.0, 172,
                                     center_lat=22.4108, center_lng=112.9408,
                                     start_ms=old_ms)
    omodel = evaluate(orun, rules, daily_runs=0)
    odchk = [c for c in omodel['checks'] if c['id'] == 'date']
    out.append(('4 days ago fails the date gate',
                omodel['verdict'] == 'fail' and odchk
                and odchk[0]['status'] == 'fail', odchk[:1]))

    # storage round-trip without touching the real data dir
    old = os.environ.get('JHP_DATA_DIR')
    tmp = tempfile.mkdtemp(prefix='jhp_rules_')
    os.environ['JHP_DATA_DIR'] = tmp
    try:
        saved = save_rules(rules)
        loaded = load_rules()
        out.append(('rules round-trip',
                    loaded.get('min_km') == 2.0 and saved.get('max_km') == 5.0,
                    loaded.get('min_km')))
        partial_saved = save_rules({'min_km': 2})
        out.append(('partial rules save',
                    partial_saved.get('min_km') == 2.0
                    and is_configured(partial_saved), partial_saved))
        out.append(('auto_fetch defaults on',
                    normalize_rules({})[0]['auto_fetch'] is True, 'True'))
        off = save_rules({'auto_fetch': False})
        out.append(('empty save keeps auto_fetch off',
                    off.get('auto_fetch') is False
                    and not is_configured(off), off))
    finally:
        if old is None:
            os.environ.pop('JHP_DATA_DIR', None)
        else:
            os.environ['JHP_DATA_DIR'] = old
    return out


if __name__ == '__main__':
    ok = True
    for name, passed, detail in self_test():
        print('%-36s %s   %s' % (name, 'PASS' if passed else 'FAIL', detail))
        ok = ok and passed
    print('\nRESULT:', 'PASS' if ok else 'FAIL')
    raise SystemExit(0 if ok else 1)

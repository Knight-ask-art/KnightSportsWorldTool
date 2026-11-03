"""Local training statistics built from receipts and (optionally) records.

The receipt store is the only local source of truth this module needs: every
submission writes a receipt (``guard.ReceiptStore``) with a start, a distance
and a final state.  Server records are accepted as an *optional* enrichment;
when the caller has none, the result says ``source="local"`` instead of
presenting the local numbers as the server's.

The module is read-only and never touches the network.
"""

from __future__ import annotations

import datetime as _dt
import tempfile

from ..domain import safety

WEEKDAYS = 7
DEFAULT_WEEKS = 12
MAX_WEEKS = 52
TOP_FAILURES = 5
WEEKDAY_LABELS = ('周一', '周二', '周三', '周四', '周五', '周六', '周日')

#: Receipt/record states that mean "the server kept the run".
_ACCEPTED = ('accepted', 'ok')
#: States that are finished but not successful.
_FAILED = ('rejected', 'fail', 'failed', 'error')
#: Not finished yet: neither success nor failure.
_OPEN = ('pending', 'unknown')


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def row_time(row: dict) -> int:
    """Epoch ms for a receipt or record row."""
    for key in ('start_ms', 'start', 'created'):
        value = _int(row.get(key))
        if value > 0:
            return value
    return 0


def row_distance_m(row: dict) -> float:
    if row.get('distance_m') is not None:
        return max(0.0, _num(row.get('distance_m')))
    if row.get('distance_km') is not None:
        return max(0.0, _num(row.get('distance_km')) * 1000.0)
    return 0.0


def row_duration_s(row: dict) -> float:
    for key in ('duration_s', 'sportTime', 'totalTime'):
        if row.get(key) is not None:
            return max(0.0, _num(row.get(key)))
    start, stop = _int(row.get('start')), _int(row.get('stop'))
    if start and stop > start:
        return (stop - start) / 1000.0
    return 0.0


def row_steps(row: dict) -> float:
    for key in ('steps', 'total_steps', 'totalSteps'):
        if row.get(key) is not None:
            return max(0.0, _num(row.get(key)))
    steps = row.get('steps_num') or row.get('stepsNum')
    if isinstance(steps, (list, tuple)):
        return sum(max(0.0, _num(s)) for s in steps)
    return 0.0


def row_state(row: dict) -> str:
    """Normalise receipt and record states onto accepted/failed/open."""
    state = str(row.get('state') or '').strip().lower()
    if state in _ACCEPTED:
        return 'accepted'
    if state in _FAILED:
        return 'failed'
    if state in _OPEN:
        return state
    if row.get('_source') == 'record':
        # A record already exists on the server: the submission succeeded.
        if row.get('complete'):
            return 'accepted'
        return 'failed'
    return ''


def pace_min_km(distance_m: float, duration_s: float):
    if distance_m <= 0 or duration_s <= 0:
        return None
    return (duration_s / 60.0) / (distance_m / 1000.0)


def failure_text(row: dict) -> str:
    for key in ('reason', 'error', 'note', 'statusInfo', 'status'):
        text = str(row.get(key) or '').strip()
        if text:
            return text[:80]
    return '未说明原因'


def _week_start(day: _dt.date) -> _dt.date:
    return day - _dt.timedelta(days=day.weekday())


def _beijing_midnight_ms(day: _dt.date) -> int:
    return int(_dt.datetime(day.year, day.month, day.day,
                            tzinfo=safety.BEIJING).timestamp() * 1000)


def _merge(rows) -> list[dict]:
    """De-duplicate by uuid, newest timestamp first.  Records beat receipts."""
    best = {}
    order = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        stamp = row_time(row)
        if stamp <= 0:
            continue
        key = str(row.get('uuid') or '%d|%s' % (stamp, row.get('rrid') or ''))
        current = best.get(key)
        if current is None:
            best[key] = row
            order.append(key)
        elif (row.get('_source') == 'record'
              or stamp > row_time(current)):
            best[key] = row
    return [best[k] for k in order]


# --------------------------------------------------------------------------
# aggregation
# --------------------------------------------------------------------------

def summarise(rows, weeks: int = DEFAULT_WEEKS, now_ms: int | None = None,
              source: str = 'local', record_error: str = '') -> dict:
    """Weekly / monthly totals, averages and the failure top list.

    Rows older than the window are ignored.  ``now_ms`` is the reference
    "today"; rows stamped after it are ignored too so a skewed clock cannot
    paint a future week.
    """
    try:
        weeks = int(weeks)
    except (TypeError, ValueError):
        weeks = DEFAULT_WEEKS
    weeks = max(1, min(MAX_WEEKS, weeks))
    if now_ms is None:
        now_ms = safety.now_ms()
    today = safety.to_beijing(now_ms).date()
    first_week = _week_start(today) - _dt.timedelta(weeks=weeks - 1)
    first_ms = _beijing_midnight_ms(first_week)
    last_ms = now_ms + 5 * 60 * 1000       # tolerate small clock skew

    picked = [r for r in _merge(rows)
              if first_ms <= row_time(r) <= last_ms]

    buckets: dict[_dt.date, dict] = {}
    attempts = accepted = failed = open_n = 0
    failures: dict[str, int] = {}
    months: dict[tuple[int, int], dict] = {}
    total_m, total_s, total_steps = 0.0, 0.0, 0.0
    cadences: list[float] = []
    weekday_m = [0.0] * WEEKDAYS

    for row in picked:
        stamp = row_time(row)
        day = safety.to_beijing(stamp).date()
        wk = _week_start(day)
        bucket = buckets.setdefault(wk, {'m': 0.0, 'runs': 0, 'days': set()})
        state = row_state(row)
        if not state:
            continue                        # e.g. a receipt being written
        attempts += 1
        if state == 'accepted':
            accepted += 1
            dist = row_distance_m(row)
            dur = row_duration_s(row)
            bucket['m'] += dist
            bucket['runs'] += 1
            bucket['days'].add(day)
            total_m += dist
            total_s += dur if dur > 0 else 0.0
            total_steps += row_steps(row)
            key = (day.year, day.month)
            month = months.setdefault(key, {'m': 0.0, 'runs': 0})
            month['m'] += dist
            month['runs'] += 1
            weekday_m[day.weekday()] += dist
            cad = _num(row.get('avg_step_freq') or row.get('cadence'))
            if cad > 0:
                cadences.append(cad)
        elif state == 'failed':
            failed += 1
            text = failure_text(row)
            failures[text] = failures.get(text, 0) + 1
        else:
            open_n += 1

    week_rows = []
    for wk in sorted(buckets):
        b = buckets[wk]
        week_rows.append({
            'week_start': wk.strftime('%Y-%m-%d'),
            'week_end': (wk + _dt.timedelta(days=6)).strftime('%Y-%m-%d'),
            'distance_km': round(b['m'] / 1000.0, 2),
            'runs': b['runs'],
            'active_days': len(b['days']),
        })

    total_km = total_m / 1000.0
    # Total time / total distance, not the mean of split paces: the latter
    # over-weights short runs.
    avg_pace = pace_min_km(total_m, total_s)
    avg_cadence = (sum(cadences) / len(cadences)) if cadences else None
    if avg_cadence is None and total_steps > 0 and total_s > 0:
        avg_cadence = total_steps / (total_s / 60.0)

    month_label = max(months) if months else (today.year, today.month)
    month = months.get(month_label, {'m': 0.0, 'runs': 0})

    return {
        'source': source,
        'record_error': str(record_error or '')[:200],
        'weeks': weeks,
        'generated_at': safety.fmt_beijing(now_ms),
        'window': {'start': first_week.strftime('%Y-%m-%d'),
                   'end': today.strftime('%Y-%m-%d')},
        'totals': {
            'distance_km': round(total_km, 2),
            'runs': accepted,
            'avg_pace_min_per_km': round(avg_pace, 2) if avg_pace else None,
            'avg_cadence': round(avg_cadence, 1) if avg_cadence else None,
            'by_week': week_rows,
        },
        'month': {
            'label': '%04d-%02d' % month_label,
            'distance_km': round(month['m'] / 1000.0, 2),
            'runs': month['runs'],
        },
        'weekday_km': [round(v / 1000.0, 2) for v in weekday_m],
        'submission': {
            'attempts': attempts,
            'accepted': accepted,
            'rejected': failed,
            'open': open_n,
            'success_rate': (round(accepted / float(attempts) * 100.0, 1)
                             if attempts else None),
            'top_failures': [
                {'reason': reason, 'count': count}
                for reason, count in sorted(failures.items(),
                                            key=lambda kv: (-kv[1], kv[0])
                                            )[:TOP_FAILURES]
            ],
        },
    }


def load_for_account(store_dir: str, uid, unid='', weeks: int = DEFAULT_WEEKS,
                     records=None, record_error: str = '',
                     now_ms: int | None = None) -> dict:
    """Receipts for one account, optionally merged with server records."""
    from . import guard
    rows: list[dict] = []
    error = ''
    if uid:
        try:
            store = guard.ReceiptStore(store_dir)
            rows = [dict(r, _source='receipt') for r in store.list(
                str(uid), str(unid or '')) if isinstance(r, dict)]
        except Exception as e:  # noqa: BLE001 - stats must not crash the UI
            error = '回执读取失败: %s' % e
    merged = list(rows)
    source = 'local'
    if records:
        merged = [dict(r, _source='record') for r in records
                  if isinstance(r, dict)] + merged
        source = 'server+local'
    out = summarise(merged, weeks=weeks, now_ms=now_ms, source=source,
                    record_error=record_error or error)
    out['records_used'] = len(records or [])
    out['receipts_used'] = len(rows)
    return out


def self_test() -> list[tuple[str, bool, str]]:
    now = safety.now_ms()
    rows = [
        {'uuid': 'a', 'start': now - 3600_000, 'stop': now,
         'distance_m': 2500, 'state': 'accepted', 'steps': 2250},
        {'uuid': 'b', 'start': now - 48 * 3600_000, 'state': 'rejected',
         'reason': '11016', 'distance_m': 2500},
        {'uuid': 'a', 'start': now - 3600_000, 'state': 'accepted',
         'distance_m': 2500, '_source': 'record', 'complete': True},
    ]
    r = summarise(rows, weeks=4, now_ms=now)
    ok = (r['totals']['runs'] == 1 and r['submission']['attempts'] == 2
          and r['submission']['top_failures']
          and r['submission']['top_failures'][0]['reason'] == '11016')
    with tempfile.TemporaryDirectory() as tmp:
        empty = load_for_account(tmp, 'nobody', now_ms=now)
    return [('summary basics', ok, str(r['submission'])),
            ('empty account', empty['totals']['runs'] == 0
             and empty['source'] == 'local', empty['source'])]

"""Local submission gate: daily cap and night window.

Ported from upstream PR #80 (`src/safety.rs`), which is fail-closed by default.

The server-side review is out of our hands, but the two easiest patterns to
spot are entirely local:

* several runs in one day,
* a run at three in the morning.

This module refuses to submit those unless the operator explicitly overrides
it.  It is a *plausibility* gate, not a risk score — it cannot make a record
safe, only prevent the obvious machine patterns.

State lives in ``<JHP_DATA_DIR>/limits/<uid>.json`` and rolls over at Beijing
midnight.
"""

from __future__ import annotations

import datetime as _dt
import json
import os

from ..domain import safety

DEFAULT_MAX_PER_DAY = 2
DEFAULT_NIGHT_START = 23
DEFAULT_NIGHT_END = 6


def _dir() -> str:
    base = os.environ.get('JHP_DATA_DIR')
    if not base:
        base = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
    return os.path.join(base, 'limits')


def _path(uid) -> str:
    return os.path.join(_dir(), '%s.json' % uid)


def today(ms: int | None = None) -> str:
    return safety.to_beijing(ms if ms is not None else safety.now_ms()
                             ).strftime('%Y-%m-%d')


def _read(uid) -> dict:
    try:
        with open(_path(uid), encoding='utf-8') as f:
            state = json.load(f)
    except Exception:  # noqa: BLE001
        state = {}
    return state if isinstance(state, dict) else {}


def _dates(state: dict) -> dict:
    """Per-date counters, migrating the old single-day ``{date, runs}`` file.

    Backfill means one lookup can be for yesterday while another is for today.
    The old shape had one slot and reset on every date change, so reading
    yesterday would wipe today's count; the map keeps both.
    """
    dates = state.get('dates')
    if not isinstance(dates, dict):
        dates = {}
        old_day = state.get('date')
        if isinstance(old_day, str) and old_day:
            dates[old_day] = {
                'runs': int(state.get('runs', 0) or 0),
                'last_start': int(state.get('last_start', 0) or 0),
            }
    return dates


def _entry(dates: dict, day: str) -> dict:
    entry = dates.get(day)
    if not isinstance(entry, dict):
        entry = {'runs': 0, 'last_start': 0}
        dates[day] = entry
    entry['runs'] = int(entry.get('runs', 0) or 0)
    entry['last_start'] = int(entry.get('last_start', 0) or 0)
    return entry


def load(uid, now_ms: int | None = None) -> dict:
    """Counters for the Beijing date of ``now_ms`` (today by default).

    Reading a past date never resets or overwrites another date's count.
    """
    day = today(now_ms)
    entry = _entry(_dates(_read(uid)), day)
    return {'date': day, 'runs': entry['runs'],
            'last_start': entry['last_start']}


def save(uid, state: dict) -> None:
    try:
        os.makedirs(_dir(), exist_ok=True)
        tmp = _path(uid) + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, _path(uid))
    except Exception:  # noqa: BLE001
        pass          # a missing counter must not block submission


def _in_night(hour: int, start: int, end: int) -> bool:
    start %= 24
    end %= 24
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def check(uid, start_ms: int, max_per_day: int = DEFAULT_MAX_PER_DAY,
          block_night: bool = True,
          night_start: int = DEFAULT_NIGHT_START,
          night_end: int = DEFAULT_NIGHT_END) -> str | None:
    """Return a refusal reason, or None when the run may proceed."""
    if max_per_day <= 0 and not block_night:
        return None

    when = safety.to_beijing(start_ms)
    if block_night and _in_night(when.hour, night_start, night_end):
        return ('本地风控闸门：%.0f:00–%d:00 之间不提交（当前 %s）。'
                '确实需要请加 --allow-night。'
                % (night_start, night_end, when.strftime('%H:%M')))

    if max_per_day > 0:
        state = load(uid, start_ms)
        if int(state.get('runs', 0)) >= max_per_day:
            label = '今天' if state.get('date') == today() else '所选日期'
            return ('本地风控闸门：%s（%s）已提交 %d 次，上限 %d。'
                    '确实需要请加 --max-per-day。'
                    % (label, state.get('date'), state['runs'], max_per_day))
    return None


def record(uid, start_ms: int) -> dict:
    """Count a submission on the run's date.  Call only after server accept."""
    state = _read(uid)
    dates = _dates(state)
    day = today(start_ms)
    entry = _entry(dates, day)
    entry['runs'] += 1
    entry['last_start'] = int(start_ms)

    # Keep the file bounded; 40 days is far more than the 3-day backfill needs.
    cutoff = (safety.to_beijing(safety.now_ms()).date()
              - _dt.timedelta(days=40)).strftime('%Y-%m-%d')
    for key in [k for k in dates if k < cutoff]:
        del dates[key]

    state['dates'] = dates
    # Keep the legacy top-level fields in sync with *today*, so an older
    # build reading the file does not misread a backdated run as today's.
    state['date'] = today()
    today_entry = _entry(dates, state['date'])
    state['runs'] = today_entry['runs']
    state['last_start'] = today_entry['last_start']
    save(uid, state)
    return {'date': day, 'runs': entry['runs'],
            'last_start': entry['last_start']}


# --------------------------------------------------------------------------
# self test
# --------------------------------------------------------------------------

def self_test() -> list[tuple[str, bool, str]]:
    import tempfile
    out = []
    old = os.environ.get('JHP_DATA_DIR')
    tmp = tempfile.mkdtemp(prefix='jhp_limits_')
    os.environ['JHP_DATA_DIR'] = tmp
    try:
        noon = safety.to_beijing(safety.now_ms()).replace(
            hour=12, minute=0, second=0, microsecond=0)
        noon_ms = int(noon.timestamp() * 1000)
        night = noon.replace(hour=2)
        night_ms = int(night.timestamp() * 1000)

        out.append(('clean day allows', check('u1', noon_ms) is None, 'None'))
        out.append(('night blocked',
                    (check('u1', night_ms) or '').find('不提交') >= 0,
                    check('u1', night_ms)))
        out.append(('night override',
                    check('u1', night_ms, block_night=False) is None, 'None'))

        record('u1', noon_ms)
        out.append(('one run counted', load('u1')['runs'] == 1, load('u1')))
        record('u1', noon_ms)
        out.append(('cap reached',
                    (check('u1', noon_ms, max_per_day=2) or '').find('上限') >= 0,
                    check('u1', noon_ms, max_per_day=2)))
        out.append(('cap override',
                    check('u1', noon_ms, max_per_day=3) is None, 'None'))
        out.append(('cap disabled',
                    check('u1', noon_ms, max_per_day=0,
                          block_night=False) is None, 'None'))

        # per-account isolation
        out.append(('other account unaffected',
                    check('u2', noon_ms) is None, 'None'))

        # day rollover
        tomorrow = noon.replace(day=noon.day) + _dt.timedelta(days=1)
        out.append(('rolls over at Beijing midnight',
                    check('u1', int(tomorrow.timestamp() * 1000)) is None,
                    'None'))

        # backfill isolation: yesterday's count must not reset today's
        yesterday = noon - _dt.timedelta(days=1)
        y_ms = int(yesterday.timestamp() * 1000)
        record('u3', y_ms)
        out.append(('backdated run counts on its own day',
                    load('u3', y_ms)['runs'] == 1
                    and load('u3', noon_ms)['runs'] == 0,
                    (load('u3', y_ms)['runs'], load('u3', noon_ms)['runs'])))
        out.append(('backdated day has its own cap',
                    (check('u3', y_ms, max_per_day=1) or '').find('上限') >= 0,
                    check('u3', y_ms, max_per_day=1)))
        out.append(('today still has room after backfill',
                    check('u3', noon_ms, max_per_day=1) is None,
                    'None'))

        # old single-day file is migrated, not discarded
        legacy = {'date': today(noon_ms), 'runs': 1, 'last_start': noon_ms}
        os.makedirs(_dir(), exist_ok=True)
        with open(_path('u4'), 'w', encoding='utf-8') as f:
            json.dump(legacy, f)
        out.append(('old counter file migrates',
                    load('u4', noon_ms)['runs'] == 1, load('u4', noon_ms)))
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

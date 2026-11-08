"""Safety helpers: log redaction and Beijing-time formatting.

Ported behaviour from YanamiNeko/NekoSportsWorldTool PR #69 (`redact_json`) and
PR #82 (all protocol timestamps interpreted in Asia/Shanghai).

Two rules that matter operationally:
  * never print a token / password / cookie, even inside a nested response;
  * never let the machine's local timezone decide what "today" is — the server
    works in Beijing time and a laptop on UTC-8 will silently produce a run that
    looks like it happened tomorrow.
"""

from __future__ import annotations

import datetime as _dt
import random
import re

from . import clock as _clock

#: matched case-insensitively as substrings of the key name
SENSITIVE_KEYS = (
    'token', 'password', 'passwd', 'psw', 'authorization', 'cookie',
    'secret', 'session', 'credential', 'sign', 'private', 'idfa',
    'deviceid', 'imei', 'mac_address', 'androidid', 'customdeviceid',
)

MASK = '***'

#: Beijing is UTC+8 year-round (no DST since 1991)
BEIJING = _dt.timezone(_dt.timedelta(hours=8), 'Asia/Shanghai')


def _is_sensitive(key: str) -> bool:
    k = str(key).lower()
    return any(s in k for s in SENSITIVE_KEYS)


def redact_json(obj, _depth: int = 0):
    """Recursively mask sensitive STRING values; numbers/bools stay intact.

    Structure is preserved so logs remain debuggable. Depth-capped to avoid
    pathological payloads.
    """
    if _depth > 12:
        return obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _is_sensitive(k) and isinstance(v, str) and v:
                out[k] = MASK
            else:
                out[k] = redact_json(v, _depth + 1)
        return out
    if isinstance(obj, list):
        return [redact_json(v, _depth + 1) for v in obj]
    return obj


def redact_text(text: str) -> str:
    """Best-effort scrub for free-form lines (URLs, headers, tracebacks)."""
    if not text:
        return text
    # Basic credentials first: the generic rule below would eat the "Basic"
    # marker and leave the base64 payload exposed.
    text = re.sub(r'(?i)\bBasic\s+[A-Za-z0-9+/=]{8,}', 'Basic ***', text)
    text = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._\-+/=]{8,}', 'Bearer ***', text)
    text = re.sub(r'(?i)(token|password|passwd|psw|authorization|cookie)'
                  r'([=:]\s*)([^\s&,;"\']+)', r'\1\2***', text)
    return text


def mask(value: str, keep: int = 4) -> str:
    """Show only the head of a secret, for one-line status output."""
    if not value:
        return ''
    if len(value) <= keep * 2:
        return MASK
    return value[:keep] + '...' + value[-2:]


# --------------------------------------------------------------------------
# Beijing time
# --------------------------------------------------------------------------

def now_ms() -> int:
    """Server-aligned wall clock.

    See :mod:`replica.domain.clock`.  The system clock is never changed; when
    ``gmttime`` has been read, this returns the server's timeline instead.
    """
    return _clock.now_ms()


def local_now_ms() -> int:
    """The operating system's clock, for diagnostics only."""
    return _clock.local_now_ms()


def to_beijing(ms: int) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(ms / 1000.0, BEIJING)


def fmt_beijing(ms: int, fmt: str = '%Y-%m-%d %H:%M:%S') -> str:
    return to_beijing(ms).strftime(fmt)


def beijing_iso(ms: int) -> str:
    return to_beijing(ms).isoformat(timespec='seconds')


def start_of_today_beijing_ms(now: int | None = None) -> int:
    now = now if now is not None else now_ms()
    d = to_beijing(now)
    midnight = d.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp() * 1000)


def within_valid_window(ms: int, start: str = '07:00:00',
                        end: str = '20:00:00') -> bool:
    """True when `ms` falls inside the reference client's random-time window.

    The bounds are 07:00-20:00 because that is what the reference samples
    (`src/ui/run.rs`: RAND_HOUR_LO=7, RAND_HOUR_HI=20).  What the *server*
    enforces is not documented — upstream issue #64 is still marked "待补证据,
    时间限制根因未确定" — so this is a plausibility check, not a guarantee.
    """
    t = to_beijing(ms).strftime('%H:%M:%S')
    return start <= t <= end


# --------------------------------------------------------------------------
# backfill start time (today .. 3 days ago, 09:00-22:00 Beijing)
# --------------------------------------------------------------------------

#: The UI/CLI lets the operator pick a start time for a run that already
#: "happened".  Upstream clamps the day offset to 0..3 (`src/ui/run.rs`,
#: `src/cli/cmds.rs`) and we use the same calendar-day semantics.
BACKFILL_DAYS = 3
SELECT_START = '09:00'
SELECT_END = '22:00'

#: The server's validTime list is a *list* of windows.  A school can split the
#: day into several disjoint bands (06:00-07:59, 09:01-11:59, ...), so a single
#: start/end pair is not enough.  Keep the cap small: a malformed policy reply
#: must not create an unbounded list.
MAX_WINDOWS = 12


def _clock_minutes(text: str) -> int:
    """'HH:MM' -> minutes since midnight.  Raises ValueError on bad input."""
    raw = str(text or '').strip()
    parts = raw.split(':')
    if len(parts) != 2:
        raise ValueError('开始时间要写成 HH:MM（例如 10:30）')
    try:
        hh, mm = int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        raise ValueError('开始时间要写成 HH:MM（例如 10:30）')
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError('开始时间要写成 HH:MM（例如 10:30）')
    return hh * 60 + mm


def _window_minutes(text) -> int | None:
    """Parse a window clock, including ``24:00`` as an end-of-day marker."""
    raw = str(text or '').strip().replace('：', ':')
    if not raw:
        return None
    parts = raw.split(':')
    if len(parts) not in (2, 3):
        return None
    try:
        hh = int(parts[0])
        mm = int(parts[1])
        ss = int(parts[2]) if len(parts) == 3 else 0
    except (TypeError, ValueError):
        return None
    if hh == 24 and mm == 0 and ss == 0:
        return 24 * 60
    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
        return None
    return hh * 60 + mm


def _fmt_window_minutes(minutes: int) -> str:
    if minutes >= 24 * 60:
        return '24:00'
    return '%02d:%02d' % (minutes // 60, minutes % 60)


def _window_pairs(raw, errors: list | None = None) -> list[tuple[int, int]]:
    """Turn every accepted server/UI shape into ``(start, end)`` minutes.

    The parser is deliberately flat: server replies have used dicts, 2-item
    lists, ``"06:00-07:59"`` strings and JSON-encoded strings.  A recursive
    tuple handler here used to loop forever on a plain tuple.

    ``errors`` is optional.  The settings save path passes a list so malformed
    rows are rejected instead of being silently deleted; policy parsing leaves
    it ``None`` and simply ignores bad server entries.
    """
    import json as _json

    out: list[tuple[int, int]] = []

    def record(message):
        if errors is not None:
            errors.append(message)

    def add(a, b):
        start_min = _window_minutes(a)
        end_min = _window_minutes(b)
        if start_min is None or end_min is None:
            has_a = bool(str(a or '').strip())
            has_b = bool(str(b or '').strip())
            if has_a and not has_b:
                record('时段缺少结束时间：%s' % a)
            elif has_b and not has_a:
                record('时段缺少开始时间：%s' % b)
            elif has_a or has_b:
                record('时段格式不对：%s–%s（要写成 HH:MM）' % (a, b))
            return
        if start_min >= 24 * 60 or end_min <= start_min:
            record('时段结束要晚于开始：%s–%s' % (_fmt_window_minutes(start_min),
                                                _fmt_window_minutes(end_min)))
            return
        out.append((start_min, end_min))

    def walk(value):
        if value is None or len(out) >= MAX_WINDOWS * 2:
            return
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return
            try:
                parsed = _json.loads(text)
            except Exception:  # noqa: BLE001
                parsed = None
            if isinstance(parsed, (list, dict)):
                walk(parsed)
                return
            text = text.replace('–', '-').replace('—', '-').replace('－', '-')
            text = text.replace('；', ',').replace(';', ',').replace('、', ',')
            text = text.replace('：', ':')
            text = text.replace('\n', ',')
            if ':' not in text and '-' not in text:
                record('时段格式不对：%s' % text)
                return
            for piece in text.split(','):
                if '-' not in piece:
                    if ':' in piece:
                        record('时段缺少结束时间：%s' % piece.strip())
                    continue
                a, b = piece.split('-', 1)
                add(a, b)
            return
        if isinstance(value, dict):
            if 'windows' in value:
                walk(value.get('windows'))
                return
            add(value.get('start', value.get('startTime')),
                value.get('end', value.get('endTime')))
            return
        if isinstance(value, (list, tuple)):
            # A flat list of clocks is paired in order: ["06:00", "07:59"] or
            # ["06:00", "07:59", "09:01", "11:59"].  Range strings such as
            # "06:00-07:59" are not clocks and fall through to walk().
            if value and all(
                    isinstance(v, str)
                    and re.search(r'\d{1,2}[:：]\d{2}', v)
                    and not re.search(r'[-–—－]', v)
                    for v in value):
                for i in range(0, len(value) - 1, 2):
                    add(value[i], value[i + 1])
                if len(value) % 2:
                    record('时段缺少结束时间：%s' % value[-1])
                return
            for item in value:
                if isinstance(item, (list, tuple)):
                    if len(item) >= 2:
                        add(item[0], item[1])
                    else:
                        walk(item)
                elif isinstance(item, dict):
                    add(item.get('start', item.get('startTime')),
                        item.get('end', item.get('endTime')))
                elif isinstance(item, str):
                    walk(item)
                elif item is not None and str(item).strip():
                    record('时段格式不对：%r' % (item,))
                if len(out) >= MAX_WINDOWS * 2:
                    break
            return
        if errors is not None and value is not None:
            record('时段格式不对：%r' % (value,))

    walk(raw)
    return out


def normalize_windows(raw, errors: list | None = None) -> list[dict]:
    """Return a stable, de-duplicated ``[{start, end}]`` list.

    Same-day windows are the normal case.  A window whose end is earlier than
    its start is treated as malformed and ignored; the reference client's
    validTime bands are same-day, and a malformed reply must not create a
    confusing overnight slot.  Pass ``errors`` to collect malformed rows for
    the settings save path.
    """
    pairs = list(_window_pairs(raw, errors))
    if errors is not None and len(pairs) > MAX_WINDOWS:
        errors.append('最多支持 %d 个有效时段' % MAX_WINDOWS)
    seen = set()
    out = []
    for a, b in sorted(pairs):
        key = (a, b)
        if key in seen:
            continue
        seen.add(key)
        out.append({'start': _fmt_window_minutes(a),
                    'end': _fmt_window_minutes(b)})
        if len(out) >= MAX_WINDOWS:
            break
    return out


def in_windows(hhmm: str, windows) -> bool:
    """True when ``hhmm`` is inside any configured window (inclusive end)."""
    try:
        minute = _clock_minutes(hhmm)
    except ValueError:
        return False
    for w in normalize_windows(windows):
        a = _window_minutes(w['start'])
        b = _window_minutes(w['end'])
        if a is None or b is None:
            continue
        if a <= minute <= b:
            return True
    return False


def run_fits_window(hhmm: str, duration_minutes: float, windows) -> bool:
    """True only when the whole run fits inside one same-day band.

    The submit path (``resolve_start_ms``) uses this rule, so the qualification
    model must use it too; otherwise the UI says "合格" and the submit fails a
    second later with "结束会超过有效时段".
    """
    try:
        minute = _clock_minutes(hhmm)
    except ValueError:
        return False
    try:
        duration = max(0.0, float(duration_minutes or 0))
    except (TypeError, ValueError):
        duration = 0.0
    for w in normalize_windows(windows):
        a = _window_minutes(w['start'])
        b = _window_minutes(w['end'])
        if a is None or b is None:
            continue
        if a <= minute and minute + duration <= b:
            return True
    return False


def format_windows(windows, sep: str = '、') -> str:
    return sep.join('%s–%s' % (w['start'], w['end'])
                    for w in normalize_windows(windows))


def windows_envelope(windows) -> tuple[str, str]:
    """Outer bounds for the legacy single-window fields.

    Old builds only understand one pair.  Writing the envelope (instead of the
    first band) means a downgrade still accepts every time the school allows,
    instead of falsely rejecting the later bands.
    """
    items = normalize_windows(windows)
    if not items:
        return '', ''
    starts = [_window_minutes(w['start']) for w in items]
    ends = [_window_minutes(w['end']) for w in items]
    if any(v is None for v in starts + ends):
        return '', ''
    return (_fmt_window_minutes(min(starts)),
            _fmt_window_minutes(max(ends)).replace('24:00', '23:59'))


def resolve_backfill_date(date: str | None = None, days_ago: int = 0,
                          now: int | None = None) -> _dt.date:
    """Validate a backfill date (Beijing calendar day).

    ``date`` is ``YYYY-MM-DD`` from the UI; ``days_ago`` is the CLI shorthand
    (0 = today, 3 = three days ago).  Only today .. 3 days ago is accepted,
    matching upstream's ``clamp(0, 3)``.
    """
    now_val = now if now is not None else now_ms()
    today = to_beijing(now_val).date()
    if date is not None and str(date).strip():
        raw = str(date).strip()
        try:
            day = _dt.datetime.strptime(raw, '%Y-%m-%d').date()
        except ValueError:
            raise ValueError('日期要写成 YYYY-MM-DD（例如 2026-10-22）')
        n = (today - day).days
    else:
        try:
            n = int(days_ago)
        except (TypeError, ValueError):
            raise ValueError('天数必须是 0–%d 的整数' % BACKFILL_DAYS)
        day = today - _dt.timedelta(days=n)

    if n < 0:
        raise ValueError('不能选未来日期')
    if n > BACKFILL_DAYS:
        raise ValueError('只能选今天到 %d 天前' % BACKFILL_DAYS)
    return day


def resolve_start_ms(days_ago: int = 0, hhmm: str | None = None,
                     duration_s: float = 0,
                     now: int | None = None,
                     date: str | None = None,
                     windows=None) -> int:
    """Resolve a backfill start timestamp in Beijing time.

    ``date`` (``YYYY-MM-DD``) is what the UI sends; ``days_ago`` is the CLI
    shorthand.  ``hhmm`` is ``HH:MM``; ``None``/empty picks a random start.
    ``windows`` is the school's ``validTime`` list.  When omitted the historical
    09:00-22:00 default is used.

    Every band is a *start-and-finish* band: a run is placed only when the
    whole run fits in one band.  That is conservative, but a school that splits
    the day into four bands did so for a reason; starting at 07:50 for a 30 min
    run would finish outside the 06:00-07:59 band.  For today the run must also
    end by ``now`` -- a start in the future is refused rather than silently
    clamped.

    The returned timestamp keeps sub-minute precision.  A whole-minute start
    (``:00.000``) is what the old implementation produced, and it bled into
    every downstream timestamp: each point ``flag``, each 10 s window boundary
    and every ``gainTime`` then sat exactly on a minute/second grid.  A real
    start comes from ``System.currentTimeMillis()`` and has an arbitrary second
    and millisecond.
    """
    now_val = now if now is not None else now_ms()
    now_bj = to_beijing(now_val)
    today = now_bj.date()
    day = resolve_backfill_date(date, days_ago, now_val)
    n = (today - day).days
    duration_ms = max(0, int(round(float(duration_s or 0) * 1000)))
    duration = _dt.timedelta(milliseconds=duration_ms)

    school_windows = normalize_windows(windows)
    using_school = bool(school_windows)
    if not school_windows:
        school_windows = [{'start': SELECT_START, 'end': SELECT_END}]
    label = format_windows(school_windows) or '09:00–22:00'

    # Validate an explicit clock before building slots.  Otherwise a time that
    # is simply not in any band (or has not arrived yet today) is misreported
    # as "today has no room left".
    explicit_raw = None
    explicit_start = None
    if hhmm is not None and str(hhmm).strip():
        explicit_raw = str(hhmm).strip()
        explicit_minutes = _clock_minutes(explicit_raw)
        explicit_start = _dt.datetime.combine(
            day, _dt.time(explicit_minutes // 60, explicit_minutes % 60),
            tzinfo=BEIJING)
        if using_school and not in_windows(explicit_raw, school_windows):
            raise ValueError('开始时间必须在学校的有效时段内（当前 %s；'
                             '可用：%s）' % (explicit_raw, label))
        if not using_school and (explicit_minutes < 9 * 60
                                 or explicit_minutes > 22 * 60):
            raise ValueError('开始时间必须在 09:00–22:00 之间（当前 %s）'
                             % explicit_raw)

    # Build the actually usable slots for this date.  A slot ends at the band
    # end, or at "now" for today, minus the run duration.
    slots = []
    for w in school_windows:
        a = _window_minutes(w['start'])
        b = _window_minutes(w['end'])
        if a is None or b is None or b <= a:
            continue
        lo = _dt.datetime.combine(
            day, _dt.time(a // 60, a % 60), tzinfo=BEIJING)
        if b >= 24 * 60:
            hi = _dt.datetime.combine(
                day + _dt.timedelta(days=1), _dt.time(0, 0),
                tzinfo=BEIJING)
        else:
            hi = _dt.datetime.combine(
                day, _dt.time(b // 60, b % 60), tzinfo=BEIJING)
        latest = min(hi, now_bj) if n == 0 else hi
        latest_start = latest - duration
        if latest_start >= lo:
            slots.append((lo, latest_start))

    if not slots:
        if n == 0 and explicit_start is not None and explicit_start > now_bj:
            raise ValueError('今天 %s 还没到（现在 %s）'
                             % (explicit_raw, now_bj.strftime('%H:%M')))
        if n == 0 and explicit_start is not None and (
                explicit_start + duration > now_bj):
            raise ValueError('这个时间结束时已经超过现在（现在 %s），'
                             '请提前开始' % now_bj.strftime('%H:%M'))
        if n == 0:
            now_min = now_bj.hour * 60 + now_bj.minute
            starts = [_window_minutes(w['start']) for w in school_windows]
            starts = [v for v in starts if v is not None]
            if starts and min(starts) > now_min:
                raise ValueError(
                    '今天有效时段还没开始（最早 %s，现在 %s）；'
                    '请到时间后再试，或改选昨天'
                    % (_fmt_window_minutes(min(starts)),
                       now_bj.strftime('%H:%M')))
            raise ValueError(
                '今天已经放不下这次跑步（约 %d 分钟）；有效时段：%s。'
                '请改选昨天或更早'
                % (max(1, int(round(duration_ms / 60000.0))), label))
        raise ValueError('这次跑步太长，放不进有效时段：%s' % label)

    if explicit_start is not None:
        chosen = None
        for lo, latest_start in slots:
            if lo <= explicit_start <= latest_start:
                chosen = latest_start
                break
        if chosen is None:
            if n == 0 and explicit_start > now_bj:
                raise ValueError('今天 %s 还没到（现在 %s）'
                                 % (explicit_raw, now_bj.strftime('%H:%M')))
            if n == 0 and explicit_start + duration > now_bj:
                raise ValueError('这个时间结束时已经超过现在（现在 %s），'
                                 '请提前开始' % now_bj.strftime('%H:%M'))
            if using_school:
                raise ValueError('这个时间结束会超过学校的有效时段（%s），'
                                 '请提前开始或换一个时段' % label)
            raise ValueError('这个时间结束会超过 22:00，请提前开始')
        start_ms = int(explicit_start.timestamp() * 1000)
        return _jittered_start(start_ms, chosen, duration_ms)

    # Pick a slot weighted by its usable seconds so long bands are not
    # under-sampled.  The +1 keeps a zero-width slot selectable when the run
    # ends exactly at "now".
    spans = []
    total = 0
    for lo, latest_start in slots:
        span = max(1, int((latest_start - lo).total_seconds()))
        spans.append((lo, span))
        total += span
    pick = random.randint(0, total - 1)
    chosen_lo = spans[-1][0]
    chosen_latest = slots[-1][1]
    for (lo, span), (_, latest_start) in zip(spans, slots):
        if pick < span:
            chosen_lo, chosen_latest = lo, latest_start
            break
        pick -= span
    picked = chosen_lo + _dt.timedelta(seconds=random.randint(
        0, max(0, int((chosen_latest - chosen_lo).total_seconds()))))
    return _jittered_start(int(picked.timestamp() * 1000),
                           chosen_latest, duration_ms)


def _jittered_start(start_ms: int, latest_start, duration_ms: int) -> int:
    """Add a sub-minute offset without moving the run past its deadline.

    The operator picked a minute (or we picked a second); the seconds and
    milliseconds inside that minute are unobservable to them and must not be
    zero, or every timestamp derived from the start shares one grid.
    """
    latest_ms = int(latest_start.timestamp() * 1000)
    room = latest_ms - (start_ms + duration_ms)
    if room <= 0:
        return start_ms
    offset = random.randint(0, 59) * 1000 + random.randint(0, 999)
    return start_ms + min(offset, room)


# --------------------------------------------------------------------------
# self test
# --------------------------------------------------------------------------

def self_test() -> list[tuple[str, bool, str]]:
    out = []

    payload = {'error': 10000,
               'data': {'uid': 99999999, 'token': 'abc123def456',
                        'name': '测试用户',
                        'nested': [{'password': 'secret-pass'}, {'ok': 1}]}}
    red = redact_json(payload)
    out.append(('token masked', red['data']['token'] == MASK, red['data']['token']))
    out.append(('password masked', red['data']['nested'][0]['password'] == MASK, MASK))
    out.append(('plain data preserved',
                red['data']['name'] == '测试用户' and red['data']['uid'] == 99999999,
                red['data']['name']))
    out.append(('numbers untouched', red['data']['nested'][1]['ok'] == 1, 'ok'))

    txt = 'Authorization: Basic dGVzdDp0ZXN0 token=abc123'
    rt = redact_text(txt)
    out.append(('free-text scrubbed',
                'abc123' not in rt and 'dGVzdDp0ZXN0' not in rt, rt[:48]))

    out.append(('mask keeps head', mask('abcdef12-3456-7890') == 'abcd...90',
                mask('abcdef12-3456-7890')))

    # Beijing is a fixed +08:00 offset
    ms = 1791815536447
    out.append(('beijing offset +8',
                to_beijing(ms).utcoffset() == _dt.timedelta(hours=8),
                fmt_beijing(ms)))

    # 06:30-22:00 window
    d = _dt.datetime(2026, 10, 12, 12, 0, tzinfo=BEIJING)
    noon = int(d.timestamp() * 1000)
    d2 = _dt.datetime(2026, 10, 12, 23, 0, tzinfo=BEIJING)
    late = int(d2.timestamp() * 1000)
    out.append(('window accepts noon', within_valid_window(noon), fmt_beijing(noon)))
    out.append(('window rejects 23:00', not within_valid_window(late), fmt_beijing(late)))

    day = _dt.datetime(2026, 10, 12, 9, 30, tzinfo=BEIJING)
    day_ms = int(day.timestamp() * 1000)
    out.append(('midnight Beijing',
                fmt_beijing(start_of_today_beijing_ms(day_ms), '%H:%M:%S') == '00:00:00',
                fmt_beijing(start_of_today_beijing_ms(day_ms))))

    # ---- backfill start resolver --------------------------------------
    now = _dt.datetime(2026, 10, 23, 15, 0, tzinfo=BEIJING)
    now_stamp = int(now.timestamp() * 1000)

    y = resolve_start_ms(1, '10:30', 1800, now_stamp)
    out.append(('backfill honours exact time',
                fmt_beijing(y, '%Y-%m-%d %H:%M') == '2026-10-22 10:30',
                fmt_beijing(y)))

    yd = resolve_start_ms(date='2026-10-22', hhmm='10:30', duration_s=1800,
                          now=now_stamp)
    # The start keeps a random second/millisecond inside the chosen minute,
    # so the two calls agree on the minute, not on the exact millisecond.
    out.append(('explicit date matches days_ago',
                fmt_beijing(yd, '%Y-%m-%d %H:%M')
                == fmt_beijing(y, '%Y-%m-%d %H:%M'), fmt_beijing(yd)))

    def _bad(fn) -> bool:
        try:
            fn()
        except ValueError:
            return True
        return False

    out.append(('backfill rejects 08:59',
                _bad(lambda: resolve_start_ms(0, '08:59', 0, now_stamp)), ''))
    out.append(('backfill rejects 22:00 + duration',
                _bad(lambda: resolve_start_ms(1, '22:00', 60, now_stamp)), ''))
    out.append(('backfill rejects 4 days ago',
                _bad(lambda: resolve_start_ms(4, '10:00', 0, now_stamp)), ''))
    out.append(('backfill rejects 4 days ago by date',
                _bad(lambda: resolve_start_ms(date='2026-10-19', hhmm='10:00',
                                              duration_s=0, now=now_stamp)), ''))
    out.append(('backfill rejects future date',
                _bad(lambda: resolve_start_ms(date='2026-10-24', hhmm='10:00',
                                              duration_s=0, now=now_stamp)), ''))
    out.append(('backfill rejects malformed date',
                _bad(lambda: resolve_start_ms(date='10/22/2026', hhmm='10:00',
                                              duration_s=0, now=now_stamp)), ''))
    out.append(('backfill rejects today future',
                _bad(lambda: resolve_start_ms(0, '16:00', 1800, now_stamp)), ''))
    out.append(('backfill rejects before 09:00 today',
                _bad(lambda: resolve_start_ms(
                    0, None, 600,
                    int(_dt.datetime(2026, 10, 23, 8, 0,
                                     tzinfo=BEIJING).timestamp() * 1000))), ''))

    picked = resolve_start_ms(0, None, 1800, now_stamp)
    p = to_beijing(picked)
    out.append(('random backfill stays in 09:00-14:30',
                p.date() == now.date()
                and (p.hour, p.minute) >= (9, 0)
                and (p.hour, p.minute) <= (14, 30),
                p.strftime('%H:%M')))
    return out


if __name__ == '__main__':
    ok = True
    for name, passed, detail in self_test():
        print('%-30s %s   %s' % (name, 'PASS' if passed else 'FAIL', detail))
        ok = ok and passed
    print('\nRESULT:', 'PASS' if ok else 'FAIL')
    raise SystemExit(0 if ok else 1)

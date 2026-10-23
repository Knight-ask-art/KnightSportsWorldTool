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
    return int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000)


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
                     date: str | None = None) -> int:
    """Resolve a backfill start timestamp in Beijing time.

    ``date`` (``YYYY-MM-DD``) is what the UI sends; ``days_ago`` is the CLI
    shorthand.  ``hhmm`` is ``HH:MM`` inside 09:00-22:00; ``None``/empty picks
    a random minute in that window.

    Both ends are enforced: the start must be >= 09:00 and the run must end by
    22:00 (the server's ``runModePolicy.validTime`` ends at 22:00).  For today
    the run must also end by ``now`` -- a start in the future is refused rather
    than silently clamped, because the operator explicitly asked for it.
    """
    now_val = now if now is not None else now_ms()
    now_bj = to_beijing(now_val)
    today = now_bj.date()
    day = resolve_backfill_date(date, days_ago, now_val)
    n = (today - day).days

    lo = _dt.datetime.combine(day, _dt.time(9, 0), tzinfo=BEIJING)
    hi = _dt.datetime.combine(day, _dt.time(22, 0), tzinfo=BEIJING)
    latest = min(hi, now_bj)                    # a run may not end in the future
    duration_ms = max(0, int(round(float(duration_s or 0) * 1000)))
    latest_start = latest - _dt.timedelta(milliseconds=duration_ms)

    if latest_start < lo:
        if n == 0:
            raise ValueError(
                '今天 09:00–22:00 已经放不下这次跑步（约 %d 分钟）；'
                '请改选昨天或更早' % max(1, int(round(duration_ms / 60000.0))))
        raise ValueError('这次跑步太长，放不进 09:00–22:00')

    if hhmm is not None and str(hhmm).strip():
        minutes = _clock_minutes(hhmm)
        if minutes < 9 * 60 or minutes > 22 * 60:
            raise ValueError('开始时间必须在 09:00–22:00 之间（当前 %s）'
                             % str(hhmm).strip())
        start = _dt.datetime.combine(
            day, _dt.time(minutes // 60, minutes % 60), tzinfo=BEIJING)
        if start > latest_start:
            if n == 0 and start > now_bj:
                raise ValueError('今天 %s 还没到（现在 %s）'
                                 % (str(hhmm).strip(),
                                    now_bj.strftime('%H:%M')))
            if n == 0:
                raise ValueError('这个时间结束时已经超过现在（现在 %s），'
                                 '请提前开始' % now_bj.strftime('%H:%M'))
            raise ValueError('这个时间结束会超过 22:00，请提前开始')
        return int(start.timestamp() * 1000)

    lo_s = int(lo.timestamp())
    hi_s = int(latest_start.timestamp())
    if hi_s < lo_s:
        raise ValueError('今天 09:00–22:00 已经放不下这次跑步')
    picked = random.randint(lo_s, hi_s)
    picked -= picked % 60                       # keep it on a whole minute
    return picked * 1000


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
    out.append(('explicit date matches days_ago',
                yd == y, fmt_beijing(yd)))

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

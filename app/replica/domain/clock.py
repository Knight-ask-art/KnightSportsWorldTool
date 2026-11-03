"""Server-aligned wall clock.

Why this exists
---------------
The real server checks the envelope ``timeStamp`` and treats a stale one as an
empty 200 (soft reject).  It also compares the record's start/stop with its own
clock, so a laptop that is a few minutes fast can produce a "future" run even
when the operator did nothing wrong.

Telling the operator to "校准系统时间" is not a fix: it asks them to change a
system setting they may not control, and it is exactly the kind of advice that
turns into a support issue.  So the application keeps its own offset, measured
from ``GET api/v3/getserver/gmttime``, and every timestamp we generate, sign or
display goes through :func:`now_ms`.  The system clock is never modified.

Anchoring
---------
While an offset is active the clock runs off ``time.monotonic()`` rather than
the wall clock.  If the user (or Windows time sync) changes the system clock
mid-session, our timestamps do not jump backwards or forwards.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time

#: A server reply outside this range is not a clock, it is a parsing accident
#: (seconds mistaken for ms, an HTML error page, ...).  Refuse to apply it.
MIN_PLAUSIBLE_MS = 1_577_836_800_000      # 2020-01-01T00:00:00Z
MAX_PLAUSIBLE_MS = 4_102_444_800_000      # 2100-01-01T00:00:00Z

#: A saved offset older than this is discarded: the machine may have been
#: restarted, its clock fixed, or moved to another timezone.  Re-measuring is
#: one cheap GET and happens before every generate/submit anyway.
SAVED_TTL_MS = 12 * 3600 * 1000

#: ``gmttime`` is the primary source.  The HTTP ``Date`` header of the same
#: reply is a coarser but independent fallback when the body is empty/blocked.
TRUSTED_SOURCES = ('gmttime', 'http-date')

_LOCK = threading.RLock()
_offset_ms = 0.0
_source = 'local'
_measured_local_ms = 0
_server_ms = 0
_rtt_ms = 0
_reason = ''
#: monotonic anchor so a system-clock change cannot move our timestamps
_anchor_mono = None
_anchor_server_ms = 0.0


def local_now_ms() -> int:
    """The operating system's wall clock (what the server would see)."""
    return int(time.time() * 1000)


def now_ms() -> int:
    """The clock we generate and sign with: server-aligned when available."""
    with _LOCK:
        if _anchor_mono is not None:
            return int(_anchor_server_ms + (time.monotonic() - _anchor_mono)
                       * 1000.0)
    return local_now_ms()


def offset_ms() -> int:
    with _LOCK:
        return round(_offset_ms)


def source() -> str:
    with _LOCK:
        return _source


def state() -> dict:
    """Snapshot for diagnostics; never raises."""
    with _LOCK:
        local = local_now_ms()
        cur = now_ms()
        return {
            'offset_ms': round(_offset_ms),
            'source': _source,
            'measured_local_ms': int(_measured_local_ms),
            'server_ms': int(cur),
            'server_time': cur,
            'local_ms': local,
            'rtt_ms': int(_rtt_ms),
            'age_ms': (local - int(_measured_local_ms)
                       if _measured_local_ms else None),
            'reason': _reason,
            'applied': _source in TRUSTED_SOURCES,
        }


def _reset_locked() -> None:
    global _offset_ms, _source, _measured_local_ms, _server_ms, _rtt_ms
    global _reason, _anchor_mono, _anchor_server_ms
    _offset_ms = 0.0
    _source = 'local'
    _measured_local_ms = 0
    _server_ms = 0
    _rtt_ms = 0
    _reason = ''
    _anchor_mono = None
    _anchor_server_ms = 0.0


def reset() -> None:
    """Forget the measured offset (tests, logout, manual reset)."""
    with _LOCK:
        _reset_locked()


def apply_server_time(server_ms, sent_local_ms, received_local_ms=None,
                      rtt_ms=None, source: str = 'gmttime') -> dict:
    """Apply a server clock reading and return the resulting state.

    The server timestamp is produced somewhere between sending the request and
    receiving the reply.  Using the midpoint of that interval removes the
    one-way network delay from the estimate, which matters at 100-300 ms RTT.
    """
    try:
        server = int(server_ms)
    except (TypeError, ValueError):
        return _reject('服务器时间不是整数: %r' % (server_ms,))
    if server < MIN_PLAUSIBLE_MS or server > MAX_PLAUSIBLE_MS:
        return _reject('服务器时间超出合理范围: %s' % server)

    received = (local_now_ms() if received_local_ms is None
                else int(received_local_ms))
    sent = int(sent_local_ms)
    rtt = (received - sent) if rtt_ms is None else int(rtt_ms)
    mid = (sent + received) // 2
    offset = float(server - mid)
    # Best estimate of server "now" at the moment we anchored.
    anchor_server = float(server) + max(0.0, rtt) / 2.0

    with _LOCK:
        global _offset_ms, _source, _measured_local_ms, _server_ms, _rtt_ms
        global _reason, _anchor_mono, _anchor_server_ms
        _offset_ms = offset
        _source = source if source in TRUSTED_SOURCES else 'gmttime'
        _measured_local_ms = received
        _server_ms = server
        _rtt_ms = rtt
        _reason = ''
        _anchor_mono = time.monotonic()
        _anchor_server_ms = anchor_server
    return {
        'applied': True,
        'source': source if source in TRUSTED_SOURCES else 'gmttime',
        'offset_ms': round(offset),
        'rtt_ms': rtt,
        'server_ms': server,
        'local_ms': received,
        'anchor_server_ms': round(anchor_server),
        'reason': '',
    }


def _reject(reason: str) -> dict:
    with _LOCK:
        global _reason
        _reason = reason
    st = state()
    # A bad reading must not throw away a previously good offset: keep using
    # it, but mark the measurement as rejected so callers can say so.
    return {
        'applied': st['applied'],
        'rejected': True,
        'source': st['source'],
        'offset_ms': st['offset_ms'],
        'reason': reason,
    }


def needs_sync(max_age_s: float = 600.0) -> bool:
    """True when the offset is unknown, stale, or came from the wall clock."""
    with _LOCK:
        if _source not in TRUSTED_SOURCES or not _measured_local_ms:
            return True
        age = local_now_ms() - _measured_local_ms
        return age > max_age_s * 1000.0 or age < -300_000


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------

def load(path: str | None) -> bool:
    """Restore a recent offset.  Returns True when one was applied."""
    if not path:
        return False
    try:
        with open(path, encoding='utf-8') as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return False
    if (not isinstance(doc, dict)
            or doc.get('source') not in TRUSTED_SOURCES):
        return False
    try:
        offset = float(doc.get('offset_ms'))
        measured = int(doc.get('measured_local_ms'))
    except (TypeError, ValueError):
        return False
    local = local_now_ms()
    age = local - measured
    # Negative age beyond a small tolerance means the wall clock was moved
    # backwards after saving; the old offset cannot be trusted.
    if age > SAVED_TTL_MS or age < -300_000:
        return False
    if _offset_plausible(offset):
        with _LOCK:
            global _offset_ms, _source, _measured_local_ms, _server_ms, _rtt_ms
            global _reason, _anchor_mono, _anchor_server_ms
            _offset_ms = offset
            _source = str(doc.get('source') or 'gmttime')
            _measured_local_ms = measured
            _server_ms = local + int(offset)
            _rtt_ms = int(doc.get('rtt_ms') or 0)
            _reason = ''
            _anchor_mono = time.monotonic()
            _anchor_server_ms = float(local) + offset
        return True
    return False


def _offset_plausible(offset: float) -> bool:
    # Offset alone has no absolute bound; check the resulting server time.
    server = local_now_ms() + offset
    return MIN_PLAUSIBLE_MS <= server <= MAX_PLAUSIBLE_MS


def save(path: str | None) -> bool:
    """Persist the current offset atomically.  Never raises."""
    if not path:
        return False
    with _LOCK:
        if _source not in TRUSTED_SOURCES:
            return False
        doc = {
            'source': _source,
            'offset_ms': round(_offset_ms),
            'measured_local_ms': int(_measured_local_ms),
            'rtt_ms': int(_rtt_ms),
        }
    try:
        folder = os.path.dirname(os.path.abspath(path)) or '.'
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix='.clock-', suffix='.json')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(doc, f, ensure_ascii=False, separators=(',', ':'))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except Exception:  # noqa: BLE001 - a read-only data dir must not block runs
        return False


def fmt_skew(ms: int) -> str:
    """Human-readable magnitude, e.g. '125 秒' / '3.2 分钟' / '2.1 小时'."""
    seconds = abs(float(ms)) / 1000.0
    if seconds < 180:
        return '%.0f 秒' % seconds
    if seconds < 5400:
        return '%.0f 分钟' % (seconds / 60.0)
    return '%.1f 小时' % (seconds / 3600.0)

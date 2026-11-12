"""Shared response/point helpers for the bridge adapters.

Kept outside ``bridge_api`` so ``bridge_run`` can reuse the same rules without
importing the large adapter module (which would create a cycle).
"""

from __future__ import annotations

import json
import os
import tempfile

#: Default checkpoint-query distance for callers without a target (CLI/tests).
#: The UI passes the selected target distance because the endpoint is
#: distance-scoped; a fixed 2500 m can be an invalid distance for another
#: school.  Submission still asks for the target-specific set.
CAMPUS_POINTS_DISTANCE_M = 2500

#: Prefer the user's own Web-service key.  Do not ship a hard-coded key in an
#: open-source build; AMap binds keys to a domain/app and a leaked one is a
#: billing and quota risk.
AMAP_KEY = os.environ.get('JHP_AMAP_KEY', '')


def integrity_message():
    """Manifest gate message for paths that skip the login shield.

    Module-level so every mixin can call it without depending on
    ``ReplicaApi``'s private methods.  A source checkout returns ``None``
    (``skipped``); a frozen build returns the repair sentence.
    """
    try:
        from ..application import integrity as I
        return I.tamper_message()
    except Exception:  # noqa: BLE001 - a gate bug must not break the UI
        return None


def _ok(data=None, message='success'):
    return {'code': 200, 'message': message, 'data': data}


def _fail(message='failed', code=500, data=None):
    return {'code': code, 'message': message, 'data': data}


def _fail_submit(message='failed', kind='fail', data=None):
    """A submit failure that states whether the write may have landed.

    Every bridge failure uses code 500, so the HTTP status cannot tell an
    explicit refusal (safe to retry) from a request that may already have
    reached the server (must stay locked).  The backend knows which branch it
    took; it records that decision as ``data.failure_kind`` and the frontend
    only renders it.  Values:

    * ``fail``    - nothing was written; the server or the local guard refused
                    the request, so the operator may fix it and resubmit.
    * ``unknown`` - the request may have reached the server; keep the submit
                    button locked until the receipt is reconciled.
    """
    payload = dict(data) if isinstance(data, dict) else {}
    payload['failure_kind'] = kind
    return _fail(message, data=payload)


def _fmt_seconds(seconds):
    """Human-readable remaining freeze time."""
    total = max(0, int(seconds or 0))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return '%d 小时 %d 分' % (hours, minutes)
    if minutes:
        return '%d 分 %d 秒' % (minutes, secs)
    return '%d 秒' % secs


def _normalise_ui_points(points) -> list[dict]:
    """Map the UI's rendered checkpoint rows back to the submit shape.

    ``renderPoints`` stores ``{n, lat, lng, must, isFixed, position, seq}``
    (GCJ).  Submit must use exactly that set: the endpoint returns a random
    five-of-N draw per request, so a fresh fetch is a different set from the
    one the operator previewed.  Invalid rows are dropped; the caller then
    falls back to a live fetch.
    """
    out = []
    for p in points or []:
        if not isinstance(p, dict):
            continue
        try:
            lat = float(p['lat'] if p.get('lat') is not None
                        else p.get('glat'))
            lon = float(p['lon'] if p.get('lon') is not None
                        else (p.get('lng') if p.get('lng') is not None
                              else p.get('glon')))
        except (KeyError, TypeError, ValueError):
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue
        out.append({
            'lat': lat,
            'lon': lon,
            'name': p.get('name') or p.get('n') or '',
            'isMustPoint': bool(p.get('isMustPoint') or p.get('must')),
            'isFixed': p.get('isFixed'),
            'position': p.get('position'),
            'seq': p.get('seq'),
        })
    return out


PLAUSIBLE_PACE_MIN = 4.5
PLAUSIBLE_PACE_MAX = 8.5
PLAUSIBLE_CADENCE_MIN = 150
PLAUSIBLE_CADENCE_MAX = 180


def plausibility_warnings(pace, cadence) -> list[str]:
    """Soft warnings for parameters outside the common running band."""
    out = []
    try:
        p = float(pace)
    except (TypeError, ValueError):
        p = 0.0
    try:
        c = int(float(cadence))
    except (TypeError, ValueError):
        c = 0
    if p and (p < PLAUSIBLE_PACE_MIN or p > PLAUSIBLE_PACE_MAX):
        out.append('配速 %.2f min/km 超出常见 4′30″–8′30″' % p)
    if c and (c < PLAUSIBLE_CADENCE_MIN or c > PLAUSIBLE_CADENCE_MAX):
        out.append('步频 %d spm 超出常见 150–180' % c)
    return out


def _read_json(path: str, default=None):
    """Corrupt or missing file -> `default`, never a crash at startup."""
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _clean_route(route):
    """Validate one route-library item coming from the UI."""
    if not isinstance(route, dict):
        return None
    name = str(route.get('name') or '未命名路线').strip()[:80]
    typ = route.get('type') if route.get('type') in (
        'draw', 'auto', 'stadium', 'file', 'record', 'learned') else 'draw'
    raw = route.get('line')
    if not isinstance(raw, list):
        return None
    line = []
    for p in raw[:50000]:
        if isinstance(p, dict):
            a, b = p.get('lat'), p.get('lng', p.get('lon'))
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            a, b = p[0], p[1]
        else:
            continue
        try:
            a, b = float(a), float(b)
        except (TypeError, ValueError):
            continue
        if -90 <= a <= 90 and -180 <= b <= 180:
            line.append([round(a, 7), round(b, 7)])
    if len(line) < 2:
        return None
    return {'name': name, 'type': typ, 'line': line,
            'closed': bool(route.get('closed')),
            'saved_at': str(route.get('saved_at') or '')[:32]}


def _write_json(path: str, data) -> None:
    """Atomic replace: a reader never sees a half-written file."""
    folder = os.path.dirname(path) or '.'
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.tmp-', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

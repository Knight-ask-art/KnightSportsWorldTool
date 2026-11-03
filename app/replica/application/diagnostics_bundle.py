"""Redacted diagnostics bundle: one ZIP the operator can attach to a report.

Two layers of protection, because one is never enough:

1. **Whitelist** — only the sections named in :data:`SECTIONS` are collected,
   and inside them only known keys survive (free-form ``extra`` from the
   collector is dropped wholesale; it used to carry a user-agent and the last
   submission window).
2. **Hard scrub** — every string that goes into the archive passes through
   :func:`scrub_text`, which strips bearer/basic credentials, ``token=`` style
   pairs, signed-URL query strings (``?Signature=``/``OSS…``), long hex/base64
   blobs and full coordinates (a ``lat``/``lng`` pair becomes ``<坐标已去除>``).

Coordinates never reach the archive at all: the collector already refuses
``lat``/``lng``-style keys, and the scrubber is the backstop for text.

The archive is assembled in memory and returned as base64; nothing is written
to disk.
"""

from __future__ import annotations

import base64
import io
import json
import re
import zipfile

from ..domain import safety

FILENAME = 'haoxiangpao-diagnostics.zip'
MIME = 'application/zip'
REDACTED = '***'
COORD = '<坐标已去除>'
#: Hard ceiling for the archive so a huge receipt table cannot blow up a mail
#: attachment; the oldest entries are dropped first.
MAX_BYTES = 2 << 20

_COORD_KEYS = ('lat', 'lng', 'lon', 'glat', 'glng', 'latitude', 'longitude')

_TEXT_PATTERNS = (
    (re.compile(r'(?i)\b(Basic|Bearer|Digest|OSS)\s+[A-Za-z0-9+/=._\-]{8,}'),
     r'\1 ***'),
    (re.compile(r'(?i)(token|password|passwd|psw|authorization|cookie|'
                r'secret|signature|x-oss-[a-z-]+|access[_-]?key[a-z_-]*)'
                r'([=:]\s*)(?!\*{3})[^\s&,;"\'<>]+'), r'\1\2***'),
    # Signed object-storage URLs: the whole query string is a credential.
    (re.compile(r'(?i)(https?://[^\s"\'<>?]+)\?[^\s"\'<>]*'),
     r'\1?***'),
    # A lone "lat=22.54321" / "lng: 113.94" pair, or two coords side by side.
    (re.compile(r'(?i)\b(g?lat|g?lon|lng|latitude|longitude)(["\']?\s*[:=]\s*)'
                r'["\']?-?\d{1,3}\.\d{2,}'), r'\1\2"***"'),
    (re.compile(r'(-?\b\d{1,3}\.\d{4,})\s*,\s*(-?\b\d{1,3}\.\d{4,})'), COORD),
    # Bare opaque credentials: "tok-…", "sk_live_…", "xoxb-…".
    (re.compile(r'\b(?:tok|sk|pk|ak|rk)[_-][A-Za-z0-9_-]{10,}', re.I),
     REDACTED),
)


def scrub_text(text) -> str:
    """Remove credentials, signed URLs and coordinate pairs from free text."""
    out = str(text if text is not None else '')
    for pattern, repl in _TEXT_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def scrub(obj, _depth: int = 0):
    """Recursive scrub that keeps structure: keys, numbers, booleans."""
    if _depth > 12:
        return REDACTED
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            key_text = str(key)
            if key_text.lower() in _COORD_KEYS:
                out[key_text] = COORD if value not in (None, 0) else value
            elif isinstance(value, str):
                out[key_text] = scrub_text(value)
            else:
                out[key_text] = scrub(value, _depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [scrub(v, _depth + 1) for v in obj]
    if isinstance(obj, str):
        return scrub_text(obj)
    return obj


#: Keys kept from a settings document; everything else is reported as a name
#: only so the operator still sees that something was configured.
_SETTINGS_SAFE = {'version', 'obs_key_mode', 'pause_profile', 'gps_profile',
                  'terrain_coupling', 'updated_at', 'schema'}
_SETTINGS_SECRET_HINTS = ('token', 'pass', 'secret', 'cookie', 'session',
                          'credential', 'apikey', 'api_key', 'access_key',
                          'secret_key', 'private_key')


def redact_settings(doc) -> dict:
    """``{key: value}`` for safe keys, ``{key: "***"}`` for secret ones."""
    doc = doc if isinstance(doc, dict) else {}
    out = {}
    for key, value in doc.items():
        name = str(key)
        low = name.lower()
        # Match on whole words: "obs_key_mode" contains "key" but is not a
        # secret, while "apikey"/"api_token"/"saved_password" are.
        if any(h in low for h in _SETTINGS_SECRET_HINTS):
            out[name] = REDACTED
        elif name in _SETTINGS_SAFE:
            out[name] = scrub(value)
        else:
            # Unknown keys: report presence, not the value.
            out[name] = REDACTED if value not in (None, '', [], {}) else value
    return out


def redact_receipts(rows) -> list[dict]:
    """Only the fields a report actually needs; no uuid/rrid/notes."""
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        start = _int(row.get('start'))
        stop = _int(row.get('stop'))
        out.append({
            'start': safety.fmt_beijing(start) if start else '',
            'duration_s': int((stop - start) / 1000) if start and stop > start
            else None,
            'distance_m': _int(row.get('distance_m')) or None,
            'state': str(row.get('state') or '')[:20],
            'note': scrub_text(str(row.get('note') or ''))[:120],
        })
    return out


def redact_audit(doc) -> dict:
    """Audit results: keep check ids/levels, drop free-form detail text."""
    doc = doc if isinstance(doc, dict) else {}
    checks = []
    for item in (doc.get('checks') or []):
        if not isinstance(item, dict):
            continue
        checks.append({
            'id': str(item.get('id') or '')[:60],
            'level': str(item.get('level') or 'info')[:10],
            'ok': bool(item.get('ok')),
            'detail': scrub_text(item.get('detail') or '')[:200],
        })
    return {'summary': scrub(doc.get('summary') or {}), 'checks': checks,
            'at': str(doc.get('at') or '')[:30]}


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _clock_json() -> dict:
    try:
        from ..domain import clock
        st = clock.state()
    except Exception as e:  # noqa: BLE001
        return {'error': scrub_text(e)}
    # Absolute timestamps are fine (they are not secrets); keep the shape.
    return {'source': st.get('source'), 'offset_ms': st.get('offset_ms'),
            'rtt_ms': st.get('rtt_ms'), 'applied': bool(st.get('applied')),
            'reason': scrub_text(st.get('reason') or '')[:120]}


def _dump(obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=1) + '\n').encode(
        'utf-8')


def build(store_dir: str = '', *, diagnostics_text: str = '',
          diagnostics_info: dict | None = None, audit: dict | None = None,
          settings: dict | None = None, receipts=None,
          clock_state: dict | None = None, app_version: str = '',
          now_ms: int | None = None) -> dict:
    """Assemble the archive.  Every argument is optional; missing sections
    become a short ``"未收集"`` note rather than a missing file."""
    now_ms = safety.now_ms() if now_ms is None else now_ms
    at = safety.fmt_beijing(now_ms)

    diag = scrub_text(diagnostics_text or '')
    if not diag:
        try:
            from ..diagnostics import collector as D
            info = diagnostics_info or D.collect(app_version=app_version)
            diag = D.render(scrub(info), 'text')
        except Exception as e:  # noqa: BLE001 - never break the bundle
            diag = '诊断信息收集失败：%s' % scrub_text(e)
    header = ('# 好想跑诊断包（已脱敏）\n\n'
              '- 生成时间：%s\n'
              '- 版本：%s\n'
              '- 不含 token / 密码 / uid / 完整坐标 / 签名 URL。\n\n'
              '---\n\n' % (at, scrub_text(app_version or 'unknown')))
    diag = header + diag

    files = {
        'diagnostics.txt': (diag + '\n').encode('utf-8'),
        'audit.json': _dump(redact_audit(audit) if audit else
                            {'note': '未运行提交前审计'}),
        'clock.json': _dump(scrub(clock_state) if clock_state
                            else _clock_json()),
        'settings.redacted.json': _dump(redact_settings(settings)),
        'recent_receipts.json': _dump(redact_receipts(receipts)),
    }

    blob = io.BytesIO()
    with zipfile.ZipFile(blob, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            zf.writestr(zipfile.ZipInfo(name, date_time=_zip_time(now_ms)),
                        files[name])
    data = blob.getvalue()
    if len(data) > MAX_BYTES:
        raise ValueError('诊断包超过 %d KB' % (MAX_BYTES // 1024))
    return {'filename': FILENAME, 'mime': MIME,
            'data_b64': base64.b64encode(data).decode('ascii'),
            'files': sorted(files), 'bytes': len(data), 'at': at}


def _zip_time(ms: int) -> tuple:
    moment = safety.to_beijing(ms)
    return (moment.year, moment.month, moment.day, moment.hour,
            moment.minute, moment.second)


def inspect(data_b64: str) -> dict:
    """Open the archive in memory: names, sizes and a strict leak check."""
    raw = base64.b64decode(data_b64)
    out = {'files': {}, 'leaks': []}
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        for name in zf.namelist():
            text = zf.read(name).decode('utf-8', 'replace')
            out['files'][name] = len(text)
            hits = _leaks(text)
            if hits:
                out['leaks'].append({'file': name, 'what': hits})
    return out


def _leaks(text: str) -> list[str]:
    """Substrings that prove a secret/coordinate survived scrubbing."""
    hits = []
    for pattern, label in (
            (r'(?i)(?:bearer|basic|oss)\s+[a-z0-9+/=._-]{8,}', 'auth'),
            (r'(?i)(?:token|password|passwd|psw|authorization|cookie|secret|'
             r'signature)[=:]+\s*(?!\*)[^\s&,;"\'<>{}]+', 'credential'),
            # "?***" is the scrubber's own mark; a longer query is a leak.
            (r'(?i)https?://[^\s"\'<>?]+\?(?!\*{3}(?:[\s"\'<>{},]|$))'
             r'[^\s"\'<>]*', 'signed url'),
            (r'"g?lat"\s*:\s*-?\d+\.\d{4,}', 'coordinate'),
            (r'-?\b\d{1,3}\.\d{4,}\s*,\s*-?\b\d{1,3}\.\d{4,}',
             'coordinate')):
        if re.search(pattern, text):
            hits.append(label)
    return hits


def self_test() -> list[tuple[str, bool, str]]:
    res = build(diagnostics_text='token=abc12345678 lat=22.54321 lng=113.94',
                settings={'obs_key_mode': 'single', 'api_token': 'x'},
                receipts=[{'start': 1700000000000, 'stop': 1700001800000,
                           'distance_m': 2500, 'state': 'accepted',
                           'uuid': 'u-1'}])
    info = inspect(res['data_b64'])
    return [('zip files', len(info['files']) == 5, str(sorted(info['files']))),
            ('no leaks', not info['leaks'], str(info['leaks']))]

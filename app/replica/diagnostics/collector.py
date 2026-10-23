"""Redacted diagnostic bundle — safe to paste into an issue.

Ported from YanamiNeko/NekoSportsWorldTool `src/diagnostics.rs` (whitelist
approach).  Only non-identifying facts are emitted; anything that could link a
report back to an account is either omitted or masked.

Never included: token, password, uid, DeviceId, coordinates, IP, raw request or
response bodies, school/campus numeric ids, local file paths.
"""

from __future__ import annotations

import json
import platform
import sys
import time

from ..transport import netsec
from ..domain import safety


def _mask_id(value) -> str:
    if value in (None, ''):
        return '(unset)'
    s = str(value)
    return s[:3] + '*' * max(0, len(s) - 5) + s[-2:] if len(s) > 5 else '***'


def collect(crypto_backend: str = '?', app_version: str = '?',
            extra: dict | None = None) -> dict:
    """Facts that are safe to share."""
    info = {
        'generated_at': safety.fmt_beijing(safety.now_ms()),
        'python': sys.version.split()[0],
        'platform': platform.platform(),
        'crypto_backend': crypto_backend,
        'app_version': app_version,
        'crypto_selftest': _selftest_summary(),
    }
    if extra:
        for k, v in extra.items():
            if any(s in k.lower() for s in ('token', 'pass', 'uid',
                                            'deviceid', 'lat', 'lng', 'lon',
                                            'ip', 'path')):
                continue
            info[k] = v
    return info


def _selftest_summary() -> dict:
    out = {}
    for name, fn in (('netsec', netsec.self_test), ):
        try:
            results = fn()
            out[name] = {'pass': sum(1 for _, ok, _ in results if ok),
                         'total': len(results)}
        except Exception as e:  # noqa: BLE001
            out[name] = {'error': str(e)[:80]}
    try:
        from ..transport import gt4
        r = gt4.self_test()
        out['gt4'] = {'pass': sum(1 for _, ok, _ in r if ok), 'total': len(r)}
    except Exception as e:  # noqa: BLE001
        out['gt4'] = {'error': str(e)[:80]}
    return out


def render(info: dict, fmt: str = 'text') -> str:
    if fmt == 'json':
        return json.dumps(info, ensure_ascii=False, indent=1)
    lines = ['# 诊断包（已脱敏）', '']
    for k, v in info.items():
        if isinstance(v, dict):
            lines.append('- **%s**: %s' % (k, json.dumps(v, ensure_ascii=False)))
        else:
            lines.append('- **%s**: %s' % (k, v))
    lines += ['', '> 不含 token / 密码 / uid / 设备号 / 坐标 / IP / 原始响应。']
    return '\n'.join(lines)


def write(path: str, info: dict | None = None, fmt: str = 'text') -> str:
    info = info or collect()
    text = render(info, fmt)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    return path


# --------------------------------------------------------------------------
# self test
# --------------------------------------------------------------------------

def self_test() -> list[tuple[str, bool, str]]:
    out = []
    info = collect(crypto_backend='netsec', app_version='7.3.11',
                   extra={'uid': 99999999, 'token': 'abc', 'note': 'ok',
                          'lat': 22.4})
    out.append(('secrets dropped', 'uid' not in info and 'token' not in info
                and 'lat' not in info, str(sorted(info))))
    out.append(('safe extra kept', info.get('note') == 'ok', 'ok'))
    text = render(info, 'text')
    out.append(('no secret in text', 'abc' not in text and '99999999' not in text,
                '%d chars' % len(text)))
    js = render(info, 'json')
    out.append(('json renders', js.strip().startswith('{'), '%d chars' % len(js)))
    out.append(('mask keeps shape', _mask_id('99999999').endswith('99'),
                _mask_id('99999999')))
    out.append(('mask unset', _mask_id(None) == '(unset)', '(unset)'))
    return out


if __name__ == '__main__':
    ok = True
    for name, passed, detail in self_test():
        print('%-30s %s   %s' % (name, 'PASS' if passed else 'FAIL', detail))
        ok = ok and passed
    print('\nRESULT:', 'PASS' if ok else 'FAIL')
    raise SystemExit(0 if ok else 1)

"""GeeTest v4 public modulus — built-in, refreshable, never fatal.

The ``w`` trailer is ``hex(RSA_PKCS1v15(str16))``.  Upstream keeps the modulus
inside ``gcaptcha4.js``; we shipped it hard-coded, which means a rotation
turns into "everything fails with no clue" until someone edits 37 integers.

This module keeps the hard-coded value as a **floor** and tries to refresh it
from the JS bundle that ``/load`` points at (``static_path``).  The refresh is
strictly best-effort:

* parse finds a candidate integer array, rebuilds the modulus;
* candidate must be odd, >= 1024 bits, and exactly 1024/2048 bits;
* anything unexpected -> keep the built-in modulus and say so in the log.

So a wrong guess can never take down a working login; it can only fail to
improve it.  ``JHP_GT4_RSA=off`` disables the network fetch entirely.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request

STATIC_HOST = 'https://static.geetest.com'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36')

#: GeeTest's jsbn-style key: 37 limbs of 28 bits, e = 65537.
BUILTIN_LIMBS = [
    134982529, 254232810, 164556709, 234907349, 134685994, 35463984, 258277946,
    12518857, 44638621, 93783641, 212253739, 62792472, 186688352, 109500232,
    182488077, 261196188, 26354094, 103248217, 106891695, 165771045, 41530993,
    263704736, 111785174, 12753611, 232116673, 155524985, 218291229, 122452343,
    248250238, 118739550, 251169095, 129059733, 149835464, 5498868, 71719731,
    154456417, 49635,
]
E = 65537

#: Smallest key size GeeTest has ever used.  Anything below is a parse error.
MIN_BITS = 1024

_ACTIVE_LIMBS: list[int] | None = None

_ARRAY_RE = re.compile(r'\[\s*(?:\d{1,9}\s*,\s*){18,}\d{1,9}\s*\]')
_HEX_RE = re.compile(
    r'\[\s*(?:0x[0-9a-fA-F]{1,8}\s*,\s*){18,}0x[0-9a-fA-F]{1,8}\s*\]')


def limbs_to_int(limbs: list[int]) -> int:
    n = 0
    for limb in reversed(limbs):
        n = n * (1 << 28) + limb
    return n


def int_to_limbs(n: int, width: int = 28) -> list[int]:
    """Little-endian, matching the order GeeTest's jsbn array uses."""
    out = []
    while n > 0:
        out.append(n & ((1 << width) - 1))
        n >>= width
    return out or [0]


def builtin_modulus() -> int:
    return limbs_to_int(BUILTIN_LIMBS)


def modulus() -> int:
    return limbs_to_int(_ACTIVE_LIMBS) if _ACTIVE_LIMBS else builtin_modulus()


def modulus_bits() -> int:
    return modulus().bit_length()


def active_source() -> str:
    return 'js' if _ACTIVE_LIMBS else 'builtin'


def set_modulus(limbs: list[int] | None) -> bool:
    """Install a modulus after validating it.  ``None`` restores the builtin."""
    global _ACTIVE_LIMBS
    if not limbs:
        _ACTIVE_LIMBS = None
        return False
    n = limbs_to_int(limbs)
    if n <= 0 or n % 2 == 0 or n.bit_length() < MIN_BITS:
        return False
    if n.bit_length() % 8 != 0:
        return False
    _ACTIVE_LIMBS = list(limbs)
    return True


def _candidate_arrays(js: str) -> list[list[int]]:
    out = []
    for match in _ARRAY_RE.finditer(js):
        body = match.group(0)[1:-1]
        try:
            out.append([int(part) for part in body.split(',')])
        except ValueError:
            continue
    for match in _HEX_RE.finditer(js):
        body = match.group(0)[1:-1]
        try:
            out.append([int(part, 16) for part in body.split(',')])
        except ValueError:
            continue
    return out


def parse_limbs(js: str) -> list[int] | None:
    """Pick the integer array that looks like an RSA modulus.

    Several arrays in a minified bundle can be long enough; the winner has to
    rebuild to a whole-byte, odd modulus of 1024 or 2048 bits.  If two do, the
    1024-bit one wins (that is what the widget has always used).
    """
    best = None
    for limbs in _candidate_arrays(js):
        n = limbs_to_int(limbs)
        if n <= 0 or n % 2 == 0:
            continue
        bits = n.bit_length()
        if bits not in (1024, 2048):
            continue
        rank = (bits != 1024, -len(limbs))
        if best is None or rank < best[0]:
            best = (rank, limbs)
    return best[1] if best else None


def fetch_js(static_path: str, timeout: float = 20.0) -> str:
    base = STATIC_HOST + '/' + static_path.strip('/')
    url = base + '/js/gcaptcha4.js'
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode('utf-8', 'replace')


def _cache_file(cache_dir: str, static_path: str) -> str:
    safe = re.sub(r'[^0-9A-Za-z._-]', '_', static_path.strip('/')) or 'unknown'
    return os.path.join(cache_dir, 'rsa-%s.json' % safe)


def _load_cache(path: str, static_path: str) -> list[int] | None:
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if data.get('static_path') != static_path:
        return None
    limbs = data.get('limbs')
    if not isinstance(limbs, list) or not all(isinstance(x, int) for x in limbs):
        return None
    return limbs if set_modulus(limbs) else None


def _save_cache(path: str, static_path: str, limbs: list[int]) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump({'static_path': static_path, 'limbs': limbs,
                       'bits': limbs_to_int(limbs).bit_length()}, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def refresh(static_path: str, cache_dir: str, log=None,
            fetch=None) -> dict:
    """Try to load the modulus that ships with this GeeTest build.

    Returns ``{'source','bits','static_path','changed','error'}``.  Never
    raises: a failure just leaves the built-in modulus in place.
    """
    log = log or (lambda *_: None)
    result = {'source': active_source(), 'bits': modulus_bits(),
              'static_path': static_path, 'changed': False, 'error': ''}
    if not static_path:
        result['error'] = 'no static_path'
        return result
    if str(os.environ.get('JHP_GT4_RSA', '')).strip().lower() in (
            'off', '0', 'false', 'no'):
        result['error'] = 'disabled'
        return result

    path = _cache_file(cache_dir, static_path)
    limbs = _load_cache(path, static_path)
    if limbs:
        result.update(source='js', bits=modulus_bits(), changed=True)
        log('[gt4] RSA modulus from cache (%d bits)' % result['bits'])
        return result

    before = modulus()
    try:
        js = (fetch or fetch_js)(static_path)
        limbs = parse_limbs(js)
        if not limbs:
            result['error'] = 'no 1024/2048-bit candidate array'
            log('[gt4] RSA refresh: %s; keeping %s modulus'
                % (result['error'], active_source()))
            return result
        _save_cache(path, static_path, limbs)
    except Exception as exc:  # noqa: BLE001 - network is best-effort
        result['error'] = str(exc)[:120]
        log('[gt4] RSA refresh failed (%s); keeping %s modulus'
            % (result['error'], active_source()))
        return result

    set_modulus(limbs)
    result.update(source='js', bits=modulus_bits(), changed=modulus() != before)
    log('[gt4] RSA modulus refreshed from %s (%d bits%s)'
        % (static_path, result['bits'],
           ', changed' if result['changed'] else ', same'))
    return result


def rsa_encrypt_hex(data: bytes) -> str:
    """PKCS#1 v1.5 type-2 (encryption) padding, then raw RSA."""
    import random
    n = modulus()
    k = (n.bit_length() + 7) // 8
    if len(data) > k - 11:
        raise ValueError('RSA payload too long')
    ps = bytes(random.randint(1, 255) for _ in range(k - 3 - len(data)))
    em = b'\x00\x02' + ps + b'\x00' + data
    c = pow(int.from_bytes(em, 'big'), E, n)
    return c.to_bytes(k, 'big').hex()


def self_test() -> list[tuple[str, bool, str]]:
    out = []
    builtin = builtin_modulus()
    out.append(('builtin is 1024 bit', builtin.bit_length() == 1024,
                '%d bits' % builtin.bit_length()))
    out.append(('builtin is odd', builtin % 2 == 1, ''))

    # 37 limbs round-trip through the cache representation.
    limbs = int_to_limbs(builtin)
    out.append(('limb round-trip', limbs == BUILTIN_LIMBS,
                '%d limbs' % len(limbs)))

    # Parser: a synthetic bundle carrying the same key must be found again.
    js = ('var a=[1,2,3];var key=[%s];function f(){return 1}'
          % ','.join(str(x) for x in BUILTIN_LIMBS))
    parsed = parse_limbs(js)
    out.append(('parser finds modulus', parsed == BUILTIN_LIMBS,
                '%s limbs' % (len(parsed) if parsed else 0)))

    # A 1024-bit even number is not a modulus; a short array is not either.
    out.append(('parser rejects noise', parse_limbs('var x=[1,2,3,4,5]') is None,
                ''))

    # A wrong candidate must not be installed.
    out.append(('invalid candidate refused', not set_modulus([2] * 37), ''))

    # round-trip through the active key
    set_modulus(None)
    ct = rsa_encrypt_hex(b'0123456789abcdef')
    out.append(('ciphertext is 128 bytes', len(ct) == 256, '%d hex chars' % len(ct)))
    out.append(('modulus restored to builtin', active_source() == 'builtin', ''))
    return out


if __name__ == '__main__':
    ok = True
    for name, passed, detail in self_test():
        print('%-30s %s   %s' % (name, 'PASS' if passed else 'FAIL', detail))
        ok = ok and passed
    print('\nRESULT:', 'PASS' if ok else 'FAIL')
    raise SystemExit(0 if ok else 1)

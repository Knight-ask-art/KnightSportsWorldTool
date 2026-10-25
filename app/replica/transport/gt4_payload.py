"""GeeTest v4 ``w`` payload — field construction only, no network, no images.

Ported from YanamiNeko/NekoSportsWorldTool ``src/api/gt4.rs``.

The whole point of this module is the **field set**.  GeeTest hands out a
ticket that *looks* fine even when the payload is incomplete; the business
server behind it scores the ticket and answers ``10003``.  The field that bit
us for months is the per-challenge dynamic key:

    "<lot_number[26:30]><lot_number[7:11]>": "<lot_number[6:14]>"

It is generated per challenge, so it cannot be recorded once and replayed.

**Why this file is separate from** :mod:`replica.transport.gt4_image`:
upstream changes the field set far more often than the image pipeline.  A
field-only update should not require re-validating Canny/ZNCC, and a CV change
must not be able to silently reshape the payload.

``PAYLOAD_KEYS`` is the baseline.  ``build_payload`` validates every payload
against it and raises :class:`PayloadError`, so dropping or reordering a field
fails loudly here instead of becoming a low-trust ticket in production.
"""

from __future__ import annotations

import hashlib
import json
import random

from .gt4_rsa import rsa_encrypt_hex

#: Slider track width in CSS pixels (GeeTest's own widget constant).
TRACK_WIDTH = 340.0

#: ``userresponse = setLeft / RATIO + 2`` in GeeTest's JS.  The ratio encodes
#: ``IMG_REF_WIDTH / TRACK_WIDTH`` for the standard render.
USERRESPONSE_RATIO = 1.0059466666666665

#: Image width the ratio above assumes (~342px).  Images inside the normal
#: band keep the exact expression that has already worked online; only a
#: genuinely different render (retina, a resized widget) is rescaled.
IMG_REF_WIDTH = TRACK_WIDTH * USERRESPONSE_RATIO
NORMAL_WIDTH_MIN = 280.0
NORMAL_WIDTH_MAX = 420.0


class PayloadError(ValueError):
    """The payload no longer matches the recorded GeeTest field baseline."""


def str16() -> str:
    """Four 4-hex chunks, the format GeeTest's AES key generator produces."""
    out = []
    for _ in range(4):
        v = int(65536.0 * (1.0 + random.random()))
        out.append(format(v, 'x')[1:5])
    return ''.join(out)


def _leading_zero_bits(digest: bytes) -> int:
    total = 0
    for byte in digest:
        if byte == 0:
            total += 8
            continue
        total += 8 - byte.bit_length()
        break
    return total


def pow_msg(pow_detail: dict, captcha_id: str, lot_number: str) -> str:
    """Find a nonce that satisfies the PoW the *server* asked for.

    The old code hard-coded sha256 and eight zero bits.  That happened to be
    stricter than what GeeTest sends today (``bits: 0``, ``hashfunc: md5``),
    so it kept working — but it ignored both stated parameters, and would fail
    outright the day they ask for more than eight bits.

    ``bits == 0`` means "no work required": return a random nonce immediately.
    """
    raw_bits = pow_detail.get('bits')
    try:
        bits = int(raw_bits)
    except (TypeError, ValueError):
        bits = 0
    func = str(pow_detail.get('hashfunc') or 'sha256').strip().lower()
    base = '%s|%s|%s|%s|%s|%s||' % (
        pow_detail.get('version', ''),
        raw_bits,
        pow_detail.get('hashfunc', ''),
        pow_detail.get('datetime', ''),
        captcha_id,
        lot_number,
    )
    if bits <= 0:
        return base + ''.join(random.choice('0123456789abcdef')
                              for _ in range(16))
    hasher = hashlib.md5 if func == 'md5' else hashlib.sha256
    while True:
        nonce = ''.join(random.choice('0123456789abcdef') for _ in range(16))
        if _leading_zero_bits(hasher((base + nonce).encode()).digest()) >= bits:
            return base + nonce


def dynamic_key(lot_number: str) -> str:
    """The per-challenge key name.  This is the field that caused 10003."""
    return lot_number[26:30] + lot_number[7:11]


def dynamic_value(lot_number: str) -> str:
    return lot_number[6:14]


def expected_keys(lot_number: str) -> tuple[str, ...]:
    """Field baseline for one challenge, in upload order."""
    return (
        'setLeft', 'passtime', 'userresponse', 'device_id', 'lot_number',
        'pow_msg', 'pow_sign', 'geetest', 'lang', 'ep', 'biht', 'gee_guard',
        'YciC', dynamic_key(lot_number), 'em',
    )


def userresponse(set_left_px: int, bg_width: int | None = None) -> float:
    """Image-pixel gap -> slider-track coordinate.

    ``bg_width`` is the width returned by ``gt4_image.locate_gap``.  A normal
    render keeps the exact expression that has already worked online; only a
    genuinely scaled image is rescaled.

    The band matters: ``IMG_REF_WIDTH`` is ~342, so a plain ``abs(diff) > 2``
    test would have rescaled a 340px render too and silently moved every
    coordinate by 0.6%.  Do not tighten this without a live sample.
    """
    x = float(set_left_px)
    if bg_width and (bg_width < NORMAL_WIDTH_MIN
                     or bg_width > NORMAL_WIDTH_MAX):
        x *= IMG_REF_WIDTH / float(bg_width)
    return x / USERRESPONSE_RATIO + 2.0


def passtime_for(distance_px: int) -> int:
    """A plausible human drag time, instead of a constant the server can spot."""
    base = 900.0 + 4.6 * max(0, int(distance_px))
    return int(min(3400.0, base + random.uniform(0.0, 420.0)))


def payload_dict(set_left: int, *, lot_number: str, pow_msg: str,
                 pow_sign: str, bg_width: int | None = None,
                 passtime: int | None = None) -> dict:
    """Build the ``w`` plaintext as an ordered dict."""
    if passtime is None:
        passtime = passtime_for(set_left)
    key = dynamic_key(lot_number)
    return {
        'setLeft': int(set_left),
        'passtime': int(passtime),
        'userresponse': userresponse(set_left, bg_width),
        'device_id': '',
        'lot_number': lot_number,
        'pow_msg': pow_msg,
        'pow_sign': pow_sign,
        'geetest': 'captcha',
        'lang': 'zh',
        'ep': '123',
        'biht': '1426265548',
        'gee_guard': {'roe': {
            'aup': '3', 'sep': '3', 'egp': '3', 'auh': '3',
            'rew': '3', 'snh': '3', 'res': '3', 'cdc': '3',
        }},
        'YciC': 'P3Vn',
        key: dynamic_value(lot_number),
        'em': {'ph': 0, 'cp': 0, 'ek': '11', 'wd': 1,
               'nt': 0, 'si': 0, 'sc': 0},
    }


def build_payload(set_left: int, *, lot_number: str, pow_msg: str,
                  pow_sign: str, bg_width: int | None = None,
                  passtime: int | None = None) -> str:
    """Serialise the ``w`` plaintext exactly as GeeTest expects it."""
    data = payload_dict(set_left, lot_number=lot_number, pow_msg=pow_msg,
                        pow_sign=pow_sign, bg_width=bg_width,
                        passtime=passtime)
    validate_payload(data, lot_number)
    return json.dumps(data, ensure_ascii=False, separators=(',', ':'))


def validate_payload(data: dict, lot_number: str) -> None:
    """Tripwire: a changed field set must fail here, not in production."""
    want = expected_keys(lot_number)
    got = tuple(data.keys())
    if got == want:
        return
    missing = [k for k in want if k not in got]
    extra = [k for k in got if k not in want]
    if missing or extra:
        raise PayloadError(
            'GeeTest w 字段集变了（缺 %s / 多 %s）；先对照上游 gt4.rs，'
            '不要直接发出去 —— 低信任票会被服务端 10003'
            % (missing or '无', extra or '无'))
    raise PayloadError('GeeTest w 字段顺序变了：%s' % (got,))


def aes_hex(plaintext: str, key: str) -> str:
    """AES-128-CBC, IV = ascii "0"*16, PKCS#7, lowercase hex."""
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad
    ct = AES.new(key.encode(), AES.MODE_CBC,
                 iv=b'0000000000000000').encrypt(pad(plaintext.encode(), 16))
    return ct.hex()


def build_w(set_left: int, lot_number: str, pow_msg: str, pow_sign: str,
            str16_key: str, *, bg_width: int | None = None,
            passtime: int | None = None) -> str:
    """``hex(AES(payload)) || hex(RSA(str16))`` — the value of ``w``."""
    plaintext = build_payload(set_left, lot_number=lot_number,
                              pow_msg=pow_msg, pow_sign=pow_sign,
                              bg_width=bg_width, passtime=passtime)
    return aes_hex(plaintext, str16_key) + rsa_encrypt_hex(str16_key.encode())


#: sha256 of the payload that the pre-split implementation produced for a fixed
#: input.  Pinned so a refactor cannot silently change a single byte.
GOLDEN_LOT = '0123456789abcdef0123456789abcdef'
GOLDEN_POW = 'v1|5|sha256|2026-09-09T00:00:00|cid|lot||deadbeefdeadbeef'
GOLDEN_SHA256 = '6a2713d391718ba2e3fde333d4278b72f8217da15a85597002d7341f64145f5f'


def self_test() -> list[tuple[str, bool, str]]:
    out = []

    golden = build_payload(181, lot_number=GOLDEN_LOT, pow_msg=GOLDEN_POW,
                           pow_sign='sign', passtime=1887)
    digest = hashlib.sha256(golden.encode()).hexdigest()
    out.append(('payload byte-identical to pre-split', digest == GOLDEN_SHA256,
                digest[:16]))

    keys = tuple(json.loads(golden).keys())
    out.append(('field baseline', keys == expected_keys(GOLDEN_LOT),
                '%d fields' % len(keys)))

    dyn = dynamic_key(GOLDEN_LOT)
    out.append(('dynamic key slice', dyn == 'abcd789a' and
                json.loads(golden).get(dyn) == '6789abcd', dyn))

    w = build_w(123, GOLDEN_LOT, GOLDEN_POW, 'sign', '0123456789abcdef',
                passtime=1887)
    out.append(('w is hex', all(c in '0123456789abcdef' for c in w),
                '%d chars' % len(w)))
    out.append(('w longer than RSA blob', len(w) > 260, '%d chars' % len(w)))

    msg = pow_msg({'version': 'v1', 'bits': 5, 'hashfunc': 'sha256',
                   'datetime': '2026-09-09T00:00:00'}, 'cid', 'lot')
    out.append(('pow prefix', msg.startswith(
        'v1|5|sha256|2026-09-09T00:00:00|cid|lot||'), msg[:40]))
    out.append(('pow tail is 16 hex', len(msg.rsplit('|', 1)[-1]) == 16,
                msg[-16:]))

    # A retina render must be halved; a normal one must not move.
    normal = userresponse(181, 342)
    legacy = 181 / USERRESPONSE_RATIO + 2.0
    doubled = userresponse(181, 684)
    out.append(('normal render unchanged', abs(normal - legacy) < 1e-9,
                '%.6f' % normal))
    out.append(('retina render halved', 80 < doubled < 100, '%.2f' % doubled))

    # Dropping the dynamic key must be refused, not silently accepted.
    data = payload_dict(1, lot_number=GOLDEN_LOT, pow_msg='p', pow_sign='s',
                        passtime=1000)
    data.pop(dynamic_key(GOLDEN_LOT))
    try:
        validate_payload(data, GOLDEN_LOT)
        out.append(('missing dynamic key refused', False, 'no exception'))
    except PayloadError:
        out.append(('missing dynamic key refused', True, 'PayloadError'))
    return out


if __name__ == '__main__':
    ok = True
    for name, passed, detail in self_test():
        print('%-34s %s   %s' % (name, 'PASS' if passed else 'FAIL', detail))
        ok = ok and passed
    print('\nRESULT:', 'PASS' if ok else 'FAIL')
    raise SystemExit(0 if ok else 1)

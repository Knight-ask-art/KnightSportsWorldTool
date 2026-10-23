"""GeeTest v4 slider solver — pure Python, no browser, no emulator.

Ported from YanamiNeko/NekoSportsWorldTool (`src/api/gt4.rs` +
`src/api/gt4image.rs`).  The whole flow runs locally:

    GET  /load      -> bg/slice images + lot_number/payload/process_token/pow_detail
    (image)         -> gap x  (gray -> gaussian 5x5 sigma 1.1 -> Canny(100,200)
                              -> zero-mean normalised cross correlation)
    (PoW)           -> nonce so that sha256(pow_msg_base + nonce) starts with "00"
    (get_w)         -> hex(AES-128-CBC(plaintext, key=str16, iv=ascii"0"*16))
                       || hex(RSA-PKCS1v15(str16))
    GET  /verify    -> {lotNumber, captchaOutput, passToken, genTime}

Those four credentials are then POSTed to `api/v70270/security/geevalidate`.

The `w` plaintext contains an anti-forgery *dynamic key* derived from
lot_number — omitting it yields a low-trust ticket that the app server rejects
with 10003 even though GeeTest itself answered success.  That was the real
reason our earlier hand-rolled attempts failed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import string
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import cv2
import numpy as np

GEE_HOST = 'https://gcaptcha4.geetest.com'
STATIC_HOST = 'https://static.geetest.com/'
UA_WEB = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
          '(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36')

#: bizType=0 captcha id used by the iOS/web login chain (upstream constant).
CAPTCHA_ID_BIZ0 = '8c065103d81f5fd3efec8ad3e3a84c30'

#: candidate ids, tried in order when the caller does not pin one.  The second
#: value is the one present in our 7.2.70/7.3.11 resources (gt4_captcha_login).
CAPTCHA_ID_CANDIDATES = [
    CAPTCHA_ID_BIZ0,
    '3b02ad39bd099fd3a8336d9347a189ab',
]

#: GeeTest's jsbn-style RSA public key for the web widget (e = 65537).
_RSA_LIMBS = [
    134982529, 254232810, 164556709, 234907349, 134685994, 35463984, 258277946,
    12518857, 44638621, 93783641, 212253739, 62792472, 186688352, 109500232,
    182488077, 261196188, 26354094, 103248217, 106891695, 165771045, 41530993,
    263704736, 111785174, 12753611, 232116673, 155524985, 218291229, 122452343,
    248250238, 118739550, 251169095, 129059733, 149835464, 5498868, 71719731,
    154456417, 49635,
]
_RSA_E = 65537


def _gee_rsa_n() -> int:
    n = 0
    for limb in reversed(_RSA_LIMBS):
        n = n * (1 << 28) + limb
    return n


def _rsa_encrypt_hex(data: bytes) -> str:
    """PKCS#1 v1.5 type-2 (encryption) padding, then raw RSA."""
    n = _gee_rsa_n()
    k = (n.bit_length() + 7) // 8
    if len(data) > k - 11:
        raise ValueError('RSA payload too long')
    ps = bytes(random.randint(1, 255) for _ in range(k - 3 - len(data)))
    em = b'\x00\x02' + ps + b'\x00' + data
    c = pow(int.from_bytes(em, 'big'), _RSA_E, n)
    return c.to_bytes(k, 'big').hex()


# --------------------------------------------------------------------------
# image: locate the gap
# --------------------------------------------------------------------------

def _decode_gray(png: bytes) -> np.ndarray:
    arr = np.frombuffer(png, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError('PNG decode failed')
    if img.ndim == 3 and img.shape[2] == 4:
        b, g, r, a = (img[:, :, i].astype(np.uint32) for i in range(4))
        a = a // 255
        b, g, r = b * a, g * a, r * a
        gray = (r * 299 + g * 587 + b * 114) // 1000
    elif img.ndim == 3:
        b, g, r = (img[:, :, i].astype(np.uint32) for i in range(3))
        gray = (r * 299 + g * 587 + b * 114) // 1000
    else:
        gray = img.astype(np.uint32)
    return np.clip(gray, 0, 255).astype(np.uint8)


def _gaussian_blur(src: np.ndarray) -> np.ndarray:
    """Separable 5-tap gaussian, sigma = 1.1, clamped edges (matches upstream)."""
    sigma = 1.1
    xs = np.arange(5, dtype=np.float64) - 2.0
    kernel = np.exp(-(xs * xs) / (2 * sigma * sigma))
    kernel /= kernel.sum()
    f = src.astype(np.float64)
    # horizontal then vertical, edge-clamped
    pad = np.pad(f, ((0, 0), (2, 2)), mode='edge')
    tmp = sum(kernel[i] * pad[:, i:i + f.shape[1]] for i in range(5))
    pad = np.pad(tmp, ((2, 2), (0, 0)), mode='edge')
    out = sum(kernel[i] * pad[i:i + f.shape[0], :] for i in range(5))
    return out


def _canny(img: np.ndarray, low: float = 100.0, high: float = 200.0) -> np.ndarray:
    """Sobel -> magnitude/angle -> non-maximum suppression -> hysteresis."""
    f = img.astype(np.float64)
    # 3x3 Sobel with edge clamping
    p = np.pad(f, 1, mode='edge')
    gx = (-p[:-2, :-2] + p[:-2, 2:]
          - 2 * p[1:-1, :-2] + 2 * p[1:-1, 2:]
          - p[2:, :-2] + p[2:, 2:])
    gy = (-p[:-2, :-2] - 2 * p[:-2, 1:-1] - p[:-2, 2:]
          + p[2:, :-2] + 2 * p[2:, 1:-1] + p[2:, 2:])
    mag = np.hypot(gx, gy)
    ang = np.arctan2(gy, gx) % np.pi

    h, w = mag.shape
    nms = np.zeros_like(mag)
    # quantise to 4 directions exactly like the reference implementation
    pad = np.pad(mag, 1, mode='edge')
    for y in range(h):
        for x in range(w):
            a = ang[y, x]
            if a < np.pi / 8 or a >= 7 * np.pi / 8:
                (dx, dy) = (1, 0)
            elif a < 3 * np.pi / 8:
                (dx, dy) = (1, 1)
            elif a < 5 * np.pi / 8:
                (dx, dy) = (0, 1)
            else:
                (dx, dy) = (-1, 1)
            m = pad[y + 1, x + 1]
            if m >= pad[y + 1 + dy, x + 1 + dx] and m >= pad[y + 1 - dy, x + 1 - dx]:
                nms[y, x] = m

    out = np.zeros(img.shape, np.uint8)
    strong = nms >= high
    weak = nms >= low
    out[strong] = 255
    # hysteresis: BFS from strong pixels through weak ones
    stack = list(zip(*np.nonzero(strong)))
    while stack:
        y, x = stack.pop()
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                ny, nx = y + dy, x + dx
                if 0 <= ny < h and 0 <= nx < w and out[ny, nx] == 0 and weak[ny, nx]:
                    out[ny, nx] = 255
                    stack.append((ny, nx))
    return out


def gap_distance(bg_png: bytes, slice_png: bytes) -> int:
    """Return the gap x offset (left edge of the best match)."""
    bg_gray = _decode_gray(bg_png)
    sl_gray = _decode_gray(slice_png)
    bg_edges = _canny(_gaussian_blur(bg_gray))
    sl_edges = _canny(_gaussian_blur(sl_gray))
    if int((sl_edges == 255).sum()) < 10:
        raise ValueError('slice has too few edge pixels (recognition failed)')
    if (sl_edges.shape[0] > bg_edges.shape[0]
            or sl_edges.shape[1] > bg_edges.shape[1]):
        raise ValueError('slice larger than background')
    res = cv2.matchTemplate(bg_edges, sl_edges, cv2.TM_CCOEFF_NORMED)
    _, _, _, max_loc = cv2.minMaxLoc(res)
    return int(max_loc[0])


# --------------------------------------------------------------------------
# proof of work
# --------------------------------------------------------------------------

def _pow_msg(pow_detail: dict, captcha_id: str, lot_number: str) -> str:
    base = '%s|%s|%s|%s|%s|%s||' % (
        pow_detail.get('version', ''),
        pow_detail.get('bits'),
        pow_detail.get('hashfunc', ''),
        pow_detail.get('datetime', ''),
        captcha_id,
        lot_number,
    )
    while True:
        nonce = ''.join(random.choice('0123456789abcdef') for _ in range(16))
        if hashlib.sha256((base + nonce).encode()).hexdigest().startswith('00'):
            return base + nonce


def _str16() -> str:
    out = []
    for _ in range(4):
        v = int(65536.0 * (1.0 + random.random()))
        out.append(format(v, 'x')[1:5])
    return ''.join(out)


# --------------------------------------------------------------------------
# w construction
# --------------------------------------------------------------------------

def _aes_hex(plaintext: str, key: str) -> str:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad
    ct = AES.new(key.encode(), AES.MODE_CBC,
                 iv=b'0000000000000000').encrypt(
        pad(plaintext.encode(), 16))
    return ct.hex()


def build_w(set_left: int, lot_number: str, pow_msg: str, pow_sign: str,
            str16: str) -> str:
    userresponse = set_left / 1.0059466666666665 + 2.0
    dyn_key = lot_number[26:30] + lot_number[7:11]
    dyn_val = lot_number[6:14]
    plaintext = (
        '{"setLeft":%d,"passtime":1887,"userresponse":%s,"device_id":"",'
        '"lot_number":"%s","pow_msg":"%s","pow_sign":"%s","geetest":"captcha",'
        '"lang":"zh","ep":"123","biht":"1426265548","gee_guard":{"roe":{'
        '"aup":"3","sep":"3","egp":"3","auh":"3","rew":"3","snh":"3",'
        '"res":"3","cdc":"3"}},"YciC":"P3Vn","%s":"%s","em":{'
        '"ph":0,"cp":0,"ek":"11","wd":1,"nt":0,"si":0,"sc":0}}'
    ) % (set_left, repr(userresponse), lot_number, pow_msg, pow_sign,
         dyn_key, dyn_val)
    return _aes_hex(plaintext, str16) + _rsa_encrypt_hex(str16.encode())


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

def _get(url: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={'User-Agent': UA_WEB})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _jsonp(text: str) -> dict:
    start, end = text.find('('), text.rfind(')')
    if start < 0 or end <= start:
        raise ValueError('JSONP malformed: %s' % text[:120])
    return json.loads(text[start + 1:end])


# --------------------------------------------------------------------------
# main entry
# --------------------------------------------------------------------------

def solve_gt4(captcha_id: str | None = None, attempts: int = 3,
              log=None) -> dict:
    """Return {'lotNumber','captchaOutput','passToken','genTime'}."""
    log = log or (lambda *_: None)
    ids = [captcha_id] if captcha_id else CAPTCHA_ID_CANDIDATES
    last = None
    for cid in ids:
        for attempt in range(1, attempts + 1):
            try:
                return _solve_once(cid, log)
            except Exception as exc:  # noqa: BLE001
                last = exc
                log('[gt4] cid=%s attempt %d/%d failed: %s'
                    % (cid[:8], attempt, attempts, exc))
                time.sleep(2)
    raise RuntimeError('GT4 failed on all captcha ids: %s' % last)


def _solve_once(cid: str, log) -> dict:
    challenge = uuid.uuid4().hex
    cb = 'geetest_%d' % int(time.time() * 1000)
    load_url = ('%s/load?callback=%s&captcha_id=%s&challenge=%s'
                '&client_type=web&risk_type=slide&lang=zh'
                % (GEE_HOST, cb, cid, challenge))
    data = _jsonp(_get(load_url).decode())['data']

    for field in ('bg', 'slice', 'lot_number', 'payload',
                  'process_token', 'pow_detail'):
        if field not in data:
            raise ValueError('GT4 /load missing %s' % field)

    bg = _get(STATIC_HOST + data['bg'])
    sl = _get(STATIC_HOST + data['slice'])
    dist = gap_distance(bg, sl)
    log('[gt4] gap distance = %d px' % dist)

    pow_detail = data['pow_detail']
    pow_msg = _pow_msg(pow_detail, cid, data['lot_number'])
    pow_sign = hashlib.sha256(pow_msg.encode()).hexdigest()
    str16 = _str16()
    w = build_w(dist, data['lot_number'], pow_msg, pow_sign, str16)

    cb2 = 'geetest_%d' % int(time.time() * 1000)
    verify_url = ('%s/verify?callback=%s&captcha_id=%s&client_type=web'
                  '&lot_number=%s&risk_type=slide&payload=%s'
                  '&process_token=%s&payload_protocol=1&pt=1&w=%s'
                  % (GEE_HOST, cb2, cid,
                     urllib.parse.quote(data['lot_number']),
                     urllib.parse.quote(data['payload']),
                     urllib.parse.quote(data['process_token']),
                     urllib.parse.quote(w)))
    root = _jsonp(_get(verify_url).decode())
    if root.get('status') != 'success':
        raise RuntimeError('GT4 /verify failed: %s'
                           % json.dumps(root, ensure_ascii=False)[:200])

    sec = root['data']['seccode']
    return {
        'lotNumber': sec['lot_number'],
        'captchaOutput': sec['captcha_output'],
        'passToken': sec['pass_token'],
        'genTime': sec['gen_time'],
    }


# --------------------------------------------------------------------------
# offline self-check
# --------------------------------------------------------------------------

def self_test() -> list[tuple[str, bool, str]]:
    out = []

    # w plaintext shape (no network, fixed lot_number)
    lot = 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6'
    s16 = '0123456789abcdef'
    w = build_w(123, lot, 'pow|msg||nonce1234567890ab', 'sign', s16)
    out.append(('w is hex', all(c in string.hexdigits.lower() for c in w), '%d chars' % len(w)))
    out.append(('w longer than RSA blob', len(w) > 260, '%d chars' % len(w)))

    # dynamic key derivation
    dyn = lot[26:30] + lot[7:11]
    out.append(('dynamic key slice', len(dyn) == 8, dyn))

    # RSA modulus size (37 jsbn limbs of 28 bits -> 1024-bit key, 128 bytes)
    bits = _gee_rsa_n().bit_length()
    out.append(('GeeTest RSA 1024 bit', bits == 1024, '%d bits' % bits))

    # pow settles quickly
    msg = _pow_msg({'version': 'v1', 'bits': 5, 'hashfunc': 'sha256',
                    'datetime': '2026-09-09T00:00:00'}, 'cid', 'lot')
    out.append(('pow_msg prefix', msg.startswith('v1|5|sha256|2026-09-09T00:00:00|cid|lot||'), msg[:40]))
    out.append(('pow_msg tail is 16 hex', len(msg.rsplit('|', 1)[-1]) == 16, msg[-16:]))

    # image pipeline on a synthetic pair: crop a slice from a known x and check
    # the matcher recovers exactly that x.
    bg = np.zeros((60, 200), np.uint8)
    bg[:] = 40
    cv2.rectangle(bg, (120, 20), (160, 55), 220, -1)
    cut_x = 100
    slice_img = bg[20:56, cut_x:cut_x + 41].copy()
    ok, bg_png = cv2.imencode('.png', bg)
    ok2, sl_png = cv2.imencode('.png', slice_img)
    dist = gap_distance(bg_png.tobytes(), sl_png.tobytes())
    out.append(('image gap localised', abs(dist - cut_x) <= 3,
                'x=%d (want %d)' % (dist, cut_x)))
    return out


if __name__ == '__main__':
    ok = True
    for name, passed, detail in self_test():
        print('%-28s %s   %s' % (name, 'PASS' if passed else 'FAIL', detail))
        ok = ok and passed
    print('\nRESULT:', 'PASS' if ok else 'FAIL')
    raise SystemExit(0 if ok else 1)

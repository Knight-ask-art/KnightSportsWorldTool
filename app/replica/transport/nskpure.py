"""Pure-Python NetSecKit envelope codec — no unidbg / JVM / native lib.

Ported from NekoSportsWorldTool `src/crypto/envelope.rs` and `src/crypto/decrypt.rs`
(the algorithm was re-derived there from the iOS 7.3.40 request chain).

Why this exists
---------------
Our original implementation drove the app's real ARM library inside unidbg to
obtain the session key. That works, but costs 330 MB of runtime (oracle.apk +
JRE + jars), requires a JVM on the host, and has a nasty ordering rule (the KEY
must be read right after the most recent encryption). The algorithm turns out to
be fully reproducible in ~150 lines, so this module replaces the sidecar.

Request envelope
----------------
    keyDataOne   = "nhang.school"          # last 12 of "com.wanhang.school"
    keyDataTwo   = "5K0E8400-E29"          # first 12 of "5K0E8400-E29B-..."
    keyDataThree = "597DEA1AFB49"          # last 12 of "DE6AED50-2B3A-..."
    keyDataFour  = last12(f"{now_ms:.6f}") # latched once per session

    container = {"data": b64(plain), "timeStamp": ts, "platform": 1,
                 "keyDataOne": .., "keyDataTwo": ..,
                 "keyDataThree": .., "keyDataFour": ..}     # insertion order
    d = b64( AES-128-CBC(reqKey, IV=0, PKCS7(container)) )
    h = md5(container)
    k = b64( RSA-1024 PKCS1v15(reqKey) )                    # 128 bytes

    body       -> {"d":d,"h":h,"k":k,"p":101,"t":0}
    headerSign -> {"k":k,"p":101,"d":d,"h":h,"t":0}

Response envelope
-----------------
    paesKey = derive_paes_key(four keyData)      # 16 bytes, little-endian
    {"r","s","v":101}
      s -> RSA public raw op -> PKCS#1 type-1 block -> 32 lowercase hex chars
      r -> b64 -> AES-128-CBC(paesKey) -> layer1
      md5(layer1) must equal the recovered digest   (fail-closed)
      layer1 is the inner envelope; if it carries "d", AES it again with paesKey
      business = b64decode(outer["data"]) -> JSON
"""
from __future__ import annotations

import base64
import hashlib
import json
import random
import time
from dataclasses import dataclass

from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad, unpad

# --------------------------------------------------------------------------
# constants (identical to the reference implementation)
# --------------------------------------------------------------------------

KEYDATA_ONE = 'nhang.school'
KEYDATA_TWO = '5K0E8400-E29'
KEYDATA_THREE = '597DEA1AFB49'

# native reqKey character pool (the duplicated '+' is intentional)
SALT_ALPHABET = ('+kot8A*B45jF6CD@a!UVWubcdKLZ{efgMpNOxyz01PQ}'
                 'Rn)Tvw23XYh(iG7rsEqJHI9+Slm/')

# client-embedded RSA-1024 public key (SPKI PEM)
RSA_PUB_PEM = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC5pqlTzsGNZk1RxhH4O4x3JNTD
V7FbVH66mPfW5v1tnIy4ty7xv8DGMG4Zn/TvstwlJWeYOADHdi8uF21lJaBvzvPt
VEhifHXZq825fI9hGYtDoaVQmCN/Nfs2dKmt89XDrhtl3SZxO6TumOCTQt+5oqjF
2Jo3o1YtkAyGzjaJnwIDAQAB
-----END PUBLIC KEY-----
"""

IOS_UA = 'SWCampus/7.3.40 (iPhone; iOS 18.1; Scale/3.00)'
ANDROID_UA = ('Mozilla/5.0 (Linux; Android 14; 22081212C Build/UKQ1.231003.002) '
              'AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 '
              'Chrome/110.0.5481.154 Mobile Safari/537.36')


class NskError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------

def now_ms() -> int:
    return int(time.time() * 1000)


def md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode()


def b64d(text: str) -> bytes:
    return base64.b64decode(text)


def _c_string_bytes(value: str) -> bytes:
    """UTF8String + strlen semantics: stop at the first NUL."""
    raw = value.encode('utf-8')
    nul = raw.find(b'\x00')
    return raw if nul < 0 else raw[:nul]


def fold32(value: str) -> int:
    """0x103f5684c: h = (((h & 0xffffff) << 8) | c) ^ (h >> 24)."""
    h = 0
    for c in _c_string_bytes(value):
        h = ((((h & 0x00FFFFFF) << 8) | c) ^ (h >> 24)) & 0xFFFFFFFF
    return h


def _ror32(value: int, bits: int) -> int:
    value &= 0xFFFFFFFF
    return ((value >> bits) | (value << (32 - bits))) & 0xFFFFFFFF


def _le(value: int) -> bytes:
    return (value & 0xFFFFFFFF).to_bytes(4, 'little')


def derive_paes_key(one: str, two: str, three: str, four: str) -> bytes:
    """0x103f5689c MBA mix -> 16-byte little-endian AES key."""
    for v in (one, two, three, four):
        n = len(_c_string_bytes(v))
        if not 8 <= n <= 12:
            raise NskError(f'keyData must be 8..12 UTF-8 bytes, got {n}: {v!r}')
    a, b, c, d = fold32(one), fold32(two), fold32(three), fold32(four)

    w9 = c ^ a
    w10 = w9 ^ _ror32(w9, 24)
    w9 = w10 ^ _ror32(w9, 8)
    w10 = w9 ^ b
    w9 ^= d
    w8 = d ^ b
    w11 = w8 ^ _ror32(w8, 24)
    w8 = w11 ^ _ror32(w8, 8)
    w11 = w8 ^ a
    w8 ^= c

    w12 = w8 & w10
    w11 ^= w12
    w12 = w8 | w9
    w8 ^= w9
    w10 ^= w12
    w8 = w10 ^ (~w8 & 0xFFFFFFFF)
    w12 = w8 ^ w11
    w8 |= w11
    w8 ^= w10
    w10 = w12 & (~w10 & 0xFFFFFFFF)
    w9 ^= w10

    w8 &= 0xFFFFFFFF
    w9 &= 0xFFFFFFFF
    w10 &= 0xFFFFFFFF
    w11 &= 0xFFFFFFFF
    w12 &= 0xFFFFFFFF
    return _le(w9) + _le(w8) + _le(w12) + _le(w11)


def aes_cbc_encrypt(key: bytes, plaintext: bytes, iv: bytes | None = None) -> bytes:
    if len(key) != 16:
        raise NskError(f'AES key must be 16 bytes, got {len(key)}')
    return AES.new(key, AES.MODE_CBC, iv or b'\x00' * 16).encrypt(pad(plaintext, 16))


def aes_cbc_decrypt(key: bytes, ciphertext: bytes, iv: bytes | None = None) -> bytes:
    if len(key) != 16:
        raise NskError(f'AES key must be 16 bytes, got {len(key)}')
    try:
        return unpad(AES.new(key, AES.MODE_CBC, iv or b'\x00' * 16).decrypt(ciphertext), 16)
    except ValueError as exc:
        raise NskError(f'AES decrypt/PKCS7 failed: {exc}') from exc


def random_fragment(n: int) -> str:
    return ''.join(random.choice(SALT_ALPHABET) for _ in range(n))


def req_key() -> str:
    return random_fragment(16)


def normalize_raw_key_data(value: str) -> str:
    """<=7B pad with random alnum to 12; >=13B take last 12 UTF-8 bytes."""
    raw = value.encode('utf-8')
    if len(raw) <= 7:
        return value + random_fragment(12 - len(raw))
    if len(raw) >= 13:
        return raw[-12:].decode('utf-8', 'replace')
    return value


def key_data_four_from_ms(ms: float) -> str:
    return normalize_raw_key_data(f'{ms:.6f}')


# --------------------------------------------------------------------------
# RSA
# --------------------------------------------------------------------------

_pub_cache = None


def rsa_public_key():
    """Embedded client public key (cached)."""
    global _pub_cache
    if _pub_cache is None:
        _pub_cache = RSA.import_key(RSA_PUB_PEM)
    return _pub_cache


def rsa_encrypt_req_key(rk: str) -> str:
    cipher = PKCS1_v1_5.new(rsa_public_key())
    return b64e(cipher.encrypt(rk.encode()))


def rsa_recovered_digest(s_b64: str, pub=None) -> str:
    """Recover the 32 lowercase hex chars packed into response.s (PKCS#1 type-1)."""
    pub = pub or rsa_public_key()
    sig = b64d(s_b64)
    n = pub.n
    size = (n.bit_length() + 7) // 8
    if len(sig) != size:
        raise NskError(f's length {len(sig)} != modulus {size}')
    m = int.from_bytes(sig, 'big')
    em = pow(m, pub.e, n).to_bytes(size, 'big')
    if len(em) < 2 or em[0] != 0x00 or em[1] != 0x01:
        raise NskError('s is not a PKCS#1 type-1 block')
    try:
        marker = em.index(b'\x00', 2)
    except ValueError as exc:
        raise NskError('s has no PKCS#1 separator') from exc
    if marker < 10 or any(b != 0xFF for b in em[2:marker]):
        raise NskError('s has invalid PKCS#1 type-1 padding')
    digest = em[marker + 1:]
    if len(digest) != 32:
        raise NskError(f's tail is not 32 bytes, got {len(digest)}')
    text = digest.decode('ascii', 'replace')
    if not all(ch in '0123456789abcdef' for ch in text):
        raise NskError('s tail is not lowercase hex')
    return text


# --------------------------------------------------------------------------
# request envelope
# --------------------------------------------------------------------------

BODY_KEYS_INSERT = ('d', 'h', 'k', 'p', 't')
HEADER_KEYS_OBSERVED = ('k', 'p', 'd', 'h', 't')


@dataclass
class BuiltEnvelope:
    json: str           # outer envelope, for the body or for headerSign
    container: bytes    # inner container bytes (input of `h`)
    req_key: str
    ts_ms: int
    key_data: tuple     # (one, two, three, four)


class EnvelopeSession:
    """Mirrors the NTESSecurityKit singleton: keyDataFour is latched once."""

    def __init__(self, four: str | None = None, ts_ms: int | None = None):
        if four is None:
            four = key_data_four_from_ms(float(ts_ms if ts_ms is not None else now_ms()))
        self.four = four

    def key_data(self) -> tuple:
        return (KEYDATA_ONE, KEYDATA_TWO, KEYDATA_THREE, self.four)

    def paes_key(self) -> bytes:
        return derive_paes_key(*self.key_data())

    def rotate(self, ts_ms: int | None = None):
        self.four = key_data_four_from_ms(float(ts_ms if ts_ms is not None else now_ms()))


def serialize_container(data_b64: str, ts_ms: int, key_data: tuple) -> bytes:
    # insertion order matters: serde_json runs with `preserve_order` in the
    # reference implementation, so the bytes are exactly this sequence.
    return json.dumps(
        {
            'data': data_b64,
            'timeStamp': ts_ms,
            'platform': 1,
            'keyDataOne': key_data[0],
            'keyDataTwo': key_data[1],
            'keyDataThree': key_data[2],
            'keyDataFour': key_data[3],
        },
        separators=(',', ':'),
        ensure_ascii=False,
    ).encode('utf-8')


def build_envelope(session: EnvelopeSession, plain: str,
                   order: str = 'insert', ts_ms: int | None = None) -> BuiltEnvelope:
    ts_ms = now_ms() if ts_ms is None else ts_ms
    key_data = session.key_data()
    container = serialize_container(b64e(plain.encode('utf-8')), ts_ms, key_data)
    rk = req_key()
    d = b64e(aes_cbc_encrypt(rk.encode(), container))
    h = md5_hex(container)
    k = rsa_encrypt_req_key(rk)
    if order == 'observed':
        body = ('{"k":"%s","p":101,"d":"%s","h":"%s","t":0}' % (k, d, h))
    else:
        body = ('{"d":"%s","h":"%s","k":"%s","p":101,"t":0}' % (d, h, k))
    return BuiltEnvelope(json=body, container=container, req_key=rk,
                         ts_ms=ts_ms, key_data=key_data)


def build_header_sign(session: EnvelopeSession, headers_json: str,
                      ts_ms: int | None = None) -> str:
    """headerSign is the observed-order envelope of the header plaintext."""
    return build_envelope(session, headers_json, order='observed', ts_ms=ts_ms).json


# --------------------------------------------------------------------------
# response envelope
# --------------------------------------------------------------------------

@dataclass
class Decrypted:
    plaintext: str
    business: object
    expected_md5: str = ''
    actual_md5: str = ''


def decrypt_response(raw: bytes | str, key: bytes, pub=None) -> Decrypted:
    """Decrypt one server response. Fails closed on a signature mismatch."""
    text = raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else raw
    try:
        obj = json.loads(text.strip())
    except Exception as exc:
        raise NskError(f'response is not JSON: {exc}') from exc

    # Some endpoints wrap the envelope in {"resp": "<json string>"}
    resp = obj.get('resp') if isinstance(obj, dict) else None
    if isinstance(resp, str):
        obj = json.loads(resp)
    elif resp is not None:
        obj = resp

    if not (isinstance(obj, dict) and all(k in obj for k in ('r', 's', 'v'))):
        # plaintext business response (gateway errors, 404 pages, ...)
        return Decrypted(plaintext=text, business=obj)

    version = obj['v']
    if str(version) != '101':
        raise NskError(f'response version {version} != 101')

    expected = rsa_recovered_digest(obj['s'], pub)
    layer1 = aes_cbc_decrypt(key, b64d(obj['r']))
    actual = md5_hex(layer1)
    if actual != expected:
        raise NskError(f'response signature mismatch: {actual} != {expected}')

    plaintext = layer1.decode('utf-8', 'replace')
    try:
        env = json.loads(plaintext)
    except Exception as exc:
        raise NskError(f'inner envelope is not JSON: {exc}') from exc

    inner_d = env.get('d') if isinstance(env, dict) else None
    if isinstance(inner_d, str):
        layer2 = aes_cbc_decrypt(key, b64d(inner_d))
        outer = json.loads(layer2)
    else:
        outer = env

    data = outer.get('data') if isinstance(outer, dict) else None
    if isinstance(data, str):
        try:
            business = json.loads(b64d(data))
        except Exception as exc:
            raise NskError(f'business data is not base64(JSON): {exc}') from exc
    else:
        business = outer

    return Decrypted(plaintext=plaintext, business=business,
                     expected_md5=expected, actual_md5=actual)


def decrypt_payload(raw: bytes | str, key: bytes, pub=None) -> object:
    """Convenience: decrypt and return only the business JSON."""
    return decrypt_response(raw, key, pub).business


# --------------------------------------------------------------------------
# self-test (run: python -m replica.nskpure)
# --------------------------------------------------------------------------

def _self_test() -> int:
    ok = 0
    bad = 0

    def check(name, got, want):
        nonlocal ok, bad
        if got == want:
            ok += 1
            print(f'  PASS {name}')
        else:
            bad += 1
            print(f'  FAIL {name}: got {got!r} want {want!r}')

    # vectors published by the reference implementation
    check('fold32(one)', f'{fold32(KEYDATA_ONE):08x}', '61297d61')
    check('fold32(two)', f'{fold32(KEYDATA_TWO):08x}', '203a324c')
    check('fold32(three)', f'{fold32(KEYDATA_THREE):08x}', '363a323c')
    check('fold32(four)', f'{fold32("86123.456789"):08x}', '3d2f3d3e')
    key = derive_paes_key(KEYDATA_ONE, KEYDATA_TWO, KEYDATA_THREE, '86123.456789')
    check('derive_paes_key', key.hex(), 'faaed5a4d99af386a7d3f5d109071f13')

    # round-trip: build an envelope, decrypt it with its own reqKey
    sess = EnvelopeSession(four='86123.456789')
    built = build_envelope(sess, '{"hello":"世界"}', order='insert')
    env = json.loads(built.json)
    check('envelope keys', tuple(env.keys()), BODY_KEYS_INSERT)
    check('envelope p', env['p'], 101)
    layer = aes_cbc_decrypt(built.req_key.encode(), b64d(env['d']))
    check('container round-trip', layer, built.container)
    check('h == md5(container)', env['h'], md5_hex(built.container))
    check('k is 128 bytes', len(b64d(env['k'])), 128)
    check('session paes key', sess.paes_key().hex(), key.hex())

    # observed order is what headerSign uses
    obs = json.loads(build_envelope(sess, '{}', order='observed').json)
    check('observed keys', tuple(obs.keys()), HEADER_KEYS_OBSERVED)

    print(f'\n{ok} passed, {bad} failed')
    return 0 if bad == 0 else 1


if __name__ == '__main__':
    raise SystemExit(_self_test())

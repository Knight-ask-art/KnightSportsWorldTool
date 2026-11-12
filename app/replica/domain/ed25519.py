"""Ed25519 signatures (RFC 8032), pure Python.

Two callers only: ``scripts/manifest_sign.py`` signs ``runtime/integrity.json``
at packaging time, and ``replica.application.integrity`` verifies it at login.
There is no general-purpose crypto API here and no key handling: the callers
own the key files.

The implementation follows the RFC 8032 reference (extended Edwards
coordinates).  It is not constant-time, which is acceptable because the
verification key and the messages are public, and signing happens once on the
maintainer's machine.  Never use it to sign attacker-chosen data in a loop.
"""
from __future__ import annotations

import hashlib

_P = 2 ** 255 - 19
_Q = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)

#: A neutral group element.
_ZERO = (0, 1, 1, 0)


def _sha512_int(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), 'little')


def _inv(x: int) -> int:
    return pow(x, _P - 2, _P)


def _recover_x(y: int) -> int:
    xx = (y * y - 1) * _inv(_D * y * y + 1) % _P
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = x * _I % _P
    if x & 1:
        x = _P - x
    return x


def _on_curve(point) -> bool:
    x, y, z, t = point
    return (y * y - x * x - z * z - _D * t * t) % _P == 0


def _encode(point) -> bytes:
    x, y, z, _t = point
    zi = _inv(z)
    x = x * zi % _P
    y = y * zi % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, 'little')


def _decode(raw: bytes):
    if len(raw) != 32:
        return None
    value = int.from_bytes(raw, 'little')
    y = value & ((1 << 255) - 1)
    if y >= _P:
        return None
    sign = (value >> 255) & 1
    x = _recover_x(y)
    # RFC 8032 5.1.3: x = 0 with the sign bit set is not a canonical encoding
    # of the identity; accepting it would make (R=identity, s=0) verify for a
    # small-order key.
    if x == 0 and sign:
        return None
    if x & 1 != sign:
        x = _P - x
    point = (x, y, 1, x * y % _P)
    return point if _on_curve(point) else None


def _is_identity(point) -> bool:
    x, y, z, _t = point
    return x % _P == 0 and (y - z) % _P == 0


def _add(p, q):
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(scalar: int, point):
    result = _ZERO
    while scalar > 0:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _base_point():
    y = 4 * _inv(5) % _P
    x = _recover_x(y)
    return (x, y, 1, x * y % _P)


_BASE = _base_point()


def _expand(seed: bytes):
    digest = hashlib.sha512(seed).digest()
    scalar = int.from_bytes(digest[:32], 'little')
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    return scalar, digest[32:]


def public_key(seed: bytes) -> bytes:
    """``seed`` is the 32-byte private key; the result is the 32-byte public key."""
    if len(seed) != 32:
        raise ValueError('seed must be 32 bytes')
    scalar, _prefix = _expand(seed)
    return _encode(_mul(scalar, _BASE))


def sign(seed: bytes, message: bytes) -> bytes:
    if len(seed) != 32:
        raise ValueError('seed must be 32 bytes')
    scalar, prefix = _expand(seed)
    public = _encode(_mul(scalar, _BASE))
    nonce = _sha512_int(prefix + message) % _Q
    r = _encode(_mul(nonce, _BASE))
    challenge = _sha512_int(r + public + message) % _Q
    s = (nonce + challenge * scalar) % _Q
    return r + int.to_bytes(s, 32, 'little')


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """True only for a canonical signature by ``public`` over ``message``."""
    if not isinstance(public, (bytes, bytearray)):
        return False
    if not isinstance(message, (bytes, bytearray)):
        return False
    if not isinstance(signature, (bytes, bytearray)):
        return False
    if len(public) != 32 or len(signature) != 64:
        return False
    a = _decode(public)
    r = _decode(signature[:32])
    if a is None or r is None:
        return False
    # A key outside the prime-order subgroup (identity, order 2/4/8) makes
    # (R=identity, s=0) a valid signature for every message.  A valid key is
    # non-identity and satisfies [L]A = identity; both are checked here.
    if _is_identity(a) or not _is_identity(_mul(_Q, a)):
        return False
    s = int.from_bytes(signature[32:], 'little')
    if s >= _Q:
        return False
    challenge = _sha512_int(signature[:32] + public + message) % _Q
    left = _mul(s, _BASE)
    right = _add(r, _mul(challenge, a))
    # Compare the affine x/y without normalising: both sides are projective.
    return (left[0] * right[2] - right[0] * left[2]) % _P == 0 and \
           (left[1] * right[2] - right[1] * left[2]) % _P == 0

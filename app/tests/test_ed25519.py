"""Ed25519 against the RFC 8032 test vectors.

The manifest signature is only worth anything if this primitive is correct, so
the vectors are checked byte for byte rather than round-tripping our own
output.
"""
from __future__ import annotations

import pytest

from replica.domain import ed25519

# (seed, public, message, signature) from RFC 8032 section 7.1.
VECTORS = [
    (
        '9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60',
        'd75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a',
        '',
        'e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155'
        '5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b',
    ),
    (
        '4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb',
        '3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c',
        '72',
        '92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da'
        '085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00',
    ),
    (
        'c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7',
        'fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025',
        'af82',
        '6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac'
        '18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a',
    ),
]


@pytest.mark.parametrize('seed,pub,msg,sig', VECTORS)
def test_public_key_and_signature_match_rfc8032(seed, pub, msg, sig):
    raw_seed = bytes.fromhex(seed)
    message = bytes.fromhex(msg)
    assert ed25519.public_key(raw_seed).hex() == pub
    assert ed25519.sign(raw_seed, message).hex() == sig
    assert ed25519.verify(bytes.fromhex(pub), message,
                          bytes.fromhex(sig)) is True


def test_a_single_flipped_bit_is_rejected():
    seed = bytes.fromhex(VECTORS[0][0])
    public = bytes.fromhex(VECTORS[0][1])
    signature = bytearray(bytes.fromhex(VECTORS[0][3]))

    bad_sig = bytes(signature[:63] + bytes([signature[63] ^ 1]))
    assert ed25519.verify(public, b'', bad_sig) is False
    assert ed25519.verify(public, b'x', bytes(signature)) is False

    bad_key = bytes([public[0] ^ 1]) + public[1:]
    assert ed25519.verify(bad_key, b'', bytes(signature)) is False


def test_non_canonical_scalars_and_lengths_are_rejected():
    public = bytes.fromhex(VECTORS[0][1])
    signature = bytes.fromhex(VECTORS[0][3])
    assert ed25519.verify(public, b'', signature[:-1]) is False
    assert ed25519.verify(public[:-1], b'', signature) is False
    assert ed25519.verify(public, b'', b'\xff' * 64) is False


def test_a_scalar_at_or_above_the_group_order_is_rejected():
    # Adding L to a valid s leaves the same point but must be refused; the
    # generic 0xff*sig test above dies in R decoding and never reaches this.
    public = bytes.fromhex(VECTORS[0][1])
    signature = bytes.fromhex(VECTORS[0][3])
    s = int.from_bytes(signature[32:], 'little') + ed25519._Q
    bad = signature[:32] + s.to_bytes(32, 'little')
    assert ed25519.verify(public, b'', bad) is False


def test_the_non_canonical_identity_encoding_is_rejected():
    # x = 0 with the sign bit set (RFC 8032 5.1.3 must reject it).
    raw = (1 | (1 << 255)).to_bytes(32, 'little')
    assert ed25519._decode(raw) is None


def test_a_small_order_public_key_cannot_verify_forgeries():
    identity = (1).to_bytes(32, 'little')
    forged = identity + bytes(32)
    assert ed25519.verify(identity, b'x', forged) is False
    # Order-2 point: y = -1.
    order2 = (ed25519._P - 1).to_bytes(32, 'little')
    assert ed25519.verify(order2, b'hello', forged) is False


def test_wrong_types_return_false_instead_of_raising():
    public = bytes.fromhex(VECTORS[0][1])
    signature = bytes.fromhex(VECTORS[0][3])
    assert ed25519.verify('a' * 32, b'', signature) is False
    assert ed25519.verify(public, 'text', signature) is False
    assert ed25519.verify(public, b'', ['x'] * 64) is False


def test_bad_seed_lengths_raise():
    with pytest.raises(ValueError):
        ed25519.public_key(b'short')
    with pytest.raises(ValueError):
        ed25519.sign(b'short', b'msg')

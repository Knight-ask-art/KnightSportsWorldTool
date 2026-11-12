"""Tests for Ed25519 manifest signature primitives and CLI signing."""

from __future__ import annotations

import base64
import copy
import json
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

from replica.application import update_signature as sig

ROOT = Path(__file__).resolve().parents[1]
PACKAGING_DIR = str(ROOT / 'packaging')
if PACKAGING_DIR not in sys.path:
    sys.path.insert(0, PACKAGING_DIR)

import make_latest


def _sample_manifest() -> dict:
    return {
        'schema': 1,
        'app': 'haoxiangpao',
        'name': '好想跑',
        'channel': 'stable',
        'version': '0.2.3',
        'min_version': '0.1.4',
        'released_at': '2026-11-12T12:00:00+08:00',
        'notes': ['修复若干问题'],
        'assets': {
            'windows-x64': {
                'file': 'haoxiangpao_v0.2.3_win64.zip',
                'url': 'haoxiangpao_v0.2.3_win64.zip',
                'size': 4,
                'sha256': 'a' * 64,
                'format': 'zip',
                'layout': 'onedir',
                'entry': '好想跑.exe',
                'version': '0.2.3',
            }
        },
        'generated_by': 'packaging/make_latest.py',
    }


def _gen_keypair():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    return priv, pub


def _raw_pub(pub) -> bytes:
    return pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _raw_priv(priv) -> bytes:
    return priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _pem_priv(priv) -> bytes:
    return priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def test_canonical_payload_is_stable_across_insertion_order_and_excludes_signature():
    m1 = {
        'version': '0.2.3',
        'assets': {'windows-x64': {'sha256': 'a' * 64, 'url': 'a.zip'}},
        'name': '好想跑',
        'signature': {'alg': 'ed25519', 'key_id': 'release', 'value': 'ignored'},
    }
    m2 = {
        'name': '好想跑',
        'assets': {'windows-x64': {'url': 'a.zip', 'sha256': 'a' * 64}},
        'version': '0.2.3',
    }

    p1 = sig.canonical_payload(m1)
    p2 = sig.canonical_payload(m2)
    assert p1 == p2
    assert b'signature' not in p1
    assert '好想跑'.encode() in p1


def test_canonical_payload_rejects_lone_surrogate():
    with pytest.raises(sig.ManifestSignatureError):
        sig.canonical_payload({'bad': '\ud800'})


def test_sign_rejects_lone_surrogate_key_id():
    with pytest.raises(sig.ManifestSignatureError):
        sig.sign_manifest({'schema': 1}, Ed25519PrivateKey.generate(), '\ud800')


def test_sign_and_verify_round_trip_with_key_coercion():
    priv, pub = _gen_keypair()
    manifest = _sample_manifest()
    signed = sig.sign_manifest(manifest, priv, 'release')

    assert signed['signature']['alg'] == 'ed25519'
    assert signed['signature']['key_id'] == 'release'

    raw_pub = _raw_pub(pub)
    b64_pub = base64.b64encode(raw_pub).decode('ascii')

    for pub_variant in (pub, raw_pub, b64_pub):
        result = sig.verify_manifest(signed, {'release': pub_variant})
        assert result is signed

    # Raw 32-byte private seed also works.
    signed_from_seed = sig.sign_manifest(manifest, _raw_priv(priv), 'release')
    assert sig.verify_manifest(signed_from_seed, {'release': raw_pub}) is signed_from_seed


def test_tampering_version_or_assets_fails():
    priv, pub = _gen_keypair()
    signed = sig.sign_manifest(_sample_manifest(), priv, 'release')
    keys = {'release': pub}

    bad_version = copy.deepcopy(signed)
    bad_version['version'] = '9.9.9'
    with pytest.raises(sig.ManifestSignatureError):
        sig.verify_manifest(bad_version, keys)

    bad_assets = copy.deepcopy(signed)
    bad_assets['assets']['windows-x64']['sha256'] = 'b' * 64
    with pytest.raises(sig.ManifestSignatureError):
        sig.verify_manifest(bad_assets, keys)


def test_wrong_key_fails():
    priv, _ = _gen_keypair()
    _, other_pub = _gen_keypair()
    signed = sig.sign_manifest(_sample_manifest(), priv, 'release')

    with pytest.raises(sig.ManifestSignatureError):
        sig.verify_manifest(signed, {'release': other_pub})


def test_unknown_key_id_and_swapped_key_id_fail():
    priv, pub = _gen_keypair()
    signed = sig.sign_manifest(_sample_manifest(), priv, 'release')

    with pytest.raises(sig.ManifestSignatureError):
        sig.verify_manifest(signed, {'other': pub})

    # Public keys default to intentionally empty PUBLIC_KEYS in source.
    with pytest.raises(sig.ManifestSignatureError):
        sig.verify_manifest(signed)

    # key_id is bound to the signed payload even if two ids map to the same key.
    swapped = copy.deepcopy(signed)
    swapped['signature']['key_id'] = 'backup'
    with pytest.raises(sig.ManifestSignatureError):
        sig.verify_manifest(swapped, {'release': pub, 'backup': pub})


def test_missing_signature_respects_required_flag():
    manifest = _sample_manifest()

    with pytest.raises(sig.ManifestSignatureError):
        sig.verify_manifest(manifest, {}, required=True)

    assert sig.verify_manifest(manifest, {}, required=False) is manifest


def test_invalid_alg_and_malformed_fields_fail():
    priv, pub = _gen_keypair()
    signed = sig.sign_manifest(_sample_manifest(), priv, 'release')
    keys = {'release': pub}

    bad_alg = copy.deepcopy(signed)
    bad_alg['signature']['alg'] = 'rsa'
    with pytest.raises(sig.ManifestSignatureError):
        sig.verify_manifest(bad_alg, keys)

    for field in ('key_id', 'value'):
        empty_field = copy.deepcopy(signed)
        empty_field['signature'][field] = ''
        with pytest.raises(sig.ManifestSignatureError):
            sig.verify_manifest(empty_field, keys)

        missing_field = copy.deepcopy(signed)
        del missing_field['signature'][field]
        with pytest.raises(sig.ManifestSignatureError):
            sig.verify_manifest(missing_field, keys)

    bad_b64 = copy.deepcopy(signed)
    bad_b64['signature']['value'] = 'not!valid!base64!'
    with pytest.raises(sig.ManifestSignatureError):
        sig.verify_manifest(bad_b64, keys)


def test_key_coercion_rejects_invalid_keys():
    priv, _ = _gen_keypair()
    signed = sig.sign_manifest(_sample_manifest(), priv, 'release')

    with pytest.raises(sig.ManifestSignatureError):
        sig.sign_manifest(_sample_manifest(), b'short-seed', 'release')

    for bad_pub in (b'short', b'x' * 33, 'not!b64!', base64.b64encode(b'x' * 31).decode()):
        with pytest.raises(sig.ManifestSignatureError):
            sig.verify_manifest(signed, {'release': bad_pub})


def test_sign_manifest_does_not_mutate_input():
    priv, _ = _gen_keypair()
    original = _sample_manifest()
    snapshot = copy.deepcopy(original)

    signed = sig.sign_manifest(original, priv, 'release')
    assert original == snapshot
    assert 'signature' not in original
    assert 'signature' in signed


def test_cli_with_sign_key_writes_verifiable_manifest(tmp_path, capsys):
    priv, pub = _gen_keypair()
    zip_path = tmp_path / 'haoxiangpao_v0.2.3_win64.zip'
    zip_path.write_bytes(b'PK\x03\x04test')

    pem_bytes = _pem_priv(priv)
    key_path = tmp_path / 'release.pem'
    key_path.write_bytes(pem_bytes)
    out_path = tmp_path / 'latest.json'

    code = make_latest.main([
        '--dist', str(tmp_path),
        '--version', '0.2.3',
        '--zip', str(zip_path),
        '--out', str(out_path),
        '--rollback-version', '',
        '--sign-key', str(key_path),
    ])
    assert code == 0

    captured = capsys.readouterr()
    pem_text = pem_bytes.decode('ascii')
    assert 'PRIVATE KEY' not in captured.out + captured.err
    assert pem_text not in captured.out + captured.err

    manifest = json.loads(out_path.read_text(encoding='utf-8'))
    assert manifest['signature']['alg'] == 'ed25519'
    assert manifest['signature']['key_id'] == 'release'
    sig.verify_manifest(manifest, {'release': _raw_pub(pub)})


def test_cli_without_sign_key_stays_unsigned(tmp_path):
    zip_path = tmp_path / 'haoxiangpao_v0.2.3_win64.zip'
    zip_path.write_bytes(b'PK\x03\x04test')
    out_path = tmp_path / 'latest.json'

    code = make_latest.main([
        '--dist', str(tmp_path),
        '--version', '0.2.3',
        '--zip', str(zip_path),
        '--out', str(out_path),
        '--rollback-version', '',
    ])
    assert code == 0

    manifest = json.loads(out_path.read_text(encoding='utf-8'))
    assert 'signature' not in manifest


def test_cli_require_signature_without_key_exits_2(tmp_path, capsys):
    zip_path = tmp_path / 'haoxiangpao_v0.2.3_win64.zip'
    zip_path.write_bytes(b'PK\x03\x04test')
    out_path = tmp_path / 'latest.json'

    code = make_latest.main([
        '--dist', str(tmp_path),
        '--version', '0.2.3',
        '--zip', str(zip_path),
        '--out', str(out_path),
        '--rollback-version', '',
        '--require-signature',
    ])
    assert code == 2
    assert not out_path.exists()
    assert '--sign-key' in capsys.readouterr().err


def test_cli_rejects_non_ed25519_pem_key(tmp_path, capsys):
    zip_path = tmp_path / 'haoxiangpao_v0.2.3_win64.zip'
    zip_path.write_bytes(b'PK\x03\x04test')
    out_path = tmp_path / 'latest.json'

    rsa_key = generate_private_key(public_exponent=65537, key_size=2048)
    rsa_pem = rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path = tmp_path / 'rsa.pem'
    key_path.write_bytes(rsa_pem)

    code = make_latest.main([
        '--dist', str(tmp_path),
        '--version', '0.2.3',
        '--zip', str(zip_path),
        '--out', str(out_path),
        '--rollback-version', '',
        '--sign-key', str(key_path),
    ])
    assert code == 2
    assert not out_path.exists()
    assert 'PRIVATE KEY' not in capsys.readouterr().err

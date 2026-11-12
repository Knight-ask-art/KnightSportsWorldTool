"""``scripts/manifest_sign.py`` against temporary trees.

The signer lives outside the package, so it is loaded by path.  Nothing here
touches the real ``app/replica/application/integrity.py`` or the real key:
``_apply_pubkey`` runs against a copy under ``tmp_path``.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from replica.application import integrity as I
from replica.domain import ed25519

SIGNER = Path(__file__).resolve().parents[2] / 'scripts' / 'manifest_sign.py'


@pytest.fixture(scope='module')
def ms():
    spec = importlib.util.spec_from_file_location('manifest_sign_under_test',
                                                  SIGNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_apply_pubkey_preserves_crlf_and_other_lines(tmp_path, ms, monkeypatch):
    app = tmp_path / 'app'
    target = app / 'replica' / 'application' / 'integrity.py'
    target.parent.mkdir(parents=True)
    target.write_bytes(b"MANIFEST_PUBKEY = ''\r\nOTHER = 1\r\n")
    monkeypatch.setattr(ms, 'APP', app)

    pub = 'ab' * 32
    ms._apply_pubkey(pub)

    raw = target.read_bytes()
    assert raw == ("MANIFEST_PUBKEY = '%s'\r\nOTHER = 1\r\n" % pub).encode()


def test_write_keeps_unknown_top_level_fields(tmp_path, ms):
    path = tmp_path / 'integrity.json'
    ms._write(path, {'version': 2, 'files': {}, 'min_version': 5,
                     'sig': 'aa'})
    doc = json.loads(path.read_text(encoding='utf-8'))
    assert doc['min_version'] == 5
    assert doc['sig'] == 'aa'


def test_keygen_refuses_to_rotate_an_embedded_key(tmp_path, ms, monkeypatch):
    monkeypatch.setattr(ms, '_embedded_pubkey', lambda: 'ab' * 32)
    out = tmp_path / 'manifest.key'
    rc = ms.cmd_keygen(SimpleNamespace(out=str(out), force=False, apply=True))
    assert rc == 1
    assert not out.exists()


def _signed_install(tmp_path, seed):
    root = tmp_path / 'install'
    files = {}
    for name, rel in I.NATIVE_FILES:
        path = root / 'runtime' / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'stub-' + name.encode())
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    doc = {'version': I.MANIFEST_VERSION, 'files': files}
    doc['sig'] = ed25519.sign(seed, I.signing_bytes(doc)).hex()
    manifest = root / 'runtime' / 'integrity.json'
    manifest.write_text(json.dumps(doc), encoding='utf-8')
    return root, manifest


def test_verify_checks_signature_and_every_sidecar(tmp_path, ms, monkeypatch):
    seed = bytes.fromhex('33' * 32)
    monkeypatch.setattr(I, 'MANIFEST_PUBKEY', ed25519.public_key(seed).hex())
    root, manifest = _signed_install(tmp_path, seed)
    args = SimpleNamespace(manifest=str(manifest))

    assert ms.cmd_verify(args) == 0

    (root / 'runtime' / 'go' / 'hxp-guard.exe').unlink()
    assert ms.cmd_verify(args) == 1


def test_verify_rejects_a_tampered_digest(tmp_path, ms, monkeypatch):
    seed = bytes.fromhex('44' * 32)
    monkeypatch.setattr(I, 'MANIFEST_PUBKEY', ed25519.public_key(seed).hex())
    root, manifest = _signed_install(tmp_path, seed)
    doc = json.loads(manifest.read_text(encoding='utf-8'))
    doc['files']['hxp-guard.exe'] = 'a' * 64
    manifest.write_text(json.dumps(doc), encoding='utf-8')
    assert ms.cmd_verify(SimpleNamespace(manifest=str(manifest))) == 1


def test_read_seed_prefers_the_environment(monkeypatch, ms):
    seed = '55' * 32
    monkeypatch.setenv('JHP_MANIFEST_KEY', seed)
    assert ms._read_seed(None) == bytes.fromhex(seed)

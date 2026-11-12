"""Tests for the update manifest signing key helper."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts' / 'update_keygen.py'


def _load_module():
    spec = importlib.util.spec_from_file_location('update_keygen', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_generates_pem_and_refuses_accidental_overwrite(tmp_path, capsys):
    module = _load_module()
    key_path = tmp_path / 'update-signing.pem'

    assert module.main(['--path', str(key_path)]) == 0
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    assert isinstance(key, Ed25519PrivateKey)
    out = capsys.readouterr().out
    assert 'PRIVATE KEY' not in out
    assert 'release:' in out

    assert module.main(['--path', str(key_path)]) == 2
    err = capsys.readouterr().err
    assert 'already exists' in err

    assert module.main(['--path', str(key_path), '--force']) == 0

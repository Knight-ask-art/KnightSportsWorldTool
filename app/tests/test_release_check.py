"""Tests for the release-time update manifest signature check."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from replica.application import update_signature as sig

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts' / 'release_check.py'


def _load_module():
    spec = importlib.util.spec_from_file_location('release_check', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _manifest() -> dict:
    return {
        'schema': 1,
        'app': 'haoxiangpao',
        'name': '好想跑',
        'channel': 'stable',
        'version': '0.2.3',
        'min_version': '0.1.4',
        'released_at': '2026-11-12T12:00:00+08:00',
        'notes': ['test'],
        'assets': {
            'windows-x64': {
                'file': 'haoxiangpao_v0.2.3_win64.zip',
                'url': 'https://example.com/haoxiangpao_v0.2.3_win64.zip',
                'size': 4,
                'sha256': 'a' * 64,
                'format': 'zip',
                'layout': 'onedir',
                'entry': '好想跑.exe',
                'version': '0.2.3',
            },
        },
    }


def test_signed_manifest_passes_and_tamper_fails(tmp_path):
    module = _load_module()
    key = Ed25519PrivateKey.generate()
    key_path = tmp_path / 'update.pem'
    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    signed = sig.sign_manifest(_manifest(), key, 'release')

    report = module.Report()
    module.check_update_signature(report, signed, key_path)
    assert report.errors == []

    tampered = dict(signed)
    tampered['version'] = '9.9.9'
    report = module.Report()
    module.check_update_signature(report, tampered, key_path)
    assert 'update manifest verifies' in report.errors

    report = module.Report()
    module.check_update_signature(report, _manifest(), key_path)
    assert 'update manifest signature' in report.errors

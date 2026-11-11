"""Native integrity manifest: match, missing, mismatch, bad manifest.

All fixtures live under ``tmp_path``; nothing reads ``app/data`` and no native
binary is started.  The check is diagnostic only -- these tests assert that a
failure is reported, not that anything is blocked.
"""
from __future__ import annotations

import hashlib
import json
import os

from replica.application import integrity as I


def _write(path, blob: bytes) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as fh:
        fh.write(blob)
    return hashlib.sha256(blob).hexdigest()


def _install(tmp_path, blobs=None):
    """Create a packaged-looking tree and its manifest."""
    root = tmp_path / 'install'
    blobs = blobs or {
        'hxp-core.exe': b'core-binary',
        'hxp-guard.exe': b'guard-binary',
        'hxp-shield.exe': b'shield-binary',
    }
    rels = dict(I.NATIVE_FILES)
    digests = {}
    for name, blob in blobs.items():
        digests[name] = _write(str(root / 'runtime' / rels[name]), blob)
    manifest = root / 'runtime' / 'integrity.json'
    manifest.write_text(json.dumps({'version': 1, 'files': digests}),
                        encoding='utf-8')
    return root, manifest


def test_all_hashes_match(tmp_path):
    root, manifest = _install(tmp_path)
    out = I.check_native(manifest_path=str(manifest), root=str(root))
    assert out == {'ok': True, 'checked': 3, 'missing': [], 'mismatch': []}


def test_missing_sidecar_is_reported_not_raised(tmp_path):
    root, manifest = _install(tmp_path)
    os.remove(str(root / 'runtime' / 'rust' / 'hxp-core.exe'))
    out = I.check_native(manifest_path=str(manifest), root=str(root))
    assert out['ok'] is False
    assert out['checked'] == 2
    assert out['missing'] == ['hxp-core.exe']
    assert out['mismatch'] == []


def test_changed_sidecar_is_a_mismatch(tmp_path):
    root, manifest = _install(tmp_path)
    with open(str(root / 'runtime' / 'go' / 'hxp-guard.exe'), 'ab') as fh:
        fh.write(b' patched')
    out = I.check_native(manifest_path=str(manifest), root=str(root))
    assert out['ok'] is False
    assert out['checked'] == 3
    assert out['missing'] == []
    assert out['mismatch'] == ['hxp-guard.exe']


def test_missing_manifest_is_a_manifest_error(tmp_path):
    root, manifest = _install(tmp_path)
    manifest.unlink()
    out = I.check_native(manifest_path=str(manifest), root=str(root))
    assert out['ok'] is False
    assert out['reason'] == 'manifest'
    assert out['checked'] == 0


def test_invalid_json_is_a_manifest_error(tmp_path):
    root, manifest = _install(tmp_path)
    manifest.write_text('{not json', encoding='utf-8')
    out = I.check_native(manifest_path=str(manifest), root=str(root))
    assert out == {'ok': False, 'checked': 0, 'missing': [], 'mismatch': [],
                   'reason': 'manifest'}


def test_wrong_version_or_missing_entry_is_a_manifest_error(tmp_path):
    root, manifest = _install(tmp_path)
    doc = json.loads(manifest.read_text(encoding='utf-8'))

    bad_version = dict(doc)
    bad_version['version'] = 2
    manifest.write_text(json.dumps(bad_version), encoding='utf-8')
    assert I.check_native(manifest_path=str(manifest),
                          root=str(root))['reason'] == 'manifest'

    missing_entry = {'version': 1, 'files': dict(doc['files'])}
    missing_entry['files'].pop('hxp-shield.exe')
    manifest.write_text(json.dumps(missing_entry), encoding='utf-8')
    assert I.check_native(manifest_path=str(manifest),
                          root=str(root))['reason'] == 'manifest'

    bad_digest = {'version': 1, 'files': dict(doc['files'])}
    bad_digest['files']['hxp-core.exe'] = 'not-a-sha256'
    manifest.write_text(json.dumps(bad_digest), encoding='utf-8')
    assert I.check_native(manifest_path=str(manifest),
                          root=str(root))['reason'] == 'manifest'


def test_source_checkout_has_no_manifest(monkeypatch):
    import replica.build_info as BI
    monkeypatch.setattr(BI, 'is_compiled', lambda: False)
    assert I.native_manifest_path() is None


def test_frozen_manifest_path_is_runtime_integrity_json(tmp_path, monkeypatch):
    import replica.build_info as BI
    monkeypatch.setattr(BI, 'is_compiled', lambda: True)
    monkeypatch.setattr(BI, 'app_root', lambda: str(tmp_path))
    expected = os.path.join(str(tmp_path), 'runtime', 'integrity.json')
    assert I.native_manifest_path() == expected

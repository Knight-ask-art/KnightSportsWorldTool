"""Native manifest: signature, match, missing, mismatch, bad manifest.

All fixtures live under ``tmp_path``; nothing reads ``app/data`` and no native
binary is started.  ``check_native`` reports; ``tamper_message`` is the narrow
login gate and must fire for a bad signature or a changed file, never for a
missing one.
"""
from __future__ import annotations

import hashlib
import json
import os

import pytest

from replica.application import integrity as I
from replica.domain import ed25519

SEED = bytes.fromhex('11' * 32)
PUB = ed25519.public_key(SEED).hex()


@pytest.fixture(autouse=True)
def _embedded_key(monkeypatch):
    monkeypatch.setattr(I, 'MANIFEST_PUBKEY', PUB)


def _write(path, blob: bytes) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as fh:
        fh.write(blob)
    return hashlib.sha256(blob).hexdigest()


def _install(tmp_path, blobs=None, *, sign=True, version=I.MANIFEST_VERSION,
             seed=SEED):
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
    doc = {'version': version, 'files': digests}
    if sign:
        doc['sig'] = ed25519.sign(seed, I.signing_bytes(doc)).hex()
    manifest = root / 'runtime' / 'integrity.json'
    manifest.write_text(json.dumps(doc), encoding='utf-8')
    return root, manifest


def _load(manifest) -> dict:
    return json.loads(manifest.read_text(encoding='utf-8'))


def _store(manifest, doc) -> None:
    manifest.write_text(json.dumps(doc), encoding='utf-8')


def test_all_hashes_match(tmp_path):
    root, manifest = _install(tmp_path)
    out = I.check_native(manifest_path=str(manifest), root=str(root))
    assert out == {'ok': True, 'checked': 3, 'missing': [],
                   'unreadable': [], 'mismatch': []}


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


def test_a_missing_signature_is_rejected(tmp_path):
    root, manifest = _install(tmp_path)
    doc = _load(manifest)
    doc.pop('sig')
    _store(manifest, doc)
    out = I.check_native(manifest_path=str(manifest), root=str(root))
    assert out == {'ok': False, 'checked': 0, 'missing': [],
                   'unreadable': [], 'mismatch': [],
                   'reason': 'signature'}


def test_a_manifest_edit_breaks_the_signature(tmp_path):
    # Replacing a digest to match a patched binary is the whole point: the
    # signature covers files, so this must fail even though the hash checks
    # would have passed.
    root, manifest = _install(tmp_path)
    doc = _load(manifest)
    doc['files']['hxp-guard.exe'] = 'a' * 64
    _store(manifest, doc)
    out = I.check_native(manifest_path=str(manifest), root=str(root))
    assert out['reason'] == 'signature'


def test_a_signature_from_another_key_is_rejected(tmp_path):
    root, manifest = _install(tmp_path, seed=bytes.fromhex('22' * 32))
    out = I.check_native(manifest_path=str(manifest), root=str(root))
    assert out['reason'] == 'signature'


def test_an_empty_embedded_key_rejects_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(I, 'MANIFEST_PUBKEY', '')
    root, manifest = _install(tmp_path)
    assert I.check_native(manifest_path=str(manifest),
                          root=str(root))['reason'] == 'signature'


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
    assert out == {'ok': False, 'checked': 0, 'missing': [],
                   'unreadable': [], 'mismatch': [],
                   'reason': 'manifest'}


def test_an_oversized_manifest_is_rejected(tmp_path):
    root, manifest = _install(tmp_path)
    manifest.write_bytes(b'{' + b' ' * (2 << 20))
    out = I.check_native(manifest_path=str(manifest), root=str(root))
    assert out['reason'] == 'manifest'


def test_wrong_version_or_missing_entry_is_a_manifest_error(tmp_path):
    root, manifest = _install(tmp_path)
    doc = _load(manifest)

    bad_version = dict(doc)
    bad_version['version'] = I.MANIFEST_VERSION + 1
    _store(manifest, bad_version)
    assert I.check_native(manifest_path=str(manifest),
                          root=str(root))['reason'] == 'manifest'

    missing_entry = {'version': doc['version'], 'files': dict(doc['files']),
                     'sig': doc['sig']}
    missing_entry['files'].pop('hxp-shield.exe')
    _store(manifest, missing_entry)
    assert I.check_native(manifest_path=str(manifest),
                          root=str(root))['reason'] == 'manifest'

    bad_digest = {'version': doc['version'], 'files': dict(doc['files']),
                  'sig': doc['sig']}
    bad_digest['files']['hxp-core.exe'] = 'not-a-sha256'
    _store(manifest, bad_digest)
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


def test_a_changed_sidecar_is_the_blocking_shape(monkeypatch):
    monkeypatch.setattr(I, 'check_native', lambda: {
        'ok': False, 'checked': 3, 'missing': [],
        'mismatch': ['hxp-guard.exe']})
    assert I.tamper_message() == I.REPAIR_MESSAGE


def test_a_bad_signature_blocks(monkeypatch):
    monkeypatch.setattr(I, 'check_native', lambda: {
        'ok': False, 'checked': 0, 'missing': [], 'mismatch': [],
        'reason': 'signature'})
    assert I.tamper_message() == I.REPAIR_MESSAGE


def test_a_bad_manifest_blocks_in_a_frozen_build(monkeypatch):
    monkeypatch.setattr(I, 'check_native', lambda: {
        'ok': False, 'checked': 0, 'missing': [], 'mismatch': [],
        'reason': 'manifest'})
    assert I.tamper_message() == I.REPAIR_MESSAGE


def test_missing_files_do_not_block_here(monkeypatch):
    # A quarantine or an interrupted copy must not look like tampering; the
    # layer check in shield.require decides whether the build can run.
    monkeypatch.setattr(I, 'check_native', lambda: {
        'ok': False, 'checked': 2, 'missing': ['hxp-core.exe'],
        'mismatch': []})
    assert I.tamper_message() is None


def test_source_checkout_does_not_block(monkeypatch):
    monkeypatch.setattr(I, 'check_native', lambda: {
        'ok': True, 'checked': 0, 'missing': [], 'mismatch': [],
        'skipped': True})
    assert I.tamper_message() is None


def test_a_checker_bug_fails_open_but_input_does_not(monkeypatch):
    # The outer catch only covers bugs in the checker itself.  Attacker input
    # is handled inside check_native; see the nested-manifest test below.
    def boom():
        raise RuntimeError('programming error')
    monkeypatch.setattr(I, 'check_native', boom)
    assert I.tamper_message() is None


def test_a_deeply_nested_manifest_is_rejected_not_raised(tmp_path):
    root, manifest = _install(tmp_path)
    # ~9k nested arrays: json.load raises RecursionError, which is not a
    # ValueError.  It must become a value here, or tamper_message() would
    # treat the check as broken and fail open.
    manifest.write_text('[' * 9000 + ']' * 9000, encoding='utf-8')
    out = I.check_native(manifest_path=str(manifest), root=str(root))
    assert out['ok'] is False
    assert out['reason'] == 'manifest'


def test_a_v1_manifest_is_rejected(tmp_path):
    root, manifest = _install(tmp_path, version=1)
    out = I.check_native(manifest_path=str(manifest), root=str(root))
    assert out['reason'] == 'manifest'


def test_a_non_hex_signature_is_rejected(tmp_path):
    root, manifest = _install(tmp_path)
    doc = _load(manifest)
    doc['sig'] = 'z' * 128
    _store(manifest, doc)
    out = I.check_native(manifest_path=str(manifest), root=str(root))
    assert out['reason'] == 'signature'


def test_an_empty_files_map_is_rejected(tmp_path):
    root, manifest = _install(tmp_path)
    doc = _load(manifest)
    doc['files'] = {}
    _store(manifest, doc)
    out = I.check_native(manifest_path=str(manifest), root=str(root))
    assert out['reason'] == 'manifest'


def test_an_unreadable_sidecar_blocks(tmp_path, monkeypatch):
    root, manifest = _install(tmp_path)
    real = I._sha256

    def locked(path):
        if path.endswith('hxp-guard.exe'):
            raise OSError('sharing violation')
        return real(path)

    monkeypatch.setattr(I, '_sha256', locked)
    out = I.check_native(manifest_path=str(manifest), root=str(root))
    assert out['unreadable'] == ['hxp-guard.exe']
    assert out['missing'] == []
    assert out['ok'] is False
    monkeypatch.setattr(I, 'check_native', lambda: out)
    assert I.tamper_message() == I.READ_MESSAGE


def test_an_oversized_sidecar_is_a_mismatch(tmp_path, monkeypatch):
    root, manifest = _install(tmp_path)
    monkeypatch.setattr(I, 'MAX_HASH_BYTES', 4)
    out = I.check_native(manifest_path=str(manifest), root=str(root))
    assert out['mismatch'] == ['hxp-core.exe', 'hxp-guard.exe',
                               'hxp-shield.exe']
    assert out['checked'] == 0


def test_extra_fields_are_covered_by_the_signature():
    base = {'version': 2, 'files': {'x': 'a' * 64}}
    assert I.signing_bytes(dict(base, min_version=5)) != \
        I.signing_bytes(dict(base, min_version=6))


def test_repair_message_names_no_file_or_layer():
    lowered = I.REPAIR_MESSAGE.lower()
    for token in ('hxp', '.exe', 'sha', 'hash', '哈希', 'rust', 'go', 'cpp',
                  'nonce', 'token', 'signature', '签名'):
        assert token not in lowered

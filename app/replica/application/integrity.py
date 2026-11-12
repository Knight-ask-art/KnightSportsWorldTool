"""Native component hash manifest.

The packaged build ships three sidecars next to the Python code::

    runtime/rust/hxp-core.exe
    runtime/go/hxp-guard.exe
    runtime/cpp/hxp-shield.exe

``packaging/build.ps1`` writes ``runtime/integrity.json``::

    {"version": 1, "files": {"hxp-core.exe": "<sha256>", ...}}

This module re-hashes those three files on demand.

The gate is narrow on purpose.  A file that is **present but hashes
differently** is either damaged or edited, so ``mismatch_message`` stops the
login with a repair sentence.  A **missing** file is not a gate here: an
antivirus quarantine, a failed update or a half-finished copy are far more
common than tampering, and ``shield.require`` already refuses a frozen build
that cannot derive a credential.  A missing/malformed manifest stays
diagnostic for the same reason.

Source checkouts have no manifest and return ``skipped``.  Nothing here reads
``data/`` or touches the network.
"""
from __future__ import annotations

import hashlib
import json
import os

#: Manifest basename -> path below ``runtime/``.
NATIVE_FILES = (
    ('hxp-core.exe', os.path.join('rust', 'hxp-core.exe')),
    ('hxp-guard.exe', os.path.join('go', 'hxp-guard.exe')),
    ('hxp-shield.exe', os.path.join('cpp', 'hxp-shield.exe')),
)

MANIFEST_VERSION = 1

#: Shown when a shipped sidecar is present but does not match the manifest.
#: It names neither the file nor the reason: the user cannot act on either,
#: and a patcher should not learn which check fired.
REPAIR_MESSAGE = '安装文件校验未通过，请重新安装软件后再试'


def _manifest_error() -> dict:
    return {'ok': False, 'checked': 0, 'missing': [], 'mismatch': [],
            'reason': 'manifest'}


def native_manifest_path(root: str | None = None) -> str | None:
    """``<app_root>/runtime/integrity.json`` for a frozen build; else None.

    ``root`` is accepted for tests and for callers that already know the
    install directory.  A source checkout deliberately has no manifest.
    """
    if root is not None:
        return os.path.join(root, 'runtime', 'integrity.json')
    from ..build_info import app_root, is_compiled
    if not is_compiled():
        return None
    return os.path.join(app_root(), 'runtime', 'integrity.json')


def _install_root(manifest_path: str) -> str:
    # <root>/runtime/integrity.json -> <root>
    return os.path.dirname(os.path.dirname(os.path.abspath(manifest_path)))


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _valid_digest(value) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(c in '0123456789abcdefABCDEF' for c in value)


def check_native(manifest_path: str | None = None,
                 root: str | None = None) -> dict:
    """Compare the three sidecars with the packaged manifest.

    Returns ``{'ok', 'checked', 'missing', 'mismatch'}`` plus ``reason`` /
    ``skipped`` where applicable.  A missing or malformed manifest is
    ``ok=False, reason='manifest'`` but callers must treat it as diagnostic
    information, never as a hard failure of login or submit.
    """
    path = native_manifest_path(root) if manifest_path is None else manifest_path
    if path is None:
        return {'ok': True, 'checked': 0, 'missing': [], 'mismatch': [],
                'skipped': True}

    try:
        with open(path, encoding='utf-8') as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return _manifest_error()

    if (not isinstance(doc, dict)
            or doc.get('version') != MANIFEST_VERSION
            or not isinstance(doc.get('files'), dict)):
        return _manifest_error()

    declared = doc['files']
    expected: dict[str, str] = {}
    for name, _rel in NATIVE_FILES:
        value = declared.get(name)
        if not _valid_digest(value):
            return _manifest_error()
        expected[name] = value.lower()

    base = root or _install_root(path)
    checked = 0
    missing: list[str] = []
    mismatch: list[str] = []
    for name, rel in NATIVE_FILES:
        full = os.path.join(base, 'runtime', rel)
        if not os.path.isfile(full):
            missing.append(name)
            continue
        try:
            actual = _sha256(full)
        except OSError:
            missing.append(name)
            continue
        checked += 1
        if actual != expected[name]:
            mismatch.append(name)

    return {'ok': not missing and not mismatch, 'checked': checked,
            'missing': missing, 'mismatch': mismatch}


def mismatch_message() -> str | None:
    """Blocking reason for a sidecar that exists but is not what we shipped.

    Missing files and a bad manifest deliberately return ``None``: those are
    the shapes a quarantine or an interrupted update leaves behind, and the
    layer check in ``shield.require`` already handles a build that cannot
    derive a credential.  Only a positive byte-level mismatch stops a login.
    """
    try:
        out = check_native()
    except Exception:  # noqa: BLE001 - a broken check must not lock a user out
        return None
    if out.get('skipped') or out.get('ok'):
        return None
    return REPAIR_MESSAGE if out.get('mismatch') else None

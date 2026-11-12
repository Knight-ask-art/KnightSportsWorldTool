"""Native component manifest: hashes plus an Ed25519 signature.

The packaged build ships three sidecars next to the Python code::

    runtime/rust/hxp-core.exe
    runtime/go/hxp-guard.exe
    runtime/cpp/hxp-shield.exe

``packaging/build.ps1`` writes ``runtime/integrity.json``::

    {"version": 2,
     "files": {"hxp-core.exe": "<sha256>", ...},
     "sig": "<ed25519 over version+files>"}

This module verifies the signature, then re-hashes the three files.

The gate is narrow on purpose.  A missing or invalid signature, a present
file whose bytes changed, and a file that exists but cannot be read all stop a
privileged action: otherwise deleting ``sig`` would defeat the signature, and
an unreadable file could hide a replaced sidecar behind an ACL.  A **missing
file** is not a gate here -- an antivirus quarantine or a half-finished copy
is far more common than a deliberate deletion, and ``shield.require`` already
refuses a build that cannot derive a credential.

Source checkouts have no manifest and return ``skipped``.  Nothing here reads
``data/`` or touches the network.
"""
from __future__ import annotations

import hashlib
import json
import os

from ..domain import ed25519

#: Manifest basename -> path below ``runtime/``.
NATIVE_FILES = (
    ('hxp-core.exe', os.path.join('rust', 'hxp-core.exe')),
    ('hxp-guard.exe', os.path.join('go', 'hxp-guard.exe')),
    ('hxp-shield.exe', os.path.join('cpp', 'hxp-shield.exe')),
)

MANIFEST_VERSION = 2

#: Ed25519 public key, 64 hex chars.  ``scripts/manifest_sign.py keygen
#: --apply`` writes this line; the matching private key stays off the repo.
#: An empty value makes every frozen build refuse to log in, which is the
#: correct failure mode for an unsigned release.
MANIFEST_PUBKEY = '845d046f58c78dd38b3460515723905bcdfb0394d706e021111836c553df23f0'

#: Shown when an install cannot be trusted.  It names neither the file nor the
#: reason: the user cannot act on either, and a patcher should not learn which
#: check fired.
REPAIR_MESSAGE = '安装文件校验未通过，请重新安装软件后再试'

#: A shipped file exists but the process may not read it (sharing violation,
#: ACL, antivirus lock).  Separate from REPAIR_MESSAGE because reinstalling is
#: not the first thing to try.
READ_MESSAGE = '安装文件无法读取，请关闭杀毒软件后重启软件'

#: A sidecar larger than this is not one of ours; hashing it would also stall
#: the login thread.  The real files are a few MB at most.
MAX_HASH_BYTES = 64 << 20


def _manifest_error(reason: str = 'manifest') -> dict:
    return {'ok': False, 'checked': 0, 'missing': [], 'unreadable': [],
            'mismatch': [], 'reason': reason}


def signing_bytes(doc: dict) -> bytes:
    """Canonical bytes covered by ``sig``.

    Every top-level field except ``sig`` is signed, so a future
    ``min_version`` cannot ride along unauthenticated.  Formatting and key
    order in the file are not covered, so the manifest can be pretty-printed
    without invalidating the signature.
    """
    body = {key: value for key, value in doc.items() if key != 'sig'}
    return json.dumps(body, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def _signature_ok(doc: dict) -> bool:
    embedded = (MANIFEST_PUBKEY or '').strip().lower()
    if len(embedded) != 64:
        return False
    sig = doc.get('sig')
    if not isinstance(sig, str) or len(sig) != 128:
        return False
    try:
        return ed25519.verify(bytes.fromhex(embedded), signing_bytes(doc),
                              bytes.fromhex(sig))
    except ValueError:
        return False


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
    """Verify the signature, then compare the three sidecars with the manifest.

    Returns ``{'ok', 'checked', 'missing', 'mismatch'}`` plus ``reason`` /
    ``skipped`` where applicable.  A missing, malformed or unsigned manifest is
    ``ok=False`` with ``reason='manifest'`` / ``'signature'``; ``tamper_message``
    turns those into a login refusal in a frozen build.
    """
    path = native_manifest_path(root) if manifest_path is None else manifest_path
    if path is None:
        return {'ok': True, 'checked': 0, 'missing': [], 'unreadable': [],
                'mismatch': [], 'skipped': True}

    try:
        # A corrupted or replaced manifest should not be able to stall a
        # login by being enormous; 1 MiB is far above the real ~300 bytes.
        if os.path.getsize(path) > (1 << 20):
            return _manifest_error()
        with open(path, encoding='utf-8') as fh:
            doc = json.load(fh)
    except Exception:  # noqa: BLE001
        # json.load raises RecursionError (~10k nested brackets) on input the
        # attacker controls.  A bad manifest must stay a value here: if it
        # escaped, tamper_message() would treat the check as broken and fail
        # open, which is exactly the bypass this gate exists to stop.
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

    # Signature first: a bad manifest should not cost three file reads, and
    # an unsigned one must not reach the hash comparison as if it were fine.
    if not _signature_ok(doc):
        return _manifest_error('signature')

    base = root or _install_root(path)
    checked = 0
    missing: list[str] = []
    unreadable: list[str] = []
    mismatch: list[str] = []
    for name, rel in NATIVE_FILES:
        full = os.path.join(base, 'runtime', rel)
        if not os.path.exists(full):
            missing.append(name)
            continue
        try:
            if os.path.getsize(full) > MAX_HASH_BYTES:
                mismatch.append(name)
                continue
            actual = _sha256(full)
        except OSError:
            # Present but unreadable is not "missing": treating it as missing
            # would let a replaced sidecar hide behind an ACL or a share lock.
            unreadable.append(name)
            continue
        checked += 1
        if actual != expected[name]:
            mismatch.append(name)

    return {'ok': not missing and not mismatch and not unreadable,
            'checked': checked, 'missing': missing, 'unreadable': unreadable,
            'mismatch': mismatch}


def tamper_message() -> str | None:
    """Blocking reason for an install that cannot be trusted.

    A frozen build refuses a manifest that is missing, malformed, unsigned or
    signed by another key: a signature that can be deleted is not a signature.
    Missing *files* deliberately return ``None`` -- a quarantine or an
    interrupted update is not tampering, and the layer check in
    ``shield.require`` already handles a build that cannot derive a credential.
    """
    try:
        out = check_native()
    except Exception:  # noqa: BLE001 - a broken check must not lock a user out
        return None
    if out.get('skipped'):
        return None
    if out.get('mismatch') or out.get('reason') in ('manifest', 'signature'):
        return REPAIR_MESSAGE
    if out.get('unreadable'):
        return READ_MESSAGE
    return None

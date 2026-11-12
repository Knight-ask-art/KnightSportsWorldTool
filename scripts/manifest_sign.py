"""Sign or verify ``runtime/integrity.json``.

The installer ships an Ed25519 signature over ``{"version","files"}`` so a
patched sidecar cannot be hidden by editing the manifest.  The private key
never enters the repository: it lives in ``JHP_MANIFEST_KEY`` (64 hex chars)
or in ``%USERPROFILE%\\.hxp\\manifest.key``, and the public key is compiled
into ``replica/application/integrity.py``.

    python scripts/manifest_sign.py keygen [--out PATH] [--force] [--apply]
    python scripts/manifest_sign.py sign   --manifest runtime/integrity.json
    python scripts/manifest_sign.py verify --manifest runtime/integrity.json

``sign`` is called by ``app/packaging/build.ps1`` after the hashes are
written; ``verify`` re-checks the result against the embedded public key so a
key mismatch fails the build instead of every user's login.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
APP = REPO / 'app'
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from replica.application import integrity as I  # noqa: E402
from replica.domain import ed25519  # noqa: E402

DEFAULT_KEY = Path.home() / '.hxp' / 'manifest.key'
# No ``$`` anchor: a CRLF checkout has a trailing ``\r`` and the line would
# silently fail to match (and ``--apply`` would refuse to run there).
PUBKEY_LINE = re.compile(r"^MANIFEST_PUBKEY = '[0-9a-f]*'", re.MULTILINE)


def _fail(message: str) -> int:
    sys.stderr.write('manifest_sign: %s\n' % message)
    return 1


def _inside_repo(path: Path) -> bool:
    try:
        path.resolve().relative_to(REPO)
    except ValueError:
        return False
    return True


def _key_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get('JHP_MANIFEST_KEY_FILE')
    if env:
        return Path(env).expanduser()
    return DEFAULT_KEY


def _read_seed(explicit: str | None) -> bytes:
    env = (os.environ.get('JHP_MANIFEST_KEY') or '').strip()
    if env:
        try:
            seed = bytes.fromhex(env)
        except ValueError:
            raise SystemExit(_fail('JHP_MANIFEST_KEY is not hex'))
        if len(seed) != 32:
            raise SystemExit(_fail('JHP_MANIFEST_KEY must be 32 bytes (64 hex)'))
        return seed

    path = _key_path(explicit)
    if not path.is_file():
        raise SystemExit(_fail(
            'no key at %s; run "manifest_sign.py keygen" first' % path))
    raw = path.read_text(encoding='ascii').strip()
    try:
        seed = bytes.fromhex(raw)
    except ValueError:
        raise SystemExit(_fail('%s is not hex' % path))
    if len(seed) != 32:
        raise SystemExit(_fail('%s must hold 32 bytes (64 hex)' % path))
    return seed


def _load(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise SystemExit(_fail('cannot read %s: %s' % (path, exc)))
    if not isinstance(doc, dict) or doc.get('version') != I.MANIFEST_VERSION:
        raise SystemExit(_fail('%s is not a version %d manifest'
                               % (path, I.MANIFEST_VERSION)))
    if not isinstance(doc.get('files'), dict):
        raise SystemExit(_fail('%s has no files map' % path))
    return doc


def _write(path: Path, doc: dict) -> None:
    body = {key: value for key, value in doc.items() if key != 'sig'}
    body['sig'] = doc['sig']
    # UTF-8 without a BOM, same as build.ps1.  Formatting is free: the
    # signature covers the canonical bytes, not this file.  Unknown fields are
    # kept so a future ``min_version`` is not silently dropped here.
    path.write_text(json.dumps(body, ensure_ascii=False, indent=1) + '\n',
                    encoding='utf-8')


def _embedded_pubkey() -> str:
    target = APP / 'replica' / 'application' / 'integrity.py'
    with open(target, encoding='utf-8', newline='') as fh:
        text = fh.read()
    match = PUBKEY_LINE.search(text)
    return match.group(0).split("'")[1] if match else ''


def _apply_pubkey(pub: str) -> None:
    target = APP / 'replica' / 'application' / 'integrity.py'
    # newline='' keeps CRLF checkouts byte-identical apart from the key, so
    # --apply does not turn the whole file into a diff.
    with open(target, encoding='utf-8', newline='') as fh:
        text = fh.read()
    if not PUBKEY_LINE.search(text):
        raise SystemExit(_fail('MANIFEST_PUBKEY line not found in %s' % target))
    text = PUBKEY_LINE.sub("MANIFEST_PUBKEY = '%s'" % pub, text, 1)
    with open(target, 'w', encoding='utf-8', newline='') as fh:
        fh.write(text)


def cmd_keygen(args) -> int:
    path = _key_path(args.out)
    if _inside_repo(path):
        return _fail('refusing to write a private key inside the repository')
    current = _embedded_pubkey()
    if args.apply and current and not args.force:
        return _fail(
            'MANIFEST_PUBKEY is already set; restore the existing key or pass '
            '--force to rotate (existing builds will stop verifying)')
    if path.exists() and not args.force:
        return _fail('%s exists; pass --force to replace it' % path)
    seed = os.urandom(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(seed.hex() + '\n', encoding='ascii')
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

    pub = ed25519.public_key(seed).hex()
    print('key written: %s' % path)
    print('public key : %s' % pub)
    print("embed with : MANIFEST_PUBKEY = '%s'" % pub)
    if args.apply:
        _apply_pubkey(pub)
        print('applied    : app/replica/application/integrity.py')
    print('back this file up: losing it means rotating the key and '
          'shipping a new build.')
    return 0


def cmd_sign(args) -> int:
    path = Path(args.manifest)
    doc = _load(path)
    seed = _read_seed(args.key)
    doc['sig'] = ed25519.sign(seed, I.signing_bytes(doc)).hex()
    _write(path, doc)
    print('signed: %s' % path)
    return 0


def cmd_verify(args) -> int:
    path = Path(args.manifest)
    _load(path)
    # check_native covers the signature *and* the manifest shape (three names,
    # 64-hex digests) *and* the files on disk.  A signature over a manifest
    # missing a sidecar would otherwise pass here and lock every frozen user
    # out at login.
    out = I.check_native(manifest_path=str(path), root=str(path.parent.parent))
    if out.get('ok'):
        print('verified: %s' % path)
        return 0
    detail = (out.get('reason') or out.get('unreadable')
              or out.get('mismatch') or out.get('missing') or 'unknown')
    return _fail('%s failed: %s' % (path, detail))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='cmd', required=True)

    gen = sub.add_parser('keygen')
    gen.add_argument('--out', default='')
    gen.add_argument('--force', action='store_true')
    gen.add_argument('--apply', action='store_true')
    gen.set_defaults(func=cmd_keygen)

    sign = sub.add_parser('sign')
    sign.add_argument('--manifest', required=True)
    sign.add_argument('--key', default='')
    sign.set_defaults(func=cmd_sign)

    verify = sub.add_parser('verify')
    verify.add_argument('--manifest', required=True)
    verify.set_defaults(func=cmd_verify)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())

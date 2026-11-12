#!/usr/bin/env python3
"""Create the Ed25519 key used to sign ``latest.json`` for the Go launcher.

The private key never enters the repository.  ``build.ps1`` derives the public
key from it, signs the manifest, and injects the public key into
``hxp-launcher.exe``.  Back the file up: rotating it means every installed
launcher needs a new build.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

DEFAULT_PATH = Path.home() / '.hxp' / 'update-signing.pem'


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--path',
        default=str(DEFAULT_PATH),
        help=f'private key path (default: {DEFAULT_PATH})',
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='overwrite an existing key; only when rotating deliberately',
    )
    args = parser.parse_args(argv)

    path = Path(os.path.expanduser(args.path))
    if path.exists() and not args.force:
        print(f'error: {path} already exists; pass --force only when '
              'rotating the key', file=sys.stderr)
        return 2

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_bytes(pem)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)

    raw_pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    print(f'wrote:       {path}')
    print(f'public key:  release:{base64.b64encode(raw_pub).decode("ascii")}')
    print('back up this file; rotating it requires rebuilding the launcher')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

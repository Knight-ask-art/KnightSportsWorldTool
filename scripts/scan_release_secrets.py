#!/usr/bin/env python3
"""Fail when a release binary still carries a plaintext protocol constant.

Usage:
    python scripts/scan_release_secrets.py app/dist/好想跑_v0.2.2/好想跑.exe

Exit codes: 0 clean, 1 forbidden literal found, 2 bad usage / unreadable file.
Only the label is printed; the matched bytes and their context never are.
Add a new literal to ``SECRETS`` and both this scanner and the build gate
pick it up.
"""
from __future__ import annotations

import sys
from pathlib import Path

#: label -> forbidden plaintext.  Keep this the single source of truth.
SECRETS: dict[str, str] = {
    'RUNSCHOOL_SECRET': '2slhe02lsfiwowlcixisla_sls-_slaor',
    'TOKEN_SIGN_SALT': '&wh2016_swcampus',
    'RUNEC_KEY': 'd8dba5641eace5ea41cc502ae30d3bb2',
    'APP_ID': 'prj7sr2xk2bntjccmzbjckps7m9ogmlz',
    'UPLOAD_HOST': 'run.gxapp.iydsj.com',
}


def _needles():
    """(label, bytes) pairs; terse strings may be UTF-16 in some toolchains."""
    for label, value in SECRETS.items():
        yield label, value.encode('utf-8')
        yield label, value.encode('utf-16-le')


def scan_bytes(data: bytes) -> list[str]:
    """Labels of every forbidden literal present in ``data`` (deduplicated)."""
    hits: list[str] = []
    for label, needle in _needles():
        if needle and needle in data and label not in hits:
            hits.append(label)
    return hits


def scan_file(path: str | Path) -> list[str]:
    return scan_bytes(Path(path).read_bytes())


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print('usage: python scripts/scan_release_secrets.py <binary>',
              file=sys.stderr)
        return 2
    path = Path(args[0])
    try:
        hits = scan_file(path)
    except OSError as exc:
        print(f'error: cannot read {path}: {exc}', file=sys.stderr)
        return 2
    if hits:
        for label in hits:
            print(f'HIT {label}')
        print(f'{path.name}: {len(hits)} forbidden plaintext literal(s)')
        return 1
    print(f'{path.name}: clean')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Minify the frontend JavaScript that ships inside the package.

Usage:
    python scripts/minify_frontend.py <frontend-dir>

The source tree is never edited: the packaging script points this tool at the
built ``frontend`` directory (``app/build_stage/好想跑/frontend``), so
development keeps readable files while a release ships compressed ones.

Why terser and not a new language
---------------------------------
Moving the UI to TypeScript or WASM does not hide it: the WebView still gets
JavaScript, DOM ids and bridge method names.  This step only makes casual
reading slower.  The real boundary stays on the Python side (server-side
checks, request signing, device credential).

Rules:
  * only ``js/*.js`` is touched; vendor code and ``theme-boot.js`` stay as-is;
  * no source map is written;
  * top-level names are preserved (no ``--toplevel``), because ``app.js``
    uses globals defined by ``charts.js`` / ``announcements.js`` and the HTML
    has no inline handlers to resolve renamed functions;
  * output is syntax-checked with ``node --check`` before anything is
    replaced; a failure leaves the target directory untouched.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TERSER_REF = 'terser@5.36.0'
MARKER = '/* build artifact: minified; do not edit */'


def _tool(name: str) -> str | None:
    return shutil.which(name)


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=False, capture_output=True, **kwargs)


def _already_minified(path: Path) -> bool:
    try:
        with path.open('rb') as fh:
            head = fh.readline().strip()
    except OSError:
        return False
    return head == MARKER.encode('utf-8')


def minify(frontend_dir: Path, terser_ref: str = TERSER_REF) -> int:
    js_dir = frontend_dir / 'js'
    if not js_dir.is_dir():
        print(f'error: {js_dir} is not a directory', file=sys.stderr)
        return 1

    files = sorted(p for p in js_dir.glob('*.js')
                   if p.is_file() and not p.name.startswith('.minify-'))
    if not files:
        print(f'error: no JavaScript files under {js_dir}', file=sys.stderr)
        return 1

    pending = [p for p in files if not _already_minified(p)]
    if not pending:
        print(f'frontend already minified: {len(files)} file(s)')
        return 0

    node = _tool('node')
    npx = _tool('npx')
    if not node:
        print('error: node is not installed or not on PATH', file=sys.stderr)
        return 1
    if not npx:
        print('error: npx is not installed or not on PATH', file=sys.stderr)
        return 1

    before = sum(p.stat().st_size for p in files)
    temps: dict[Path, Path] = {}
    try:
        for src in pending:
            print(f'minify {src.name} ...', end=' ', flush=True)
            cmd = [
                npx, '--yes', terser_ref, str(src),
                '--compress', 'passes=2',
                '--mangle',
                '--ecma', '2020',
            ]
            proc = _run(cmd)
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout).decode('utf-8', 'replace')
                print('FAILED')
                print(detail.strip()[:2000], file=sys.stderr)
                return 1
            out = proc.stdout
            if not out:
                print('FAILED (empty output)')
                return 1

            fd, tmp_name = tempfile.mkstemp(
                prefix='.minify-', suffix='.js', dir=str(js_dir))
            os.close(fd)
            tmp = Path(tmp_name)
            temps[src] = tmp
            tmp.write_bytes(MARKER.encode('utf-8') + b'\n' + out)

            check = _run([node, '--check', str(tmp)])
            if check.returncode != 0:
                detail = (check.stderr or check.stdout).decode('utf-8', 'replace')
                print('FAILED (node --check)')
                print(detail.strip()[:2000], file=sys.stderr)
                return 1
            print(f'ok ({src.stat().st_size // 1024} KB -> {tmp.stat().st_size // 1024} KB)')

        # Only replace originals after every file passed the syntax check.
        for src, tmp in temps.items():
            os.replace(tmp, src)
            temps[src] = src  # mark as consumed for the cleanup below
    finally:
        for src, tmp in temps.items():
            if tmp != src:
                try:
                    tmp.unlink()
                except OSError:
                    pass

    after = sum(p.stat().st_size for p in files)
    saved = before - after
    pct = (saved / before * 100) if before else 0
    print(f'minified {len(pending)}/{len(files)} file(s): '
          f'{before // 1024} KB -> {after // 1024} KB '
          f'({pct:.1f}% smaller, no source map)')
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('frontend_dir', type=Path,
                        help='built frontend directory (contains js/)')
    parser.add_argument('--terser', default=TERSER_REF,
                        help=f'npx package reference (default: {TERSER_REF})')
    args = parser.parse_args(argv)
    return minify(args.frontend_dir.resolve(), args.terser)


if __name__ == '__main__':
    raise SystemExit(main())

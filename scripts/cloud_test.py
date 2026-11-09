#!/usr/bin/env python3
"""Run this repo's tests on an InstaBox Linux machine.

The local machine is Windows-first; the cloud box is where the cross-platform
part of the suite (and any long-running tooling) belongs.  Only files tracked
by git are uploaded, so ``data/``, ``dist/`` and local credentials never leave
the laptop.

Usage:
    python scripts/cloud_test.py
    python scripts/cloud_test.py --machine b --command "pytest tests/test_clock_alignment.py -q"
    python scripts/cloud_test.py --keep-source

Environment:
    INSTABOX_API_KEY   API key (already set for the OpenCode MCP server)
    INSTABOX_MACHINE   default machine name/ID (fallback: agent-browser-yrbo)
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

API = 'https://api.instabox.sh'
DEFAULT_VENV = '/data/work/venvs/hxp311'
DEFAULT_COMMAND = 'python -m pytest tests -q'
DEPENDENCIES = (
    'pytest',
    'requests',
    'pycryptodome',
    'numpy',
    'opencv-python-headless',
)


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ['git', *args], cwd=root, text=True,
        encoding='utf-8', errors='replace').strip()


def resolve_machine(session: requests.Session, ref: str) -> str:
    if ref.startswith('m_'):
        return ref
    response = session.get(f'{API}/v1/machines', timeout=30)
    response.raise_for_status()
    for machine in response.json().get('machines', []):
        if machine.get('name') == ref:
            return machine['id']
    names = ', '.join(m.get('name', '?') for m in
                      response.json().get('machines', [])) or '(none)'
    raise SystemExit(f'machine {ref!r} not found; available: {names}')


def build_script(command: str, cwd: str, keep_source: bool) -> str:
    deps = ' '.join(DEPENDENCIES)
    cleanup = '' if keep_source else (
        '\ncd /\nrm -rf /data/work/hxp-src\n')
    return f'''set -e
mkdir -p /data/work
rm -rf /data/work/hxp-src
mkdir -p /data/work/hxp-src
tar xzf /data/work/hxp-src.tar.gz -C /data/work/hxp-src
rm -f /data/work/hxp-src.tar.gz
if [ ! -x {DEFAULT_VENV}/bin/python ]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv {DEFAULT_VENV} --python 3.11
  else
    python3 -m venv {DEFAULT_VENV}
  fi
fi
if [ ! -f {DEFAULT_VENV}/.hxp-deps ]; then
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python {DEFAULT_VENV}/bin/python {deps}
  else
    {DEFAULT_VENV}/bin/python -m pip install -U pip {deps}
  fi
  touch {DEFAULT_VENV}/.hxp-deps
fi
export PATH={DEFAULT_VENV}/bin:$PATH
cd /data/work/hxp-src/{shlex.quote(cwd)}
{command}
{cleanup}'''


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--machine',
                        default=os.environ.get('INSTABOX_MACHINE',
                                               'agent-browser-yrbo'),
                        help='InstaBox machine name or m_... ID')
    parser.add_argument('--command', default=DEFAULT_COMMAND,
                        help=f'shell command run inside app/ (default: {DEFAULT_COMMAND})')
    parser.add_argument('--cwd', default='app',
                        help='directory under the repo root (default: app)')
    parser.add_argument('--timeout', type=int, default=600,
                        help='exec timeout in seconds (max 600)')
    parser.add_argument('--keep-source', action='store_true',
                        help='leave /data/work/hxp-src on the machine')
    args = parser.parse_args(argv)

    api_key = os.environ.get('INSTABOX_API_KEY')
    if not api_key:
        raise SystemExit('INSTABOX_API_KEY is not set')

    try:
        root = Path(git(Path.cwd(), 'rev-parse', '--show-toplevel'))
    except (subprocess.CalledProcessError, FileNotFoundError):
        raise SystemExit('not inside a git repository')

    commit = git(root, 'rev-parse', '--short', 'HEAD')
    session = requests.Session()
    session.headers['X-API-Key'] = api_key
    machine_id = resolve_machine(session, args.machine)

    tmpdir = Path(tempfile.mkdtemp(prefix='hxp-cloud-'))
    archive = tmpdir / 'src.tar.gz'
    try:
        subprocess.run(
            ['git', 'archive', '--format=tar.gz', '--output', str(archive),
             'HEAD'],
            cwd=root, check=True)
        size_mb = archive.stat().st_size / (1024 * 1024)
        print(f'uploading {commit} ({size_mb:.1f} MiB) to {args.machine} ...')
        with archive.open('rb') as handle:
            response = session.put(
                f'{API}/v1/machines/{machine_id}/files',
                params={'path': '/data/work/hxp-src.tar.gz', 'parents': 'true'},
                data=handle,
                headers={'Content-Type': 'application/octet-stream'},
                timeout=300)
        response.raise_for_status()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    script = build_script(args.command, args.cwd, args.keep_source)
    response = session.post(
        f'{API}/v1/machines/{machine_id}/exec',
        json={'cmd': script, 'timeoutSeconds': min(600, args.timeout)},
        timeout=args.timeout + 30)
    response.raise_for_status()
    result = response.json()

    if result.get('stdout'):
        print(result['stdout'], end='')
    if result.get('stderr'):
        print(result['stderr'], end='', file=sys.stderr)
    if result.get('timedOut'):
        print('cloud test timed out', file=sys.stderr)
        return 124
    code = result.get('exitCode')
    print(f"exit={code} machine={args.machine} commit={commit}")
    if code is None:
        print('cloud test returned no exit code', file=sys.stderr)
        return 1
    return int(code)


if __name__ == '__main__':
    raise SystemExit(main())

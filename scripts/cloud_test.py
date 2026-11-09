#!/usr/bin/env python3
"""Run this repo's tests on an InstaBox Linux machine.

The local machine is Windows-first; the cloud box is where the cross-platform
part of the suite (and any long-running tooling) belongs.  Only files tracked
by git are uploaded, so ``data/``, ``dist/`` and local credentials never leave
the laptop.  A dirty working tree is refused by default because ``git archive
HEAD`` would silently test the previous commit.

Usage:
    python scripts/cloud_test.py
    python scripts/cloud_test.py --command "pytest tests/test_clock_alignment.py -q"
    python scripts/cloud_test.py --keep-source

Environment:
    INSTABOX_API_KEY   API key (already set for the OpenCode MCP server)
    INSTABOX_MACHINE   default machine name/ID (fallback: agent-browser-yrbo)
"""

from __future__ import annotations

import argparse
import hashlib
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
DEPS_HASH = hashlib.sha256(
    ('|'.join(DEPENDENCIES) + '|py311').encode()).hexdigest()[:12]


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ['git', *args], cwd=root, text=True,
        encoding='utf-8', errors='replace').strip()


def resolve_machine(session: requests.Session, ref: str) -> str:
    if ref.startswith('m_'):
        return ref
    response = session.get(f'{API}/v1/machines', timeout=30)
    if response.status_code >= 400:
        raise SystemExit(f'machine list failed: {response.text[:500]}')
    machines = response.json().get('machines', [])
    for machine in machines:
        if machine.get('name') == ref:
            return machine['id']
    names = ', '.join(m.get('name', '?') for m in machines) or '(none)'
    raise SystemExit(f'machine {ref!r} not found; available: {names}')


def build_script(command: str, cwd: str, keep_source: bool,
                 source_dir: str, tar_path: str) -> str:
    deps = ' '.join(DEPENDENCIES)
    cleanup = '' if keep_source else (
        f"trap 'cd /; rm -rf {source_dir}' EXIT\n")
    return f'''set -e
{cleanup}mkdir -p {source_dir}
tar xzf {tar_path} -C {source_dir}
rm -f {tar_path}
if [ ! -x {DEFAULT_VENV}/bin/python ]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv {DEFAULT_VENV} --python 3.11
  else
    python3 -m venv {DEFAULT_VENV}
  fi
fi
if [ ! -f {DEFAULT_VENV}/.deps-{DEPS_HASH} ]; then
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python {DEFAULT_VENV}/bin/python {deps}
  else
    {DEFAULT_VENV}/bin/python -m pip install -U pip {deps}
  fi
  touch {DEFAULT_VENV}/.deps-{DEPS_HASH}
fi
export PATH={DEFAULT_VENV}/bin:$PATH
cd {source_dir}/{shlex.quote(cwd)}
{command}
'''


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
                        help='exec timeout in seconds (1-600)')
    parser.add_argument('--keep-source', action='store_true',
                        help='leave the extracted source on the machine')
    parser.add_argument('--allow-dirty', action='store_true',
                        help='allow uploading HEAD while the worktree is dirty')
    args = parser.parse_args(argv)

    if not 1 <= args.timeout <= 600:
        parser.error('--timeout must be between 1 and 600 seconds')
    cwd_path = Path(args.cwd)
    if cwd_path.is_absolute() or '..' in cwd_path.parts:
        parser.error('--cwd must be a relative path without ".."')

    api_key = os.environ.get('INSTABOX_API_KEY')
    if not api_key:
        raise SystemExit('INSTABOX_API_KEY is not set')

    try:
        root = Path(git(Path.cwd(), 'rev-parse', '--show-toplevel'))
    except (subprocess.CalledProcessError, FileNotFoundError):
        raise SystemExit('not inside a git repository')

    dirty = git(root, 'status', '--porcelain')
    if dirty and not args.allow_dirty:
        raise SystemExit(
            'working tree is dirty; commit first (or pass --allow-dirty). '
            'git archive HEAD does not include uncommitted changes.')
    if dirty:
        print('warning: uploading HEAD, not the dirty working tree',
              file=sys.stderr)

    commit = git(root, 'rev-parse', '--short', 'HEAD')
    run_id = f'{commit}-{os.getpid()}'
    source_dir = f'/data/work/hxp-src-{run_id}'
    tar_path = f'/data/work/hxp-src-{run_id}.tar.gz'
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
        print(f'uploading {commit} ({size_mb:.1f} MiB) to {args.machine} ...',
              flush=True)
        with archive.open('rb') as handle:
            response = session.put(
                f'{API}/v1/machines/{machine_id}/files',
                params={'path': tar_path, 'parents': 'true'},
                data=handle,
                headers={'Content-Type': 'application/octet-stream'},
                timeout=300)
        if response.status_code >= 400:
            print(response.text[:1000], file=sys.stderr)
            response.raise_for_status()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    script = build_script(args.command, args.cwd, args.keep_source,
                          source_dir, tar_path)
    response = session.post(
        f'{API}/v1/machines/{machine_id}/exec',
        json={'cmd': script, 'timeoutSeconds': args.timeout},
        timeout=args.timeout + 30)
    if response.status_code >= 400:
        print(response.text[:1000], file=sys.stderr)
        response.raise_for_status()
    try:
        result = response.json()
    except ValueError:
        print(response.text[:1000], file=sys.stderr)
        return 1

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

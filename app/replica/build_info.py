"""Version and build identifier for the UI, CLI and support bundle.

`packaging/build.ps1` writes ``replica/_build_info.py`` immediately before
PyInstaller, so a frozen build carries the commit it was made from.  A source
checkout asks git instead.  If neither is available (a copied folder without
.git), the identifier degrades to ``v0.1.2`` — it never invents a hash.

Keep this module dependency-free: it is imported by the bridge on startup.
"""

from __future__ import annotations

import os
import subprocess
import sys
from functools import lru_cache

#: Single source of truth.  packaging/build.ps1 and tests read this value.
VERSION = '0.2.3'

_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@lru_cache(maxsize=1)
def _generated() -> dict:
    """Values injected at build time (empty in a source checkout)."""
    try:
        from ._build_info import BUILD  # type: ignore
    except Exception:  # noqa: BLE001 - absent generated file is normal
        return {}
    return dict(BUILD) if isinstance(BUILD, dict) else {}


@lru_cache(maxsize=1)
def _git_state() -> tuple[str, bool]:
    """(short commit, dirty) for a source checkout; ('', False) otherwise."""
    if is_compiled():
        return '', False
    try:
        commit = subprocess.check_output(
            ['git', '-C', _APP_ROOT, 'rev-parse', '--short=7', 'HEAD'],
            stderr=subprocess.DEVNULL, timeout=3).decode('utf-8').strip()
        dirty = bool(subprocess.check_output(
            ['git', '-C', _APP_ROOT, 'status', '--porcelain'],
            stderr=subprocess.DEVNULL, timeout=3).decode('utf-8').strip())
        return commit, dirty
    except Exception:  # noqa: BLE001 - no git / not a repo
        return '', False


def is_compiled() -> bool:
    """True inside a PyInstaller/Nuitka build, false in a source checkout."""
    return bool(getattr(sys, 'frozen', False)
                or globals().get('__compiled__') is not None)


def app_root() -> str:
    """Directory that holds ``frontend/`` and ``data/``.

    In a compiled build that is next to the exe.  In a source checkout this
    file is ``app/replica/build_info.py``, so two dirnames reach ``app/``.
    """
    if is_compiled():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_info() -> dict:
    """Version + provenance.  Safe to show in the UI and in a diagnostic."""
    gen = _generated()
    commit = str(gen.get('commit') or '')
    dirty = bool(gen.get('dirty'))
    built_at = str(gen.get('built_at') or '')
    source = 'build'
    if not gen:
        commit, dirty = _git_state()
        source = 'git' if commit else 'source'
    return {
        'version': VERSION,
        'commit': commit,
        'dirty': dirty,
        'built_at': built_at,
        'source': source,
        'frozen': is_compiled(),
    }


def version_string(with_time: bool = True) -> str:
    """`v0.1.2+abc1234-dirty · 2026-10-25 12:34` (parts omitted if unknown)."""
    info = build_info()
    text = 'v' + VERSION
    if info['commit']:
        text += '+' + info['commit']
        if info['dirty']:
            text += '-dirty'
    if with_time and info['built_at']:
        text += ' · ' + info['built_at']
    return text


def self_test() -> list[tuple[str, bool, str]]:
    out = []
    info = build_info()
    out.append(('version matches module', info['version'] == VERSION, VERSION))
    text = version_string()
    out.append(('label starts with v', text.startswith('v' + VERSION), text))
    out.append(('no secret fields', not any(
        k in info for k in ('token', 'password', 'uid')), str(sorted(info))))
    return out

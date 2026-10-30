"""Pytest bootstrap: make the application package importable as `replica`.

Canonical import root is `replica` (``app/replica``). Importing it as
``app.replica`` from the repository root works as a namespace path, but loads a
second copy of the package; the test suite never does that.

The autouse fixture also points every runtime file at ``tmp_path``.  Without
it a single 10603 test writes ``app/data/points_ratelimit.json`` and the next
test (or a parallel run) inherits the cooldown.
"""

from __future__ import annotations

import os
import sys

import pytest

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)

_BASELINE = None


def pytest_configure(config):
    """Snapshot ``app/data`` so the session can prove it stayed untouched."""
    global _BASELINE
    data_dir = os.path.join(APP_ROOT, 'data')
    try:
        _BASELINE = set(os.listdir(data_dir))
    except OSError:
        _BASELINE = set()


def pytest_sessionfinish(session, exitstatus):
    if _BASELINE is None or os.environ.get('JHP_ALLOW_DATA_WRITES') == '1':
        return
    data_dir = os.path.join(APP_ROOT, 'data')
    try:
        current = set(os.listdir(data_dir))
    except OSError:
        return
    new = sorted(current - _BASELINE)
    if new:
        print('\n[test hygiene] the suite wrote into app/data: %s' % new)
        print('[test hygiene] isolate runtime files with tmp_path; set '
              'JHP_ALLOW_DATA_WRITES=1 only for a deliberate local run.')
        session.exitstatus = 1


@pytest.fixture(autouse=True)
def _isolate_runtime_files(tmp_path, monkeypatch):
    """Keep points cache / cooldown / store out of the repository."""
    from replica.application import points as P

    monkeypatch.setenv('JHP_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('JHP_POINTS_CACHE', str(tmp_path / 'points_cache.json'))
    monkeypatch.setenv('JHP_POINTS_RATELIMIT',
                       str(tmp_path / 'points_ratelimit.json'))
    P.clear_rate_limit()
    yield
    P.clear_rate_limit()

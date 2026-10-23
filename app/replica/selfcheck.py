"""Legacy entry point: ``python -m replica.selfcheck``.

The implementation lives in :mod:`replica.diagnostics.selfcheck`.
"""

from __future__ import annotations

from .diagnostics.selfcheck import MODULES, main, run

__all__ = ['MODULES', 'main', 'run']


if __name__ == '__main__':
    raise SystemExit(main())

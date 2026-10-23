"""Legacy entry point: ``python -m replica.pipeline``.

The implementation lives in :mod:`replica.adapters.cli`.  This wrapper keeps
the documented command stable for scripts and notes written before the split.
"""

from __future__ import annotations

from .adapters.cli import main

__all__ = ['main']


if __name__ == '__main__':
    raise SystemExit(main())

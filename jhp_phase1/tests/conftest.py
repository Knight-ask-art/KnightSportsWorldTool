"""Pytest bootstrap: make the application package importable as `replica`.

Canonical import root is `replica` (``jhp_phase1/replica``). `jhp_phase1.replica`
remains importable only as a legacy compatibility path when the parent folder is
on `sys.path`; the test suite never uses it.
"""

from __future__ import annotations

import os
import sys

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)

"""Pytest bootstrap: make the application package importable as `replica`.

Canonical import root is `replica` (``app/replica``). Importing it as
``app.replica`` from the repository root works as a namespace path, but loads a
second copy of the package; the test suite never does that.
"""

from __future__ import annotations

import os
import sys

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)

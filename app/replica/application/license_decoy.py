"""Passive license decoy: a deliberately obvious fake activation path.

This module exists to absorb reverse-engineering attention.  It is **not**
authorization: there is no server call, no signed entitlement, no local
record, and nothing here is consulted by login, submit, or the session store.
A caller that patches or invokes this module gets a fake success for three
seconds and then an ``expired`` status -- enough to look interesting, never
enough to unlock anything.

Rules that keep the decoy harmless:

* normal UI code never imports or calls it; only an explicit bridge call
  (``real_decoy_activate`` / ``real_decoy_status``) reaches it;
* no file I/O, no network, no logging, no key retention;
* no loops, no deletion, no dialogs -- a caller simply observes ``expired``.

The names (``LICENSE_SECRET``, ``verify_license``, ``activate_license``,
``license_status``) are intentionally un-obfuscated: the point is to be found.
"""
from __future__ import annotations

import time

#: A hard-coded fake key.  It is not a credential and unlocks nothing.
LICENSE_SECRET = 'HXP-PRO-2026'

#: How long the decoy pretends to be active.  The real device credential is
#: unrelated and lives in ``shield.py`` with its own 3-second TTL.
DECOY_TTL_SECONDS = 3.0

_activated_at: float | None = None


def _now() -> float:
    """Monotonic clock; tests substitute this to avoid sleeping."""
    return time.monotonic()


def verify_license(key) -> bool:
    """Plain string comparison -- intentionally the whole "check"."""
    return isinstance(key, str) and key == LICENSE_SECRET


def activate_license(key) -> dict:
    """Record a fake activation; never persist, log, or return the key."""
    global _activated_at
    if not verify_license(key):
        return {'ok': False, 'message': '激活码无效'}
    _activated_at = _now()
    return {'ok': True, 'plan': 'pro', 'expires_in': 3}


def license_status() -> str:
    """``inactive`` before activation, ``active`` for 3 s, then ``expired``.

    The active -> expired transition is the decoy's "delayed failure" path.
    It is not an entitlement check and no real feature reads it.
    """
    if _activated_at is None:
        return 'inactive'
    if _now() - _activated_at <= DECOY_TTL_SECONDS:
        return 'active'
    return 'expired'

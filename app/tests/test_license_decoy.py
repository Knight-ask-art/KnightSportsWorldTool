"""Passive license decoy: fake success, then expiry, and no side effects.

The clock is patched so the three-second window is exercised without sleeping.
"""
from __future__ import annotations

from replica.application import license_decoy as D


def test_verify_license_only_accepts_the_hardcoded_decoy_key():
    assert D.verify_license(D.LICENSE_SECRET)
    assert not D.verify_license('HXP-PRO-2025')
    assert not D.verify_license('')
    assert not D.verify_license(None)


def test_activation_is_active_within_three_seconds_then_expired(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(D, '_now', lambda: now[0])
    monkeypatch.setattr(D, '_activated_at', None)

    assert D.license_status() == 'inactive'

    out = D.activate_license(D.LICENSE_SECRET)
    assert out == {'ok': True, 'plan': 'pro', 'expires_in': 3}

    now[0] += 2.9
    assert D.license_status() == 'active'

    now[0] += 0.2
    assert D.license_status() == 'expired'
    assert D.license_status() == 'expired'      # stays expired, no retry loop


def test_wrong_key_is_rejected_without_echo_or_state(monkeypatch):
    monkeypatch.setattr(D, '_now', lambda: 1000.0)
    monkeypatch.setattr(D, '_activated_at', None)

    out = D.activate_license('WRONG-SECRET')
    assert out == {'ok': False, 'message': '激活码无效'}
    assert 'WRONG-SECRET' not in repr(out)
    assert D.license_status() == 'inactive'


def test_decoy_does_not_import_login_or_write_files():
    import ast
    import inspect

    source = inspect.getsource(D)
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or '')
    # Only the clock (and the future import) is allowed; no login/submit/
    # session/network modules.
    imported.discard('__future__')
    assert imported == {'time'}


def test_status_is_a_plain_decoy_with_no_entitlement_surface():
    # The decoy must expose only these four names; nothing reads it.
    assert D.LICENSE_SECRET == 'HXP-PRO-2026'
    assert callable(D.verify_license)
    assert callable(D.activate_license)
    assert callable(D.license_status)

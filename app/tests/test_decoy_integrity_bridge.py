"""Bridge wiring for the passive decoy and the integrity report.

These two surfaces are diagnostic/honeypot only: neither may alter the login
gate, write into the store, or appear in the frontend.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from replica.adapters.bridge_api import ReplicaApi
from replica.application import integrity as I
from replica.application import license_decoy as D
from replica.application import shield


def _api(tmp_path):
    return ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                      str(tmp_path))


def test_bridge_decoy_returns_fake_pro_plan_then_active(tmp_path, monkeypatch):
    monkeypatch.setattr(D, '_activated_at', None)
    monkeypatch.setattr(D, '_now', lambda: 1000.0)
    api = _api(tmp_path)

    out = api.real_decoy_activate(D.LICENSE_SECRET)
    assert out['code'] == 200
    assert out['data'] == {'ok': True, 'plan': 'pro', 'expires_in': 3}

    status = api.real_decoy_status()
    assert status['code'] == 200
    assert status['data'] == {'status': 'active'}


def test_bridge_decoy_rejects_wrong_key_without_echo(tmp_path, monkeypatch):
    monkeypatch.setattr(D, '_activated_at', None)
    api = _api(tmp_path)
    out = api.real_decoy_activate('WRONG-SECRET')
    assert out['code'] == 200
    assert out['data'] == {'ok': False, 'message': '激活码无效'}
    assert 'WRONG-SECRET' not in repr(out)


def test_bridge_integrity_is_skipped_in_source_checkout(tmp_path, monkeypatch):
    import replica.build_info as BI
    monkeypatch.setattr(BI, 'is_compiled', lambda: False)
    out = _api(tmp_path).real_integrity_status()
    assert out['code'] == 200
    assert out['data'] == {'ok': True, 'checked': 0, 'missing': [],
                           'mismatch': [], 'skipped': True}


def test_bridge_integrity_reports_a_manifest_error_without_raising(
        tmp_path, monkeypatch):
    monkeypatch.setattr(I, 'check_native', lambda: {
        'ok': False, 'checked': 0, 'missing': [], 'mismatch': [],
        'reason': 'manifest'})
    out = _api(tmp_path).real_integrity_status()
    assert out['code'] == 200
    assert out['data']['ok'] is False
    assert out['data']['reason'] == 'manifest'


def test_bridge_integrity_adds_the_selfcheck_outside_a_source_checkout(
        tmp_path, monkeypatch):
    monkeypatch.setattr(I, 'check_native', lambda: {
        'ok': True, 'checked': 3, 'missing': [], 'mismatch': []})
    monkeypatch.setattr(shield, 'selfcheck',
                        lambda: {'ok': False, 'reason': 'image'})
    out = _api(tmp_path).real_integrity_status()
    assert out['code'] == 200
    assert out['data']['selfcheck'] == {'ok': False, 'reason': 'image'}


def test_decoy_and_integrity_calls_do_not_bypass_the_login_gate(
        tmp_path, monkeypatch):
    monkeypatch.setattr(D, '_activated_at', None)
    monkeypatch.setattr(shield, 'require', lambda store: {
        'blocked': True, 'message': shield.SHIELD_MESSAGE})
    api = _api(tmp_path)

    before = set(os.listdir(str(tmp_path)))
    api.real_decoy_activate(D.LICENSE_SECRET)
    api.real_decoy_status()
    api.real_integrity_status()
    # The honeypot does not create files in the store either.
    assert set(os.listdir(str(tmp_path))) == before

    out = api.real_login('13800000000', 'pw')
    assert out == {'code': 403, 'message': shield.SHIELD_MESSAGE,
                   'data': {'reason': 'shield'}}


def test_decoy_has_no_frontend_entry():
    frontend = os.path.join(ROOT, 'frontend')
    hits = []
    for dirpath, _dirs, files in os.walk(frontend):
        for name in files:
            if not name.endswith(('.js', '.html', '.css')):
                continue
            path = os.path.join(dirpath, name)
            with open(path, encoding='utf-8', errors='ignore') as fh:
                text = fh.read()
            for needle in ('real_decoy_activate', 'real_decoy_status'):
                if needle in text:
                    hits.append((path, needle))
    assert hits == []

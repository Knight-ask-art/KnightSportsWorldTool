"""Submit failures must carry the backend's own classification.

The frontend used to infer "did the write land?" from Chinese message
prefixes because every bridge failure uses HTTP 500.  That decision now lives
in ``_fail_submit`` and reaches the UI as ``data.failure_kind``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import requests

from replica.adapters.bridge_api import ReplicaApi, _fail_submit
from replica.adapters.http_server import serve

BRIDGE = Path(__file__).resolve().parents[1] / 'replica' / 'adapters' / 'bridge_run.py'


def test_fail_submit_adds_kind_without_mutating_the_payload():
    payload = {'error': 10001}
    res = _fail_submit('提交被拒', data=payload)
    assert res['code'] == 500
    assert res['data'] == {'error': 10001, 'failure_kind': 'fail'}
    # The caller's dict may be a live response object; it must not be mutated.
    assert payload == {'error': 10001}

    res = _fail_submit('提交异常，结果未知', kind='unknown')
    assert res['data'] == {'failure_kind': 'unknown'}


def test_device_migration_failure_is_an_explicit_refusal():
    api = SimpleNamespace(_device_migration={
        'message': '设备指纹已修复，请重新登录后再提交'})
    res = ReplicaApi.real_submit_run(api)
    assert res['code'] != 200
    assert res['data']['failure_kind'] == 'fail'


def test_missing_account_failure_is_an_explicit_refusal():
    api = SimpleNamespace(_device_migration=None, _real_account=lambda: None)
    res = ReplicaApi.real_submit_run(api)
    assert res['code'] != 200
    assert res['data']['failure_kind'] == 'fail'
    assert 'token' in res['message']


def test_every_submit_failure_is_classified():
    """A raw _fail inside real_submit_run would drop failure_kind silently."""
    tree = ast.parse(BRIDGE.read_text(encoding='utf-8'))
    method = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == 'real_submit_run')
    calls = [
        node for node in ast.walk(method)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert [n for n in calls if n.func.id == '_fail'] == []
    classified = [n for n in calls if n.func.id == '_fail_submit']
    assert len(classified) >= 20
    kinds = [kw.value for n in classified for kw in n.keywords if kw.arg == 'kind']
    # The pre-submit branches default to fail; the three "maybe landed" paths
    # must say unknown either as a literal or through the state branch.
    assert any(isinstance(v, ast.Constant) and v.value == 'unknown' for v in kinds)
    assert any(isinstance(v, ast.IfExp) for v in kinds)


def test_http_submit_failure_envelope_carries_fail(tmp_path, monkeypatch):
    """End-to-end: the envelope JS receives has the field, not just the helper."""
    monkeypatch.setenv('JHP_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('JHP_POINTS_CACHE', str(tmp_path / 'points_cache.json'))
    srv = serve(0, store_dir=str(tmp_path), quiet=True)
    base = f'http://127.0.0.1:{srv.server_address[1]}'
    try:
        r = requests.post(base + '/bridge/real_submit_run',
                          json={'args': {}}, timeout=15)
        assert r.status_code == 200, r.text
        payload = r.json()
        assert payload['code'] == 500, payload
        assert payload['data']['failure_kind'] == 'fail', payload
    finally:
        srv.shutdown()
        srv.server_close()

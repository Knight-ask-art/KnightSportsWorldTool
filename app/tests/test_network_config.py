"""Network settings are visual, session-only, and never return the key raw."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from replica.adapters import bridge_api as replica_api  # noqa: E402


def _api(tmp_path):
    return replica_api.ReplicaApi(
        'http://127.0.0.1:1', 'http://127.0.0.1:1', b'', str(tmp_path))


def test_proxy_and_key_apply_to_process_only(tmp_path, monkeypatch):
    monkeypatch.delenv('JHP_PROXY', raising=False)
    monkeypatch.delenv('JHP_AMAP_KEY', raising=False)
    monkeypatch.setattr(replica_api, 'AMAP_KEY', '')
    api = _api(tmp_path)
    out = api.set_network_config(proxy='127.0.0.1:7890',
                                 amap_key='abcdef1234567890')
    assert out['code'] == 200
    assert os.environ['JHP_PROXY'] == 'http://127.0.0.1:7890'
    assert os.environ['JHP_AMAP_KEY'] == 'abcdef1234567890'
    assert out['data']['amap_key_present'] is True
    assert 'abcdef1234567890' not in str(out['data'])
    assert out['data']['amap_key_masked'].endswith('7890')
    # No settings file is written; the key lives in the process env only.
    assert not (tmp_path / 'network.json').exists()


def test_clear_removes_both_values(tmp_path, monkeypatch):
    monkeypatch.delenv('JHP_PROXY', raising=False)
    monkeypatch.delenv('JHP_AMAP_KEY', raising=False)
    monkeypatch.setattr(replica_api, 'AMAP_KEY', '')
    api = _api(tmp_path)
    api.set_network_config(proxy='http://127.0.0.1:7890',
                           amap_key='abcdef1234567890')
    out = api.set_network_config(clear_proxy=True, clear_amap_key=True)
    assert out['code'] == 200
    assert out['data']['proxy_present'] is False
    assert out['data']['amap_key_present'] is False
    assert 'JHP_PROXY' not in os.environ
    assert 'JHP_AMAP_KEY' not in os.environ


def test_invalid_proxy_is_rejected(tmp_path):
    api = _api(tmp_path)
    out = api.set_network_config(proxy='ftp://127.0.0.1:21')
    assert out['code'] != 200

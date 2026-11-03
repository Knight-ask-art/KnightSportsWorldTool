"""Bridge wiring for P0 features; no network calls."""

import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from replica.adapters.bridge_api import ReplicaApi  # noqa: E402
from replica.domain.rundata import generate_run_data  # noqa: E402


def _api(tmp_path):
    return ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                      str(tmp_path))


def test_audit_last_run(tmp_path):
    api = _api(tmp_path)
    api._last_run = generate_run_data(
        1.0, 6.0, 150, center_lat=22.4108, center_lng=112.9408,
        rng=random.Random(2))
    out = api.real_audit_run()
    assert out['code'] == 200
    assert out['data']['summary']['ok'] is True


def test_audit_with_preview_parameters(tmp_path):
    api = _api(tmp_path)
    out = api.real_audit_run(
        km=1.0, pace=6.0, cadence=150,
        waypoints=[[22.4108, 112.9408], [22.4118, 112.9418],
                   [22.4100, 112.9410]],
        closed=True, days_ago=1, seed=4)
    assert out['code'] == 200, out
    assert out['data']['summary']['ok'] is True


def test_audit_read_failure_is_a_bridge_failure(tmp_path, monkeypatch):
    from replica.application import readonly
    api = _api(tmp_path)
    api._real_account = lambda: object()
    monkeypatch.setattr(readonly, 'record_detail',
                        lambda *a, **k: {'ok': False, 'error': 'detail down'})
    out = api.real_audit_run(rrid=7)
    assert out['code'] != 200
    assert 'detail down' in out['message']


def test_obs_key_mode_read_reports_unsaved(tmp_path):
    out = _api(tmp_path).real_obs_key_mode()
    assert out['data']['saved'] is False


def test_audit_without_run_is_explicit(tmp_path):
    out = _api(tmp_path).real_audit_run()
    assert out['code'] != 200
    assert '还没有生成数据' in out['message']


def test_explain_error(tmp_path):
    out = _api(tmp_path).real_explain_error(11016, '不在有效跑步时间')
    assert out['code'] == 200
    assert out['data']['id'] == 'run-window'


def test_obs_key_mode_roundtrip(tmp_path):
    api = _api(tmp_path)
    assert api.real_obs_key_mode()['data']['mode'] == 'single'
    assert api.real_obs_key_mode('legacy')['data']['mode'] == 'legacy'
    assert api.real_obs_key_mode()['data']['mode'] == 'legacy'
    assert api.real_obs_key_mode('bad')['code'] != 200


def test_realism_profile_roundtrip(tmp_path):
    api = _api(tmp_path)
    out = api.real_obs_key_mode(pause_profile='auto', gps_profile='tunnel',
                                terrain_coupling=True)
    assert out['data']['pause_profile'] == 'auto'
    got = api.real_obs_key_mode()['data']
    assert got['pause_profile'] == 'auto'
    assert got['gps_profile'] == 'tunnel'
    assert got['terrain_coupling'] is True

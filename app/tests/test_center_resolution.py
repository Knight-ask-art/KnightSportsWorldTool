"""Campus centre resolution.

The submit coordinate prefers the account's real geofence.  A built-in city
fallback is still forbidden: a record in the wrong city cannot be undone.
Schools without a fence may use an operator-set centre or the first waypoint.
"""

import json

import pytest

from replica.adapters.bridge_api import ReplicaApi
from replica.application import device_profile as DP
from replica.transport.realclient import Account

FENCE = [
    (22.4000, 112.9300), (22.4000, 112.9500),
    (22.4200, 112.9500), (22.4200, 112.9300),
]
POLICY = {'policy': 1, 'geoFence': {'geoFences': [
    {'id': 1, 'name': '学校',
     'points': [{'glat': a, 'glon': b} for a, b in FENCE]}]}}


def _api(tmp_path, **session):
    data = {'uid': '1', 'token': 'tok', 'unid': 47203,
            'campusName': '广州软件学院 江门校区'}
    data.update(session)
    DP.save_profile(str(tmp_path), DP.generate_profile(platform='android'))
    (tmp_path / 'accounts_emu.json').write_text(
        json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                      str(tmp_path))


class FakeClient:
    def __init__(self, fence=None, fail=False):
        self.fence = FENCE if fence is None else fence
        self.fail = fail
        self.calls = []

    def request(self, acct, path, payload=None, method='POST', **kw):
        self.calls.append(path)
        if self.fail:
            return {'json': None, 'status': 500, 'error': 'boom'}
        return {'json': {'error': 10000, 'data': {'geoFences': [
            {'name': '学校', 'points': [{'glat': a, 'glon': b}
                                        for a, b in self.fence]}]}}}


def test_account_has_no_builtin_centre():
    acct = Account(name='x', uid='1', token='t')
    assert (acct.center_lat, acct.center_lng) == (0.0, 0.0)


def test_session_without_coordinates_stays_unset(tmp_path):
    acct = _api(tmp_path)._real_account()
    assert (acct.center_lat, acct.center_lng) == (0.0, 0.0)


def test_ensure_fence_adopts_server_centre(tmp_path):
    api = _api(tmp_path)
    acct = api._real_account()
    fence = api._ensure_fence(acct, FakeClient(), policy_data=POLICY)
    lat, lng = fence['center']
    assert abs(acct.center_lat - lat) < 1e-9
    assert abs(acct.center_lng - lng) < 1e-9
    saved = json.loads((tmp_path / 'accounts_emu.json').read_text('utf-8'))
    assert saved['lat'] == round(lat, 6)
    assert saved['lon'] == round(lng, 6)


def test_ensure_fence_refuses_empty_fence(tmp_path):
    api = _api(tmp_path)
    acct = api._real_account()
    with pytest.raises(RuntimeError):
        api._ensure_fence(acct, FakeClient(fence=[]),
                          policy_data={'policy': 1,
                                       'geoFence': {'geoFences': []}})
    saved = json.loads((tmp_path / 'accounts_emu.json').read_text('utf-8'))
    assert 'lat' not in saved and 'lon' not in saved


def test_fetch_points_without_fence_or_coordinates_fails(tmp_path, monkeypatch):
    from replica.transport import realclient as R
    monkeypatch.setattr(R, 'RealClient',
                        lambda *a, **k: FakeClient(fail=True))
    r = _api(tmp_path).real_fetch_points()
    assert r['code'] == 500
    assert '围栏' in r['message']


def test_preview_model_requires_real_coordinates(tmp_path):
    r = _api(tmp_path).real_preview_model()
    assert r['code'] == 500
    assert '坐标' in r['message']


def test_fetch_points_persists_the_resolved_centre(tmp_path, monkeypatch):
    from replica.transport import realclient as R
    monkeypatch.setattr(R, 'RealClient', lambda *a, **k: FakeClient())
    r = _api(tmp_path).real_fetch_points()
    assert r['code'] == 200
    saved = json.loads((tmp_path / 'accounts_emu.json').read_text('utf-8'))
    assert saved['lat'] == 22.41
    assert saved['lon'] == 112.94


def test_submit_stops_before_receipt_when_fence_is_unavailable(
        tmp_path, monkeypatch):
    from replica.transport import realclient as R
    api = _api(tmp_path)
    monkeypatch.setattr(R, 'RealClient',
                        lambda *a, **k: FakeClient(fail=True))
    monkeypatch.setattr(api, '_fetch_run_policy', lambda *a, **k: {
        'policy': 1, 'geoFence': {'geoFences': []}})
    r = api.real_submit_run()
    assert r['code'] == 500
    assert '围栏' in r['message']
    receipts = tmp_path / 'submission_receipts'
    assert not receipts.exists() or not list(receipts.iterdir())

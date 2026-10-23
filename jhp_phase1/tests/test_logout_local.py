"""Logout must always end the local session (“随登随退”)."""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from replica.transport import realclient  # noqa: E402
from replica.replica_api import ReplicaApi  # noqa: E402


def test_logout_uses_the_official_v6_endpoint():
    # The APK's extracted constant is /api/v6/user/logout; v70100 is a 404.
    assert realclient.LOGOUT_URL.endswith('/api/v6/user/logout')


def _api(tmp_path):
    return ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                      str(tmp_path))


def _login(api):
    return api.real_set_session(
        uid='u-1', token='tok-1', name='tester', campusName='江门校区',
        schoolName='广州软件学院', unid=47203, DeviceId='dev-1')


def test_failed_server_verification_still_clears_local_session(tmp_path,
                                                               monkeypatch):
    api = _api(tmp_path)
    _login(api)

    class FailingClient:
        def logout_and_verify(self, acct):
            return {'ok': False, 'stage': 'verify',
                    'logout': {'status': 200}, 'verify': {'status': 200}}

    monkeypatch.setattr(realclient, 'RealClient', FailingClient)
    out = api.real_logout()
    assert out['code'] == 200
    assert out['data']['verified'] is False
    assert out['data']['local_only'] is True
    assert out['data']['warning']
    session = api._read_session()
    assert 'token' not in session and 'uid' not in session
    assert 'name' not in session and 'campusName' not in session
    assert session['keepalive']['enabled'] is False


def test_verified_logout_reports_proof(tmp_path, monkeypatch):
    api = _api(tmp_path)
    _login(api)

    class GoodClient:
        def logout_and_verify(self, acct):
            return {'ok': True, 'stage': 'verified',
                    'logout': {'status': 200}, 'verify': {'status': 500}}

    monkeypatch.setattr(realclient, 'RealClient', GoodClient)
    out = api.real_logout()
    assert out['code'] == 200
    assert out['data']['verified'] is True
    assert out['data']['stage'] == 'verified'
    assert api._read_session().get('token') is None


def test_logout_without_session_is_a_clean_noop(tmp_path):
    api = _api(tmp_path)
    out = api.real_logout()
    assert out['code'] == 200
    assert out['data']['local_only'] is True


def test_local_only_logout_returns_before_network(tmp_path, monkeypatch):
    api = _api(tmp_path)
    _login(api)
    calls = []

    class SlowClient:
        def logout_and_verify(self, acct):
            calls.append(acct.uid)
            return {'ok': False, 'stage': 'still-alive'}

    monkeypatch.setattr(realclient, 'RealClient', SlowClient)
    out = api.real_logout(local_only=True)
    assert out['code'] == 200
    assert out['data']['local_only'] is True
    assert 'token' not in api._read_session()
    # The background thread is allowed to race; joining it would defeat the
    # point of “local first”.  Just make sure it was scheduled.
    for _ in range(50):
        if calls:
            break
        import time
        time.sleep(0.01)
    assert calls == ['u-1']

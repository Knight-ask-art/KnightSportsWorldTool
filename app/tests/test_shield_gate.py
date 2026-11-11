"""Login-entry gate for the four-layer device credential.

The slider tests in ``test_gt4_flow`` stub ``_shield_block`` away; these tests
are the other half: every real login entry point must return the fixed 403
payload, and the keep-alive re-login must raise, when the shield blocks.
No native binary is started.
"""
from __future__ import annotations

import pytest

from replica.adapters.bridge_api import ReplicaApi
from replica.application import shield, tokenkeeper


@pytest.fixture
def blocked(monkeypatch):
    payload = {'blocked': True, 'message': shield.SHIELD_MESSAGE}
    monkeypatch.setattr(shield, 'require', lambda store: dict(payload))
    return payload


def _api(tmp_path):
    # Only the surface the three login entries touch before the gate.
    api = ReplicaApi.__new__(ReplicaApi)
    api.store_dir = str(tmp_path)
    return api


def _assert_shield_failure(result):
    assert result == {
        'code': 403,
        'message': shield.SHIELD_MESSAGE,
        'data': {'reason': 'shield'},
    }


def test_real_login_is_refused(tmp_path, blocked):
    api = _api(tmp_path)
    _assert_shield_failure(ReplicaApi.real_login(api, '13800000000', 'pw'))


def test_real_login_ticket_is_refused(tmp_path, blocked):
    api = _api(tmp_path)
    result = ReplicaApi.real_login_ticket(
        api, '13800000000', 'pw', lotNumber='lot', captchaOutput='co',
        passToken='pt', genTime='1700000000')
    _assert_shield_failure(result)


def test_real_gt4_manual_finish_is_refused(tmp_path, blocked):
    api = _api(tmp_path)
    result = ReplicaApi.real_gt4_manual_finish(
        api, 'manual-token', 40, '13800000000', 'pw')
    _assert_shield_failure(result)


def test_tokenkeeper_relogin_raises_the_fixed_message(tmp_path, blocked):
    with pytest.raises(RuntimeError) as excinfo:
        tokenkeeper.relogin_real('13800000000', 'pw', store_dir=str(tmp_path))
    assert str(excinfo.value) == shield.SHIELD_MESSAGE

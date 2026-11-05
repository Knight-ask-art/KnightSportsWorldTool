# -*- coding: utf-8 -*-
"""Offline tests for replica.tokenkeeper.

No network, no real account: probe/relogin are injected fakes.
"""
import json

from replica.application import session_store
from replica.application import tokenkeeper as T


def _write(path, data):
    T._write_json(str(path), data)


def _read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def test_probe_updates_state(tmp_path):
    path = tmp_path / 'accounts_emu.json'
    _write(path, {'uid': 'u1', 'token': 'tok-old', 'keepalive': {}})
    now = [1000]

    k = T.TokenKeeper(str(path), probe=lambda s: {'ok': True},
                      interval_s=3600, now=lambda: now[0])
    res = k.run_once()
    state = _read(path)['keepalive']
    assert res['ok'] is True
    assert res['action'] == 'probe'
    assert state['last_ok_at'] == 1000
    assert state['fail_count'] == 0
    assert state['token_expired'] is False


def test_auth_failure_marks_expired(tmp_path):
    path = tmp_path / 'accounts_emu.json'
    _write(path, {'uid': 'u1', 'token': 'tok-old', 'keepalive': {}})
    calls = {'relogin': 0}

    def relogin(user, password):
        calls['relogin'] += 1
        return {'uid': 'u1', 'token': 'tok-new'}

    k = T.TokenKeeper(
        str(path),
        probe=lambda s: {'ok': False, 'auth_failed': True, 'status': 401},
        relogin=relogin, interval_s=3600, now=lambda: 2000)
    res = k.run_once()
    state = _read(path)['keepalive']
    assert res['ok'] is False
    assert state['token_expired'] is True
    assert state['fail_count'] == 1
    assert calls['relogin'] == 0


def test_auto_relogin_rotates_token(tmp_path):
    path = tmp_path / 'accounts_emu.json'
    _write(path, {'uid': 'u1', 'token': 'tok-old', 'keepalive': {}})

    def relogin(user, password):
        assert user == 'u1'
        assert password == 'secret-pass'
        return {'uid': 'u1', 'token': 'tok-new',
                'profile': {'name': 'test'}}

    k = T.TokenKeeper(
        str(path),
        probe=lambda s: {'ok': False, 'auth_failed': True, 'status': 401},
        relogin=relogin, interval_s=3600, auto_relogin=True,
        username='u1', password='secret-pass', now=lambda: 3000)
    res = k.run_once(force=True)
    data = _read(path)
    plain = session_store.read(str(path))
    assert res['ok'] is True
    assert res['action'] == 'relogin'
    # The token is DPAPI-sealed at rest; the reader gets it back in memory.
    assert 'token' not in data and data.get('token_enc')
    assert plain['token'] == 'tok-new'
    assert data['keepalive']['token_expired'] is False
    assert 'secret-pass' not in json.dumps(data)


def test_disabled_is_never_probed(tmp_path):
    path = tmp_path / 'accounts_emu.json'
    _write(path, {'uid': 'u1', 'token': 'tok-old',
                  'disabled': True, 'keepalive': {}})
    calls = {'probe': 0}

    def probe(session):
        calls['probe'] += 1
        return {'ok': True}

    k = T.TokenKeeper(str(path), probe=probe, now=lambda: 4000)
    res = k.run_once()
    assert res['action'] == 'skipped'
    assert calls['probe'] == 0
    assert k.status()['disabled'] is True


def test_status_has_no_secrets(tmp_path):
    path = tmp_path / 'accounts_emu.json'
    _write(path, {'uid': 'u1', 'token': 'tok-secret', 'keepalive': {}})
    k = T.TokenKeeper(str(path), probe=lambda s: {'ok': True},
                      username='u1', password='secret-pass',
                      now=lambda: 5000)
    blob = json.dumps(k.status(), ensure_ascii=False)
    assert 'tok-secret' not in blob
    assert 'secret-pass' not in blob
    assert 'u1' not in blob


def test_real_probe_accepts_and_rejects_without_network(tmp_path):
    from replica.application import device_profile as DP
    DP.save_profile(str(tmp_path), DP.generate_profile(platform='android'))

    class Fake:
        def __init__(self, payload):
            self.payload = payload

        def request(self, *a, **k):
            return self.payload

    session = {'uid': 'u1', 'token': 'tok', 'unid': 47203}
    ok = T.probe_real_session(
        session, client_factory=lambda: Fake({
            'status': 200, 'json': {'error': 10000, 'data': {}},
            'plain': '{}', 'error': None}), store_dir=str(tmp_path))
    assert ok['ok'] is True

    dead = T.probe_real_session(
        session, client_factory=lambda: Fake({
            'status': 401, 'json': None, 'plain': None,
            'error': 'decode failed'}), store_dir=str(tmp_path))
    assert dead['ok'] is False
    assert dead['auth_failed'] is True

    hiccup = T.probe_real_session(
        session, client_factory=lambda: Fake({
            'status': 200, 'json': None, 'plain': None,
            'error': 'decode failed'}), store_dir=str(tmp_path))
    assert hiccup['ok'] is False
    assert hiccup['auth_failed'] is False


def test_interval_is_clamped(tmp_path):
    path = tmp_path / 'accounts_emu.json'
    _write(path, {'uid': 'u1', 'token': 'tok', 'keepalive': {}})
    k = T.TokenKeeper(str(path), probe=lambda s: {'ok': True},
                      interval_s=1, now=lambda: 0)
    assert k.interval_s == T.MIN_INTERVAL_S
    k2 = T.TokenKeeper(str(path), probe=lambda s: {'ok': True},
                       interval_s=10 ** 9, now=lambda: 0)
    assert k2.interval_s == T.MAX_INTERVAL_S

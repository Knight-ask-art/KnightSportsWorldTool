"""v0.1.7 hardening: token at rest, per-account UI prefs, updater transport.

These are the three review findings that are cheap to regress silently:
a plaintext token in `accounts_emu.json`, a run-area choice that only lives in
memory, and an updater that accepted a zip without a hash over plain http.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from replica.adapters.bridge_api import ReplicaApi  # noqa: E402
from replica.application import session_store as SESS  # noqa: E402
from replica.application import updater as U  # noqa: E402


def _api(tmp_path):
    return ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                      str(tmp_path))


@pytest.mark.windows_only
def test_session_token_is_sealed_at_rest(tmp_path):
    path = tmp_path / 'accounts_emu.json'
    SESS.write(str(path), {'uid': '1', 'token': 'tok-123',
                           'keepalive': {'enabled': True}})
    raw = json.loads(path.read_text(encoding='utf-8'))
    assert 'token' not in raw
    assert raw['token_enc']
    assert raw['uid'] == '1'                 # metadata stays readable
    assert SESS.read(str(path))['token'] == 'tok-123'


@pytest.mark.windows_only
def test_legacy_plaintext_token_is_migrated(tmp_path):
    path = tmp_path / 'accounts_emu.json'
    path.write_text(json.dumps({'uid': '1', 'token': 'tok-old'}),
                    encoding='utf-8')
    assert SESS.migrate(str(path)) is True
    raw = json.loads(path.read_text(encoding='utf-8'))
    assert 'token' not in raw and raw['token_enc']
    assert SESS.read(str(path))['token'] == 'tok-old'


@pytest.mark.windows_only
def test_bridge_writes_only_the_sealed_token(tmp_path):
    api = _api(tmp_path)
    api.real_set_session(uid='1', token='tok-x', unid=9)
    raw = json.loads((tmp_path / 'accounts_emu.json').read_text(
        encoding='utf-8'))
    assert 'token' not in raw and raw['token_enc']
    assert api._read_session()['token'] == 'tok-x'
    # The browser bridge must not hand the sealed blob (or the plaintext) to
    # the webview; it only needs to know that a token exists.
    public = api.real_get_session()['data']
    assert 'token' not in public and 'token_enc' not in public
    assert public['token_present'] is True


def test_ui_state_keeps_disclaimer_acceptance(tmp_path):
    api = _api(tmp_path)
    assert api.real_get_ui_state()['data']['disclaimer_version'] == 0

    assert api.real_set_ui_state(disclaimer_version=1)['code'] == 200
    state = api.real_get_ui_state()['data']
    assert state['disclaimer_version'] == 1
    assert state['disclaimer_accepted_at']

    # Withdrawing acceptance clears both fields, not just the version.
    assert api.real_set_ui_state(disclaimer_version=0)['code'] == 200
    state = api.real_get_ui_state()['data']
    assert state['disclaimer_version'] == 0
    assert state['disclaimer_accepted_at'] == ''

    assert api.real_set_ui_state(disclaimer_version='bad')['code'] != 200
    assert api.real_set_ui_state(disclaimer_version=True)['code'] != 200


def test_ui_state_keeps_announcement_seen(tmp_path):
    api = _api(tmp_path)
    assert api.real_get_ui_state()['data']['announcement_seen'] == {}

    assert api.real_set_ui_state(
        announcement_seen={'qq-group-2026-11': 1})['code'] == 200
    assert api.real_get_ui_state()['data']['announcement_seen'] == {
        'qq-group-2026-11': 1}

    # Removing one notice must not clear the others.
    api.real_set_ui_state(announcement_seen={'other': 2})
    api.real_set_ui_state(announcement_seen={'qq-group-2026-11': 0})
    assert api.real_get_ui_state()['data']['announcement_seen'] == {'other': 2}

    assert api.real_set_ui_state(announcement_seen='bad')['code'] != 200
    assert api.real_set_ui_state(
        announcement_seen={'x': True})['code'] != 200
    assert api.real_set_ui_state(
        announcement_seen={'x': False})['code'] != 200
    assert api.real_set_ui_state(
        announcement_seen={'x': -1})['code'] != 200


def test_announcements_catalog_and_bridge(tmp_path):
    from replica.application import announcements as A

    items = A.current()
    qq = next(i for i in items if i['id'] == 'qq-group-2026-11')
    assert qq['version'] == 1
    assert qq['image'] == 'assets/qq-group.jpg'
    assert qq['note'] == '加群暗号：皮卡丘'
    assert 'copy' not in qq and 'meta' not in qq
    assert A.by_id('qq-group-2026-11')['title'] == qq['title']
    assert A.by_id('missing') is None

    api = _api(tmp_path)
    payload = api.real_get_announcements()
    assert payload['code'] == 200
    assert payload['data']['seen'] == {}
    assert any(i['id'] == 'qq-group-2026-11'
               for i in payload['data']['items'])


def test_ui_state_keeps_run_area_per_account(tmp_path):
    api = _api(tmp_path)
    res = api.real_set_ui_state(
        run_area_choice={'1:9': 22, '2:9': 33})
    assert res['code'] == 200
    api.real_set_ui_state(run_area_choice={'1:9': 44})
    state = api.real_get_ui_state()['data']
    assert state['run_area_choice'] == {'1:9': 44, '2:9': 33}
    api.real_set_ui_state(run_area_choice={'1:9': None})
    assert api.real_get_ui_state()['data']['run_area_choice'] == {'2:9': 33}


def _manifest(asset):
    return {'schema': 1, 'app': 'haoxiangpao', 'version': '9.9.9',
            'assets': {'windows-x64': asset}}


def test_zip_without_a_hash_is_refused():
    with pytest.raises(ValueError, match='sha256'):
        U.normalize(_manifest({'file': 'a.zip', 'url': 'https://e/a.zip'}),
                    'https://e/latest.json')


def test_plain_http_asset_is_refused():
    with pytest.raises(ValueError, match='https'):
        U.normalize(_manifest({'file': 'a.zip', 'url': 'http://e/a.zip',
                               'sha256': '0' * 64}),
                    'https://e/latest.json')


def test_loopback_http_is_allowed_for_tests():
    info = U.normalize(
        _manifest({'file': 'a.zip', 'url': 'http://127.0.0.1:8123/a.zip',
                   'sha256': '0' * 64}),
        'http://127.0.0.1:8123/latest.json')
    assert info['url'].endswith('/a.zip')
    assert info['sha256'] == '0' * 64


def test_single_instance_mutex(monkeypatch):
    if os.name != 'nt':
        pytest.skip('Windows named mutex')
    import app as entry

    monkeypatch.setattr(entry, '_focus_existing', lambda title: None)
    name = 'Local\\Haoxiangpao.Test.%d' % os.getpid()
    try:
        assert entry._acquire_single_instance('test', name) is True
        assert entry._acquire_single_instance('test', name) is False
    finally:
        if entry._MUTEX:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(entry._MUTEX)
            entry._MUTEX = None

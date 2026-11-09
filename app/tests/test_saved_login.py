"""The login page may remember the password only through DPAPI.

These guards exist because the old settings panel wrote ``account.json`` as
plaintext.  A future "convenience" change must not reintroduce that fallback.
"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from replica.application import credentials  # noqa: E402
from replica.adapters.bridge_api import ReplicaApi  # noqa: E402


pytestmark = pytest.mark.windows_only

ACCOUNT = '13800138000'
PASSWORD = 'p@ss-中文-123'


def _api(tmp_path):
    return ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                      str(tmp_path))


def test_dpapi_roundtrip_writes_no_plaintext(tmp_path):
    credentials.save(str(tmp_path), ACCOUNT, PASSWORD)
    raw = (tmp_path / credentials.FILENAME).read_bytes()
    assert ACCOUNT.encode() not in raw
    assert PASSWORD.encode() not in raw
    doc = json.loads(raw.decode('utf-8'))
    assert doc['scheme'] == 'dpapi'
    assert credentials.load(str(tmp_path)) == {
        'account': ACCOUNT, 'password': PASSWORD}


def test_clear_removes_saved_and_legacy_files(tmp_path):
    credentials.save(str(tmp_path), ACCOUNT, PASSWORD)
    (tmp_path / credentials.LEGACY_FILENAME).write_text(
        json.dumps({'account': ACCOUNT, 'password': PASSWORD}),
        encoding='utf-8')
    assert credentials.clear(str(tmp_path)) is True
    assert not (tmp_path / credentials.FILENAME).exists()
    assert not (tmp_path / credentials.LEGACY_FILENAME).exists()


def test_corrupt_file_is_ignored_not_crashing(tmp_path):
    (tmp_path / credentials.FILENAME).write_text('{"scheme":"dpapi",'
                                                 '"blob":"not-base64!"}',
                                                 encoding='utf-8')
    assert credentials.load(str(tmp_path)) is None


def test_save_fails_closed_when_dpapi_is_unavailable(tmp_path, monkeypatch):
    def no_dpapi():
        raise credentials.CredentialError('unavailable')

    monkeypatch.setattr(credentials, '_api', no_dpapi)
    with pytest.raises(credentials.CredentialError):
        credentials.save(str(tmp_path), ACCOUNT, PASSWORD)
    assert not (tmp_path / credentials.FILENAME).exists()


def test_bridge_save_get_and_clear(tmp_path):
    api = _api(tmp_path)
    out = api.save_login(ACCOUNT, PASSWORD)
    assert out['code'] == 200, out
    assert out['data']['saved'] is True

    out = api.get_saved_login()
    assert out['code'] == 200, out
    assert out['data'] == {'saved': True, 'account': ACCOUNT,
                           'password': PASSWORD}

    out = api.clear_saved_login()
    assert out['code'] == 200, out
    assert out['data'] == {'saved': False}
    assert api.get_saved_login()['data']['saved'] is False


def test_bridge_remember_false_clears(tmp_path):
    api = _api(tmp_path)
    api.save_login(ACCOUNT, PASSWORD)
    out = api.save_login('', '', remember=False)
    assert out['code'] == 200, out
    assert out['data']['saved'] is False
    assert not (tmp_path / credentials.FILENAME).exists()


def test_legacy_plaintext_is_migrated_and_deleted(tmp_path):
    api = _api(tmp_path)
    legacy = tmp_path / credentials.LEGACY_FILENAME
    legacy.write_text(json.dumps({'account': ACCOUNT, 'password': PASSWORD}),
                      encoding='utf-8')

    out = api.get_account_and_password()
    assert out['code'] == 200, out
    assert out['data'] == {'account': ACCOUNT, 'password': PASSWORD}
    assert not legacy.exists()
    raw = (tmp_path / credentials.FILENAME).read_bytes()
    assert PASSWORD.encode() not in raw


def test_legacy_plaintext_is_migrated_at_startup(tmp_path):
    legacy = tmp_path / credentials.LEGACY_FILENAME
    legacy.write_text(json.dumps({'account': ACCOUNT, 'password': PASSWORD}),
                      encoding='utf-8')

    api = _api(tmp_path)
    assert not legacy.exists()
    out = api.get_saved_login()
    assert out['data'] == {'saved': True, 'account': ACCOUNT,
                           'password': PASSWORD}

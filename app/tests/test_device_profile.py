"""Per-installation device profile regression tests."""

import json
import os
import re

import pytest

from replica.application import device_profile as DP
from replica.transport.realclient import Account, build_headers

ACCOUNT = '13800138000'
PASSWORD = 'p@ss-中文-123'


def _session(tmp_path, **extra):
    data = {'uid': '15663170', 'token': 'tok-test', 'unid': 47203,
            'campusName': '广州软件学院 江门校区'}
    data.update(extra)
    with open(tmp_path / 'accounts_emu.json', 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    return data


def test_generated_profile_is_stable(tmp_path):
    first = DP.load_or_create(str(tmp_path))
    second = DP.load_or_create(str(tmp_path))
    assert first == second
    assert re.fullmatch(r'[0-9a-f]{32}', first['DeviceId'])
    assert first['platform'] == 'android'
    assert first['isRoot'] == '0'
    assert first['cpuModel'] == 'arm64-v8a'
    assert (tmp_path / DP.PROFILE_FILENAME).exists()


def test_two_installs_get_different_ids(tmp_path):
    a = DP.load_or_create(str(tmp_path / 'a'))
    b = DP.load_or_create(str(tmp_path / 'b'))
    assert a['DeviceId'] != b['DeviceId']


def test_generated_profile_fields_are_coherent(tmp_path):
    profile = DP.generate_profile(now_ms=1_792_800_000_000)
    assert profile['deviceName'] in {m for m, _ in DP.ANDROID_MODELS}
    allowed = dict(DP.ANDROID_MODELS)[profile['deviceName']]
    assert profile['osVersion'] in allowed
    assert profile['isRoot'] == '0'
    assert re.fullmatch(r'\d{3,5}x\d{3,5}', profile['physicPixel'])
    assert re.fullmatch(r'\d{3,5}x\d{3,5}', profile['logicPixel'])
    assert int(profile['appInstallTime']) <= int(profile['appUpdateTime'])
    assert int(profile['appUpdateTime']) <= 1_792_800_000_000


def test_legacy_session_freezes_current_submission_fingerprint(tmp_path):
    session = _session(tmp_path)
    profile = DP.load_or_create(str(tmp_path), legacy_session=session)
    assert profile['source'] == 'legacy-default'
    assert profile['DeviceId'] == DP.LEGACY_DEVICE['DeviceId']
    assert profile['deviceName'] == 'Xiaomi 2304FPN6DG'
    assert profile['osVersion'] == '9'
    assert profile['cpuModel'] == 'x86_64'
    assert profile['isRoot'] == '1'


def test_legacy_session_with_explicit_device_id_is_preserved(tmp_path):
    session = _session(tmp_path, DeviceId='dev-secret-123456')
    profile = DP.load_or_create(str(tmp_path), legacy_session=session)
    assert profile['source'] == 'legacy-session'
    assert profile['DeviceId'] == 'dev-secret-123456'


def test_session_override_wins_over_install_profile(tmp_path):
    profile = DP.load_or_create(str(tmp_path))
    device = DP.effective_device(profile, {
        'DeviceId': 'manual-device-1', 'deviceName': 'Manual Phone',
        'osVersion': '12',
    })
    assert device['DeviceId'] == 'manual-device-1'
    assert device['deviceName'] == 'Manual Phone'
    assert device['osVersion'] == '12'
    assert device['source'] == 'session'


def test_rotate_backs_up_and_changes(tmp_path):
    old = DP.load_or_create(str(tmp_path))
    new, backup = DP.rotate(str(tmp_path), force=True)
    assert new['DeviceId'] != old['DeviceId']
    assert new['source'] == 'rotated'
    assert new['rotated_from'] == 'generated'
    assert backup and os.path.exists(backup)
    with open(backup, encoding='utf-8') as f:
        saved = json.load(f)
    assert saved['DeviceId'] == old['DeviceId']
    assert DP.load_or_create(str(tmp_path))['DeviceId'] == new['DeviceId']


def test_rotate_refuses_without_force(tmp_path):
    DP.load_or_create(str(tmp_path))
    with pytest.raises(DP.DeviceProfileError):
        DP.rotate(str(tmp_path))


def test_rotate_legacy_session_freezes_then_replaces(tmp_path):
    session = _session(tmp_path)
    new, backup = DP.rotate(str(tmp_path), legacy_session=session, force=True)
    assert new['source'] == 'rotated'
    assert new['rotated_from'] == 'legacy-default'
    assert new['DeviceId'] != DP.LEGACY_DEVICE['DeviceId']
    with open(backup, encoding='utf-8') as f:
        saved = json.load(f)
    assert saved['DeviceId'] == DP.LEGACY_DEVICE['DeviceId']


def test_corrupt_profile_fails_closed(tmp_path):
    path = tmp_path / DP.PROFILE_FILENAME
    path.write_text('{not json', encoding='utf-8')
    with pytest.raises(DP.DeviceProfileError):
        DP.load_or_create(str(tmp_path))
    assert path.read_text(encoding='utf-8') == '{not json'


def test_invalid_profile_field_fails_closed(tmp_path):
    path = tmp_path / DP.PROFILE_FILENAME
    path.write_text(json.dumps({'DeviceId': 'x' * 32, 'deviceName': 'Phone',
                                'osVersion': '12', 'cpuModel': 'arm64-v8a',
                                'physicPixel': '1080x2400',
                                'logicPixel': '1080x2400',
                                'isRoot': 'yes',
                                'appInstallTime': '1',
                                'appUpdateTime': '1'}), encoding='utf-8')
    with pytest.raises(DP.DeviceProfileError):
        DP.read_profile(str(tmp_path))


def test_login_and_submit_headers_share_the_same_device(tmp_path):
    profile = DP.load_or_create(str(tmp_path))
    login_account = Account(name=ACCOUNT, uid='-1', token='',
                            DeviceId='', deviceName='', osVersion='',
                            cpuModel='', physicPixel='', logicPixel='',
                            appInstallTime='', appUpdateTime='')
    submit_account = Account(name=ACCOUNT, uid='15663170', token='tok-test',
                             DeviceId='', deviceName='', osVersion='',
                             cpuModel='', physicPixel='', logicPixel='',
                             appInstallTime='', appUpdateTime='')
    DP.apply_to_account(login_account, profile)
    DP.apply_to_account(submit_account, profile)
    login_headers = build_headers(login_account, '1792828000000', 'nonce')
    submit_headers = build_headers(submit_account, '1792828000000', 'nonce')
    for key in ('DeviceId', 'deviceName', 'osVersion', 'cpuModel',
                'physicPixel', 'logicPixel', 'isRoot', 'appInstallTime',
                'appUpdateTime'):
        assert login_headers[key] == submit_headers[key], key


def test_public_view_never_returns_raw_device_id(tmp_path):
    session = _session(tmp_path)
    profile = DP.load_or_create(str(tmp_path), legacy_session=session)
    view = DP.public_view(profile, session)
    blob = json.dumps(view, ensure_ascii=False)
    assert profile['DeviceId'] not in blob
    assert view['device_id_masked'].startswith(profile['DeviceId'][:4])
    assert view['device_source'] == 'legacy-default'

def test_bridge_public_session_uses_install_profile(tmp_path):
    from replica.adapters.bridge_api import ReplicaApi
    _session(tmp_path)
    api = ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                     str(tmp_path))
    data = api.real_get_session()['data']
    assert data['device_id_present'] is True
    assert data['device_source'] == 'legacy-default'
    assert data['device_name'] == 'Xiaomi 2304FPN6DG'
    assert data['device_os'] == '9'
    assert 'DeviceId' not in data
    for key in ('DeviceId', 'deviceName', 'osVersion', 'cpuModel',
                'physicPixel', 'logicPixel', 'isRoot', 'appInstallTime',
                'appUpdateTime'):
        assert key not in data, key
    assert DP.LEGACY_DEVICE['DeviceId'] not in json.dumps(data)


def test_bridge_uses_rotated_profile_for_account(tmp_path):
    from replica.adapters.bridge_api import ReplicaApi
    _session(tmp_path)
    new, _ = DP.rotate(str(tmp_path),
                       legacy_session=DP.read_session(str(tmp_path)),
                       force=True)
    api = ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                     str(tmp_path))
    data = api.real_get_session()['data']
    assert data['device_source'] == 'rotated'
    assert data['device_name'] == new['deviceName']
    assert data['device_os'] == new['osVersion']
    acct = api._real_account()
    for key in ('DeviceId', 'deviceName', 'osVersion', 'cpuModel',
                'physicPixel', 'logicPixel', 'isRoot', 'appInstallTime',
                'appUpdateTime'):
        assert getattr(acct, key) == new[key], key

def test_manual_session_device_survives_effective_device(tmp_path):
    from replica.adapters.bridge_api import ReplicaApi
    api = ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                     str(tmp_path))
    api.real_set_session(
        uid='1', token='tok', DeviceId='manual-device-1',
        deviceName='Manual Phone', osVersion='12',
        cpuModel='arm64-v8a', physicPixel='1080x2400',
        logicPixel='1080x2400', isRoot='0',
        appInstallTime='1790000000000', appUpdateTime='1790000000000')
    device = api._effective_device()
    assert device['DeviceId'] == 'manual-device-1'
    assert device['deviceName'] == 'Manual Phone'
    assert device['osVersion'] == '12'
    assert device['source'] == 'session'

def test_real_login_keeps_manual_device_override(tmp_path, monkeypatch):
    from replica.adapters.bridge_api import ReplicaApi
    from replica.application import login_flow
    api = ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                     str(tmp_path))
    api.real_set_session(
        uid='1', token='tok-old', DeviceId='manual-device-1',
        deviceName='Manual Phone', osVersion='12',
        cpuModel='arm64-v8a', physicPixel='1080x2400',
        logicPixel='1080x2400', isRoot='0',
        appInstallTime='1790000000000', appUpdateTime='1790000000000')
    captured = {}

    def fake_login(username, password, **kwargs):
        captured['device'] = kwargs.get('device')
        return {'uid': '1', 'token': 'tok-new', 'unid': 47203,
                'profile': {'name': 'Test'}}

    monkeypatch.setattr(login_flow, 'login', fake_login)
    out = api.real_login('u', 'p')
    assert out['code'] == 200
    assert captured['device']['DeviceId'] == 'manual-device-1'
    session = api._read_session()
    assert session['DeviceId'] == 'manual-device-1'
    assert session['deviceName'] == 'Manual Phone'
    assert session['token'] == 'tok-new'


def test_real_login_does_not_freeze_generated_profile_in_session(tmp_path,
                                                                 monkeypatch):
    from replica.adapters.bridge_api import ReplicaApi
    from replica.application import login_flow
    api = ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                     str(tmp_path))

    def fake_login(username, password, **kwargs):
        return {'uid': '1', 'token': 'tok-new', 'unid': 47203,
                'profile': {'name': 'Test'}}

    monkeypatch.setattr(login_flow, 'login', fake_login)
    out = api.real_login('u', 'p')
    assert out['code'] == 200
    session = api._read_session()
    assert 'DeviceId' not in session
    assert api._effective_device()['source'] == 'generated'

def test_rotate_repairs_corrupt_profile_and_keeps_bytes(tmp_path):
    path = tmp_path / DP.PROFILE_FILENAME
    path.write_text('{broken', encoding='utf-8')
    new, backup = DP.rotate(str(tmp_path), force=True)
    assert new['source'] == 'rotated'
    assert backup and backup.endswith('.corrupt.json')
    with open(backup, encoding='utf-8') as f:
        assert f.read() == '{broken'
    assert DP.read_profile(str(tmp_path))['DeviceId'] == new['DeviceId']


def test_rotate_repairs_corrupt_profile_with_legacy_session(tmp_path):
    _session(tmp_path)
    path = tmp_path / DP.PROFILE_FILENAME
    path.write_text('{broken', encoding='utf-8')
    new, backup = DP.rotate(str(tmp_path), legacy_session=DP.read_session(str(tmp_path)),
                            force=True)
    assert new['source'] == 'rotated'
    assert backup and backup.endswith('.corrupt.json')
    backups = list(tmp_path.glob('device_profile.backup-*'))
    assert len(backups) >= 2

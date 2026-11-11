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


def test_generated_profile_is_stable(tmp_path, monkeypatch):
    monkeypatch.setenv('JHP_IOS_CHANCE', '0')
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


def test_android_profile_is_coherent():
    profile = DP.generate_profile(now_ms=1_792_800_000_000,
                                  platform='android')
    row = next(r for r in DP.ANDROID_MODELS
               if r[0] == profile['deviceName'])
    _, versions, physic, logic = row
    assert profile['osVersion'] in versions
    assert profile['physicPixel'] == physic
    assert profile['logicPixel'] == logic
    assert profile['physicPixel'] != profile['logicPixel']
    assert profile['isRoot'] == '0'
    assert profile['cpuModel'] == 'arm64-v8a'
    assert not DP.audit_profile(profile)


def test_ios_profile_is_coherent():
    profile = DP.generate_profile(now_ms=1_792_800_000_000, platform='ios')
    row = next(r for r in DP.IOS_MODELS if r[0] == profile['deviceName'])
    _, versions, physic, logic = row
    assert profile['osVersion'] in versions
    assert profile['physicPixel'] == physic
    assert profile['logicPixel'] == logic
    assert profile['cpuModel'] == 'arm64e'
    assert profile['isRoot'] == '0'
    assert re.fullmatch(
        r'[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}',
        profile['DeviceId'])
    assert re.fullmatch(
        r'(?:[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12})',
        profile['idfa'])
    assert not DP.audit_profile(profile)


def test_ios_pool_can_be_disabled_and_forced(monkeypatch):
    monkeypatch.setenv('JHP_IOS_CHANCE', '0')
    assert all(DP.generate_profile()['platform'] == 'android'
               for _ in range(50))
    monkeypatch.setenv('JHP_IOS_CHANCE', '100')
    assert all(DP.generate_profile()['platform'] == 'ios'
               for _ in range(50))
    monkeypatch.setenv('JHP_IOS_CHANCE', 'not-a-number')
    assert DP.generate_profile()['platform'] in ('android', 'ios')


def test_legacy_session_is_migrated_not_frozen(tmp_path):
    session = _session(tmp_path)
    profile = DP.load_or_create_ex(str(tmp_path), legacy_session=session)[0]
    assert profile['source'] == 'migrated'
    assert profile['DeviceId'] != DP.LEGACY_DEVICE['DeviceId']
    assert profile['deviceName'] != 'Xiaomi 2304FPN6DG'
    assert profile['osVersion'] != '9'
    assert profile['cpuModel'] == 'arm64-v8a'
    assert profile['isRoot'] == '0'
    assert not DP.audit_profile(profile)
    assert DP.read_migration_marker(str(tmp_path))


def test_legacy_session_with_explicit_device_id_is_preserved(tmp_path):
    session = _session(tmp_path, DeviceId='dev-secret-123456')
    profile = DP.load_or_create(str(tmp_path), legacy_session=session)
    assert profile['source'] == 'legacy-session'
    assert profile['DeviceId'] == 'dev-secret-123456'


def test_shared_profile_file_is_migrated_with_backup(tmp_path):
    legacy = dict(DP.LEGACY_DEVICE)
    legacy.update(version=DP.PROFILE_VERSION, source='legacy-default',
                  created_at=1_792_800_000_000)
    DP._write_json_atomic(DP._profile_path(str(tmp_path)), legacy)

    profile, info = DP.load_or_create_ex(str(tmp_path))
    assert info and info['kind'] == 'shared-legacy-device'
    assert profile['DeviceId'] != DP.LEGACY_DEVICE['DeviceId']
    assert profile['source'] == 'migrated'
    assert not DP.audit_profile(profile)
    backup = tmp_path / info['backup']
    assert backup.exists()
    saved = json.loads(backup.read_text(encoding='utf-8'))
    assert saved['DeviceId'] == DP.LEGACY_DEVICE['DeviceId']
    assert DP.read_migration_marker(str(tmp_path))['kind'] == \
        'shared-legacy-device'

    # A second load is a no-op: the repaired profile stays put and no new
    # backup is written.
    again, info2 = DP.load_or_create_ex(str(tmp_path))
    assert info2 is None and again == profile
    assert len(list(tmp_path.glob('device_profile.backup-*'))) == 1


def test_unique_but_incoherent_profile_is_repaired_with_same_id(tmp_path):
    profile = DP.generate_profile(platform='android')
    profile['cpuModel'] = 'x86_64'
    profile['isRoot'] = '1'
    DP.save_profile(str(tmp_path), profile)

    fixed, info = DP.load_or_create_ex(str(tmp_path))
    assert info and info['kind'] == 'incoherent-profile'
    assert fixed['DeviceId'] == profile['DeviceId']
    assert fixed['cpuModel'] == 'arm64-v8a'
    assert fixed['isRoot'] == '0'
    assert fixed['source'] == 'repaired'
    assert not DP.audit_profile(fixed)
    assert (tmp_path / info['backup']).exists()


def test_save_profile_refuses_shared_legacy(tmp_path):
    legacy = dict(DP.LEGACY_DEVICE)
    legacy.update(version=DP.PROFILE_VERSION, source='legacy-default',
                  created_at=1_792_800_000_000)
    with pytest.raises(DP.DeviceProfileError):
        DP.save_profile(str(tmp_path), legacy)


def test_apply_to_account_refuses_shared_legacy():
    legacy = dict(DP.LEGACY_DEVICE)
    legacy.update(version=DP.PROFILE_VERSION, source='legacy-default',
                  created_at=1_792_800_000_000)
    with pytest.raises(DP.DeviceProfileError):
        DP.apply_to_account(Account(name='x', uid='1', token='t'), legacy)


def test_bridge_clears_old_token_and_clears_notice_after_login(
        tmp_path, monkeypatch):
    from replica.adapters.bridge_api import ReplicaApi
    from replica.application import login_flow
    _session(tmp_path)
    api = ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                     str(tmp_path))
    assert api._read_session().get('token') is None
    assert api.real_get_session()['data']['device_migration_notice']

    def fake_login(username, password, **kwargs):
        return {'uid': '1', 'token': 'tok-new', 'unid': 47203,
                'profile': {'name': 'Test'}}

    monkeypatch.setattr(login_flow, 'login', fake_login)
    out = api.real_login('u', 'p')
    assert out['code'] == 200
    assert not DP.read_migration_marker(str(tmp_path))
    assert api._read_session()['token'] == 'tok-new'
    assert not api.real_get_session()['data'].get('device_migration_notice')


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


def test_rotate_legacy_session_migrates_then_replaces(tmp_path):
    session = _session(tmp_path)
    new, backup = DP.rotate(str(tmp_path), legacy_session=session, force=True)
    assert new['source'] == 'rotated'
    assert new['rotated_from'] == 'migrated'
    assert new['DeviceId'] != DP.LEGACY_DEVICE['DeviceId']
    with open(backup, encoding='utf-8') as f:
        saved = json.load(f)
    assert saved['DeviceId'] != DP.LEGACY_DEVICE['DeviceId']
    assert saved['source'] == 'migrated'


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
    profile = DP.generate_profile(platform='android')
    DP.save_profile(str(tmp_path), profile)
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
    assert login_headers['osType'] == submit_headers['osType'] == '0'


def test_ios_login_and_submit_headers_share_the_same_device():
    profile = DP.generate_profile(platform='ios')
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
    for key in ('osType', 'DeviceId', 'deviceName', 'CustomDeviceId',
                'osVersion', 'IDFA', 'logicPixel', 'physicPixel',
                'cpuModel', 'appVersion', 'isRoot', 'appInstallTime'):
        assert login_headers[key] == submit_headers[key], key
    assert login_headers['osType'] == submit_headers['osType'] == '1'
    assert 'uid' not in login_headers
    assert submit_headers['uid'] == 15663170


def test_public_view_never_returns_raw_device_id(tmp_path):
    session = _session(tmp_path)
    profile = DP.load_or_create(str(tmp_path), legacy_session=session)
    view = DP.public_view(profile, session)
    blob = json.dumps(view, ensure_ascii=False)
    assert profile['DeviceId'] not in blob
    assert view['device_id_masked'].startswith(profile['DeviceId'][:4])
    assert view['device_source'] == 'migrated'
    assert view['device_environment_ok'] is True

def test_bridge_public_session_uses_install_profile(tmp_path):
    from replica.adapters.bridge_api import ReplicaApi
    _session(tmp_path)
    api = ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                     str(tmp_path))
    data = api.real_get_session()['data']
    assert data['device_id_present'] is True
    assert data['device_source'] == 'migrated'
    assert data['device_platform'] == 'android'
    assert data['device_name'] != 'Xiaomi 2304FPN6DG'
    assert data['device_os'] != '9'
    assert data['device_environment_ok'] is True
    assert data['device_migration_notice']
    assert data['token_present'] is False
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
    assert data['device_platform'] == new['platform']
    assert data['device_name'] == new['deviceName']
    assert data['device_os'] == new['osVersion']
    acct = api._real_account()
    assert acct.platform == new['platform']
    assert acct.idfa == new.get('idfa', '')
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

def test_set_session_device_change_clears_token(tmp_path):
    from replica.adapters.bridge_api import ReplicaApi
    api = ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                     str(tmp_path))
    api.real_set_session(uid='1', token='tok', DeviceId='dev-1')
    out = api.real_set_session(DeviceId='dev-2')
    assert out['data']['relogin_required'] is True
    assert 'token' not in api._read_session()


def test_real_account_rejects_incoherent_session_override(tmp_path):
    from replica.adapters.bridge_api import ReplicaApi
    api = ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                     str(tmp_path))
    api.real_set_session(
        uid='1', token='tok', platform='ios',
        DeviceId='0123456789ABCDEF0123456789ABCDEF',
        deviceName='Xiaomi 2304FPN6DG', osVersion='9',
        cpuModel='x86_64', physicPixel='900x1600',
        logicPixel='600x1066', isRoot='1',
        appInstallTime='1790000000000', appUpdateTime='1790000000000')
    with pytest.raises(DP.DeviceProfileError):
        api._real_account()


def test_real_login_keeps_manual_device_override(tmp_path, monkeypatch):
    from replica.adapters.bridge_api import ReplicaApi
    from replica.application import login_flow
    api = ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                     str(tmp_path))
    api.real_set_session(
        uid='1', token='tok-old', DeviceId='manual-device-1',
        deviceName='Pixel 8', osVersion='14', platform='android',
        cpuModel='arm64-v8a', physicPixel='1080x2400',
        logicPixel='411x914', isRoot='0',
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
    assert session['deviceName'] == 'Pixel 8'
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

def test_rotate_device_endpoint_requires_confirmation_and_login(tmp_path):
    from replica.adapters.bridge_api import ReplicaApi
    api = ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                     str(tmp_path))
    assert api.real_rotate_device('')['code'] != 200
    assert api.real_rotate_device('更换')['code'] != 200
    assert api.real_rotate_device('更换设备指纹')['code'] != 200


def test_rotate_device_endpoint_rotates_and_clears_session(tmp_path,
                                                            monkeypatch):
    from replica.adapters.bridge_api import ReplicaApi
    api = ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                     str(tmp_path))
    api.real_set_session(uid='1', token='tok', DeviceId='dev-1')
    monkeypatch.setattr(api, '_remote_logout_background', lambda acct: None)
    old = api._effective_device()['DeviceId']

    out = api.real_rotate_device('更换设备指纹')
    assert out['code'] == 200, out
    assert out['data']['relogin_required'] is True
    assert out['data']['device']['device_source'] == 'rotated'
    assert out['data']['device']['device_id_masked']
    assert out['data']['backup'].startswith('device_profile.backup-')

    session = api._read_session()
    assert 'token' not in session and 'uid' not in session
    new = DP.read_profile(str(tmp_path))
    assert new['DeviceId'] != old
    assert new['source'] == 'rotated'

def test_apply_to_account_switches_platform_and_idfa():
    android = DP.generate_profile(platform='android')
    ios = DP.generate_profile(platform='ios')
    acct = Account(name=ACCOUNT, uid='1', token='tok')
    DP.apply_to_account(acct, ios)
    assert acct.platform == 'ios'
    assert acct.idfa == ios['idfa']
    assert acct.cpuModel == 'arm64e'
    DP.apply_to_account(acct, android)
    assert acct.platform == 'android'
    assert acct.idfa == ''
    assert acct.cpuModel == 'arm64-v8a'


def test_rotate_can_force_ios_or_android(tmp_path):
    DP.load_or_create(str(tmp_path))
    ios, _ = DP.rotate(str(tmp_path), force=True, platform='ios')
    assert ios['platform'] == 'ios'
    assert ios['cpuModel'] == 'arm64e'
    android, _ = DP.rotate(str(tmp_path), force=True, platform='android')
    assert android['platform'] == 'android'
    assert android['cpuModel'] == 'arm64-v8a'


def test_rotate_device_endpoint_accepts_platform(tmp_path, monkeypatch):
    from replica.adapters.bridge_api import ReplicaApi
    api = ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                     str(tmp_path))
    api.real_set_session(uid='1', token='tok', DeviceId='dev-1')
    monkeypatch.setattr(api, '_remote_logout_background', lambda acct: None)
    out = api.real_rotate_device('更换设备指纹', 'ios')
    assert out['code'] == 200, out
    assert out['data']['device']['device_platform'] == 'ios'
    assert DP.read_profile(str(tmp_path))['platform'] == 'ios'
    bad = api.real_rotate_device('更换设备指纹', 'windows')
    assert bad['code'] != 200


def test_audit_flags_legacy_profile_but_not_new_ones():
    legacy = dict(DP.LEGACY_DEVICE)
    legacy['version'] = DP.PROFILE_VERSION
    legacy['created_at'] = 1_792_800_000_000
    legacy['source'] = 'legacy-default'
    issues = DP.audit_profile(legacy)
    assert any('unknown Android model' in i or 'isRoot' in i for i in issues)
    assert not DP.audit_profile(DP.generate_profile(platform='android'))
    assert not DP.audit_profile(DP.generate_profile(platform='ios'))


def test_device_catalog_covers_both_platforms():
    cat = DP.device_catalog()
    assert cat['android'] and cat['ios']
    row = cat['android'][0]
    assert row['deviceName'] and row['label']
    assert row['osVersions'] and row['physicPixel'] and row['logicPixel']
    assert row['cpuModel'] == 'arm64-v8a'
    assert cat['ios'][0]['cpuModel'] == 'arm64e'
    assert cat['default_install_days'] == 90
    assert cat['max_install_days'] == 3650


def test_custom_profile_is_coherent_and_uses_custom_source():
    row = DP.device_catalog()['android'][1]
    profile = DP.build_custom_profile(
        platform='android', device_name=row['deviceName'],
        os_version=row['osVersions'][0], device_id='custom-device-1',
        install_days=12)
    assert profile['source'] == 'custom'
    assert profile['DeviceId'] == 'custom-device-1'
    assert profile['isRoot'] == '0'
    assert profile['cpuModel'] == 'arm64-v8a'
    assert DP.audit_profile(profile) == []
    days = (int(profile['appUpdateTime']) - int(profile['appInstallTime'])) // 86400000
    assert days == 12


def test_custom_profile_rejects_incoherent_input():
    row = DP.device_catalog()['android'][0]
    with pytest.raises(DP.DeviceProfileError):
        DP.build_custom_profile(platform='android', device_name='Not A Phone',
                                os_version='14', device_id='custom-1')
    with pytest.raises(DP.DeviceProfileError):
        DP.build_custom_profile(platform='android', device_name=row['deviceName'],
                                os_version='99', device_id='custom-1')
    with pytest.raises(DP.DeviceProfileError):
        DP.build_custom_profile(platform='android', device_name=row['deviceName'],
                                os_version=row['osVersions'][0],
                                device_id='bad id with spaces')
    with pytest.raises(DP.DeviceProfileError):
        DP.build_custom_profile(platform='android', device_name=row['deviceName'],
                                os_version=row['osVersions'][0],
                                device_id='custom-1', install_days='9999')
    with pytest.raises(DP.DeviceProfileError):
        DP.build_custom_profile(platform='windows', device_name=row['deviceName'],
                                os_version=row['osVersions'][0],
                                device_id='custom-1')


def test_set_custom_backs_up_and_installs(tmp_path):
    old = DP.load_or_create(str(tmp_path))
    row = DP.device_catalog()['ios'][0]
    candidate = DP.build_custom_profile(
        platform='ios', device_name=row['deviceName'],
        os_version=row['osVersions'][0], device_id='custom-ios-1')
    new, backup = DP.set_custom(str(tmp_path), candidate)
    assert new['source'] == 'custom'
    assert new['platform'] == 'ios'
    assert new['idfa']
    assert backup and os.path.exists(backup)
    saved = DP.read_profile(str(tmp_path))
    assert saved['DeviceId'] == 'custom-ios-1'
    assert DP.audit_profile(saved) == []
    with open(backup, encoding='utf-8') as f:
        assert json.load(f)['DeviceId'] == old['DeviceId']


def test_custom_device_endpoint_requires_confirmation_and_clears_session(
        tmp_path, monkeypatch):
    from replica.adapters.bridge_api import ReplicaApi
    api = ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                     str(tmp_path))
    row = DP.device_catalog()['android'][0]
    common = dict(platform='android', deviceName=row['deviceName'],
                  osVersion=row['osVersions'][0], DeviceId='custom-bridge-1')
    assert api.real_set_custom_device(confirm='', **common)['code'] != 200
    assert api.real_set_custom_device(confirm='自定义', **common)['code'] != 200
    assert 'token' not in api._read_session()

    api.real_set_session(uid='1', token='tok', DeviceId='dev-1')
    monkeypatch.setattr(api, '_remote_logout_background', lambda acct: None)
    out = api.real_set_custom_device(confirm='自定义设备', **common)
    assert out['code'] == 200, out
    assert out['data']['relogin_required'] is True
    assert out['data']['backup'].startswith('device_profile.backup-')
    session = api._read_session()
    assert 'token' not in session and 'uid' not in session
    assert DP.read_profile(str(tmp_path))['DeviceId'] == 'custom-bridge-1'


def test_device_options_endpoint_returns_catalog(tmp_path):
    from replica.adapters.bridge_api import ReplicaApi
    api = ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                     str(tmp_path))
    out = api.real_get_device_options()
    assert out['code'] == 200, out
    assert out['data']['catalog']['android']
    assert out['data']['current']['device_id_masked']


def test_public_view_reports_platform():
    ios = DP.generate_profile(platform='ios')
    view = DP.public_view(ios)
    assert view['device_platform'] == 'ios'
    assert view['device_name'] == ios['deviceName']


def test_public_view_exposes_the_wire_identity():
    # The account page shows what the server actually sees.  If these drift
    # from realclient.user_agent(), the UI would be lying about the wire.
    for platform, prefix in (('android', 'okhttp/'), ('ios', 'SWCampus/')):
        profile = DP.generate_profile(platform=platform)
        view = DP.public_view(profile)
        assert view['device_ua'].startswith(prefix)
        # Android logicPixel is dp, not a divisor; only iOS has a scale factor.
        assert view['device_scale'] == (3 if platform == 'ios' else 0)
        assert view['device_installed_days'] > 0


def test_repair_pixels_keeps_device_id_and_backs_up(tmp_path):
    profile = DP.generate_profile(platform='android')
    profile['physicPixel'] = '1080x2400'
    profile['logicPixel'] = '1080x2400'
    DP.save_profile(str(tmp_path), profile)
    fixed, backup, changes = DP.repair_pixels(str(tmp_path))
    assert fixed['DeviceId'] == profile['DeviceId']
    row = next(r for r in DP.ANDROID_MODELS
               if r[0] == fixed['deviceName'])
    assert fixed['physicPixel'] == row[2]
    assert fixed['logicPixel'] == row[3]
    assert not DP.audit_profile(fixed)
    assert backup and os.path.exists(backup)
    with open(backup, encoding='utf-8') as f:
        saved = json.load(f)
    assert saved['logicPixel'] == '1080x2400'
    assert changes
    _, backup2, changes2 = DP.repair_pixels(str(tmp_path))
    assert not changes2 and backup2 is None


def test_keepalive_probe_uses_install_profile(tmp_path):
    from replica.application import tokenkeeper as TK
    profile = DP.generate_profile(platform='android')
    DP.save_profile(str(tmp_path), profile)
    captured = {}

    class FakeClient:
        def request(self, acct, path, method='GET', retries=1):
            captured['acct'] = acct
            return {'status': 200, 'raw_len': 1, 'error': None,
                    'json': {'error': 10000, 'data': {}}}

    out = TK.probe_real_session(
        {'uid': '1', 'token': 'tok', 'name': 'x', 'unid': 47203},
        client_factory=lambda *a, **k: FakeClient(),
        store_dir=str(tmp_path))
    assert out['ok'] is True
    acct = captured['acct']
    assert acct.DeviceId == profile['DeviceId']
    assert acct.platform == profile['platform']
    assert acct.cpuModel == profile['cpuModel']
    assert acct.logicPixel == profile['logicPixel']

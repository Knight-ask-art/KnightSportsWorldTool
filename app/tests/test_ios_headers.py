"""iOS header shape tests (offline; no network)."""

import json

from replica.transport.ios_headers import (
    IOS_APP_VERSION,
    build_ios_headers,
    ios_header_json,
    token_sign,
)
from replica.transport.realclient import Account, build_headers, user_agent


def _account(**extra):
    values = {
        'name': 'tester',
        'uid': '15663170',
        'token': 'tok-test',
        'platform': 'ios',
        'DeviceId': '0FA3EA16-46F0-4E44-9A97-1B63F0D9B11A',
        'deviceName': 'iPhone 15',
        'osVersion': '18.1',
        'cpuModel': 'arm64e',
        'physicPixel': '1179x2556',
        'logicPixel': '393x852',
        'isRoot': '0',
        'idfa': '00000000-0000-0000-0000-000000000000',
        'appInstallTime': '1790000000000',
        'appUpdateTime': '1790000000000',
    }
    values.update(extra)
    return Account(**values)


def test_ios_header_shape_matches_reference_order():
    headers = build_ios_headers(_account(), '1792828000000', 'nonce-1',
                                idfa='IDFA-TEST')
    assert list(headers) == [
        'osType', 'DeviceId', 'deviceName', 'CustomDeviceId', 'osVersion',
        'IDFA', 'logicPixel', 'physicPixel', 'cpuModel', 'appVersion',
        'isRoot', 'appInstallTime', 'nonce', 'uid', 'token', 'timeStamp',
        'studentId', 'tokenSign',
    ]
    assert headers['osType'] == '1'
    assert headers['deviceName'] == 'iPhone 15'
    assert headers['CustomDeviceId'].endswith('_iOS_sportsWorld_campus')
    assert headers['osVersion'] == '18.1'
    assert headers['logicPixel'] == '393x852'
    assert headers['physicPixel'] == '1179x2556'
    assert headers['cpuModel'] == 'arm64e'
    assert headers['appVersion'] == IOS_APP_VERSION == '7.3.40'
    assert headers['isRoot'] is False
    assert headers['appInstallTime'] == 1790000000000
    assert headers['uid'] == 15663170
    assert headers['studentId'] == 15663170
    assert headers['token'] == 'tok-test'
    assert headers['IDFA'] == 'IDFA-TEST'
    assert 'appUpdateTime' not in headers


def test_ios_header_omits_optionals_when_logged_out():
    headers = build_ios_headers(_account(uid='-1', token=''),
                                '1792828000000', 'nonce-2')
    assert 'uid' not in headers
    assert 'token' not in headers
    assert 'tokenSign' not in headers
    assert headers['studentId'] == 0
    assert headers['IDFA']  # from the profile, not added by the caller


def test_ios_header_json_keeps_native_types_and_order():
    headers = build_ios_headers(_account(), '1792828000000', 'nonce-3')
    raw = ios_header_json(headers)
    assert raw.startswith('{"osType":"1","DeviceId":')
    assert '"isRoot":false' in raw
    assert '"uid":15663170' in raw
    assert '"studentId":15663170' in raw
    assert ', ' not in raw
    assert '": ' not in raw
    # A stringified boolean/number would be a different signature.
    assert '"isRoot":"' not in raw
    assert '"uid":"' not in raw


def test_build_headers_dispatches_on_platform():
    android = build_headers(_account(platform='android', cpuModel='arm64-v8a',
                                     physicPixel='1080x2400',
                                     logicPixel='393x873'),
                            '1792828000000', 'nonce-4')
    ios = build_headers(_account(), '1792828000000', 'nonce-5')
    assert android['osType'] == '0'
    assert 'IMEI' in android and 'androidId' in android
    assert 'IDFA' not in android
    assert ios['osType'] == '1'
    assert 'IMEI' not in ios and 'studentId' in ios
    assert ios['DeviceId'] == android['DeviceId']
    assert ios['cpuModel'] == 'arm64e'
    assert android['cpuModel'] == 'arm64-v8a'


def test_ios_token_sign_matches_reference_vector():
    assert token_sign('1788958186123', 'TOKENABC', 13056447) == \
        '4a8d163186d91ac7b539a7bb07d457ba'


def test_ios_header_json_is_valid_json():
    headers = build_ios_headers(_account(), '1792828000000', 'nonce-6')
    assert json.loads(ios_header_json(headers)) == headers


def test_user_agent_matches_platform_and_ios_major():
    # Android: the official dex sets no custom UA, so OkHttp's default is the
    # honest wire value (antiban review, ANTI-FLAG-AUDIT.md).
    assert user_agent(_account(platform='android')) == 'okhttp/4.12.0'
    # iOS: NSURLSession default shape <App>/<ver> (iPhone; iOS <ver>; Scale/n).
    ua = user_agent(_account(osVersion='18.1'))
    assert ua == 'SWCampus/7.3.40 (iPhone; iOS 18.1; Scale/3.00)'
    assert user_agent(_account(osVersion='16.7.10')).endswith(
        '(iPhone; iOS 16.7.10; Scale/3.00)')
    assert 'Darwin/' not in ua and 'CFNetwork/' not in ua
    # The UA version must never disagree with the signed appVersion.
    assert '/%s ' % IOS_APP_VERSION in ua + ' '
    assert 'Python-urllib' not in ua


def test_ios_ua_scale_follows_the_profile_pixels():
    # @2x devices must not advertise Scale/3.00.
    acct = _account(physicPixel='828x1792', logicPixel='414x896')
    assert user_agent(acct).endswith('Scale/2.00)')


def test_ios_ua_refuses_an_impossible_scale():
    # A corrupt profile (physic == logic, or 4x) would otherwise advertise a
    # scale no iPhone has.
    for physic, logic in (('1170x2532', '1170x2532'),
                          ('1560x3384', '390x846')):
        acct = _account(physicPixel=physic, logicPixel=logic)
        assert user_agent(acct).endswith('Scale/3.00)')


def test_ios_header_install_time_never_sends_zero():
    headers = build_ios_headers(_account(appInstallTime='0'),
                                '1792828000000', 'nonce-9')
    # 3 days before the request timestamp, like the reference builder.
    assert headers['appInstallTime'] == 1792828000000 - 3 * 86_400_000


def test_ios_header_device_id_is_never_empty():
    first = build_ios_headers(_account(DeviceId=''), '1792828000000', 'n-a')
    second = build_ios_headers(_account(DeviceId=''), '1792828000100', 'n-b')
    assert first['DeviceId']
    # Stable per process: a per-request UUID would be its own signal.
    assert first['DeviceId'] == second['DeviceId']
    assert first['CustomDeviceId'] == first['DeviceId'] + '_iOS_sportsWorld_campus'


def test_header_sign_uses_ios_serializer(monkeypatch):
    from replica.transport import netsec
    from replica.transport.realclient import RealClient

    client = RealClient(backend='netsec')
    captured = {}

    def fake_envelope(session, plain, order):
        captured['plain'] = plain
        captured['order'] = order
        return {'json': 'signed'}

    monkeypatch.setattr(netsec, 'build_envelope', fake_envelope)
    headers = build_ios_headers(_account(), '1792828000000', 'nonce-7')
    assert client.header_sign(headers, 'ios') == 'signed'
    assert captured['plain'] == ios_header_json(headers)
    assert captured['order'] == 'observed'
    assert '"uid":15663170' in captured['plain']


def test_android_header_shape_is_unchanged():
    acct = _account(platform='android', cpuModel='arm64-v8a',
                    physicPixel='1080x2400', logicPixel='393x873',
                    idfa='')
    headers = build_headers(acct, '1792828000000', 'nonce-8')
    assert list(headers) == [
        'Accept', 'Content-Type', 'appVersion', 'physicPixel', 'logicPixel',
        'isRoot', 'osType', 'uid', 'IMEI', 'timeStamp', 'blMac', 'nonce',
        'token', 'tokenSign', 'cpuModel', 'deviceName', 'appInstallTime',
        'androidId', 'DeviceId', 'appUpdateTime', 'wifiMac',
        'CustomDeviceId', 'osVersion',
    ]
    assert headers['osType'] == '0'
    assert 'IDFA' not in headers and 'studentId' not in headers

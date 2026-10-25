"""Phase-1 regression suite: protocol fidelity + mock loop."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from replica.domain import crypto as C
from replica.domain import rundata as R
from replica.compat.mocks.mock_iydsj import MockIydsjState, serve as serve_iydsj
from replica.compat.mocks.mock_65530 import MockBackendState, serve as serve_be
from replica.adapters.bridge_api import ReplicaApi

PASS = []
FAIL = []


def check(name, cond, extra=''):
    (PASS if cond else FAIL).append(name)
    print(('PASS ' if cond else 'FAIL ') + name + (f' [{extra}]' if extra and not cond else ''))


def main():
    tmp = tempfile.mkdtemp(prefix='app_')
    priv, pub = C.ensure_keypair(os.path.join(tmp, 'mock_rsa'))

    # 1. signing
    s1 = C.runschool_sign({'b': '2', 'a': '1'})
    s2 = C.runschool_sign({'a': '1', 'b': '2'})
    check('sign deterministic/order-independent', s1 == s2 and len(s1) == 32)
    import hashlib
    check('sign == md5(a=1&b=2)',
          s1 == hashlib.md5(b'a=1&b=2').hexdigest())
    # official SignCode semantics (Coaixy 7.2.70 source, hand-derived goldens)
    check('official basic',
          C.official_sign({'b': '2', 'a': '1'}, 'S')
          == hashlib.md5(b'a=1&b=2S').hexdigest())
    check('official case-insensitive sort',
          C.official_sign({'B': '1', 'a': '2'}, 'S')
          == hashlib.md5(b'a=2&B=1S').hexdigest())
    check('official drops sign/null',
          C.official_sign({'a': '1', 'sign': 'X', 'n': None}, 'S')
          == hashlib.md5(b'a=1S').hexdigest())
    check('official presign no salt',
          C.official_presign({'b': 2, 'a': True}) == 'a=True&b=2')
    check('tokenSign recovered salt',
          C.token_sign('1700000000000', 'abc', '42')
          == hashlib.md5('timeStamp=1700000000000&token=abc&uid=42'
                         '&wh2016_swcampus'.encode()).hexdigest())

    # 2. hybrid roundtrip
    obj = {'uid': 'u1', 'run_data_': {'juli': 2500}}
    p = C.hybrid_encrypt(obj, pub)
    check('payload fields', set(p) == {'encrypted_data', 'encrypted_key', 'aes_iv'})
    check('roundtrip', C.hybrid_decrypt(p, priv) == obj)
    bad = dict(p)
    bad['encrypted_data'] = bad['encrypted_data'][:-4] + 'AAAA'
    try:
        C.hybrid_decrypt(bad, priv)
        check('tamper rejected', False)
    except Exception:
        check('tamper rejected', True)
    # wrong key
    from Crypto.PublicKey import RSA
    other = RSA.generate(2048).export_key()
    try:
        C.hybrid_decrypt(p, other)
        check('wrong-key rejected', False)
    except Exception:
        check('wrong-key rejected', True)

    # 3. IMEI
    imeis = [C.generate_imei() for _ in range(20)]
    check('imei luhn x20', all(C.luhn_valid(i) for i in imeis))
    check('imei rejects bad', not C.luhn_valid('123456789012345'))

    # 4. run data structure
    d = R.generate_run_data(2.5, center_lat=22.4108, center_lng=112.9408)
    check('25 pts', len(d['allLocJson']) >= 25)
    check('90 speed segs', len(d['speed_json']) == 90)
    check('90 step segs', len(d['step_freq_json']) == 90)
    check('5 fivePoint', len(d['fivePoint']) == 5)
    check('validate clean', R.validate_run_data(d) == [])
    d2 = R.generate_run_data(2.5, center_lat=22.4108, center_lng=112.9408)
    d2['allLocJson'][5]['ts'] = d2['allLocJson'][4]['ts']
    check('non-monotonic caught',
          'timestamps-not-monotonic' in R.validate_run_data(d2))

    # 5. API surface: 71 names
    istate = MockIydsjState(priv, pub)
    isrv = serve_iydsj(istate)
    bstate = MockBackendState()
    bsrv = serve_be(bstate)
    api = ReplicaApi(f'http://127.0.0.1:{isrv.server_port}',
                     f'http://127.0.0.1:{bsrv.server_port}', pub, tmp)
    expected = ['addProxyAgent', 'addStudentAccount', 'adminLogin',
                'admini_changePassword', 'admini_getAccountBalance', 'checkForUpdates',
                'checkRechargeStatus', 'checkRequiredFields', 'checkSecretKeyStatus',
                'check_login_ip', 'close_window', 'connectVM', 'deleteProxyAgent',
                'deleteStudentAccount', 'delete_sport_data', 'disconnectVM',
                'downloadCS7', 'downloadUpdate', 'downloadVM', 'getAmapKey',
                'getAppLogo', 'getCampusRunPoints', 'getCampusRunPointsList',
                'getGeetestConfig', 'getGeetoken', 'getProxyAgents',
                'getProxyRechargeAddress', 'getProxyRechargeInfo', 'getSchoolList',
                'getStudentAccountDetail', 'getStudentAccounts',
                'getStudentLoginInfoById', 'getUpdateProgress', 'getUsdtExchangeRate',
                'getVMStatus', 'get_Apple_inspector', 'get_TTT',
                'get_account_and_password', 'get_app_set', 'get_cla_task_uuid_info',
                'get_geevalidate', 'get_location', 'get_one_record',
                'get_run_task_uuid_info', 'get_sava_sport_datas', 'get_student_records',
                'get_user_proxy', 'get_users_ordrs', 'installUpdate',
                'is_window_maximized', 'kill_task_uuid', 'login_verify',
                'maximize_window', 'minimize_window', 'open_dirs', 'queryProxyUser',
                'redeemSecretKey', 'repeat_run_task_uuid', 'save_sport_data',
                'searchStudents', 'set_account_and_password', 'set_user_proxy',
                'startResize', 'start_drag', 'studentLogin', 'studentLogout',
                'transfer_mileage', 'updateProxyAgentStatus',
                'updateProxyRechargeAddress', 'updateStudentAccount',
                'uploadRunningData']
    missing = [n for n in expected if not callable(getattr(api, n, None))]
    check(f'api surface 71 ({len(expected)-len(missing)}/{len(expected)})', not missing,
          extra=str(missing))

    # 6. E2E mock loop
    r = api.studentLogin('stu01')
    check('login needs gee', r['code'] == 500)
    r = api.studentLogin('stu01', gee_uuid='gee-test-uuid')
    check('login ok', r['code'] == 200, extra=str(r))
    run = api.build_run_data(2.5, seed=42)
    up = api.uploadRunningData('stu01', run, 'G01-123-abcdef0123456789')
    check('upload ok', up['code'] == 200, extra=str(up)[:200])
    recs = api.get_student_records('x')
    check('records list', recs['code'] == 200 and len(recs['data']) == 1,
          extra=str(recs)[:200])
    rid = recs['data'][0]['id']
    one = api.get_one_record(rid, None, None, None)
    check('get_one_record', one['code'] == 200 and one['data']['id'] == rid)
    # official duplicate code 10304 on same run uuid
    dup = api.uploadRunningData('stu01', run, 'G01-123-abcdef0123456789')
    check('duplicate 10304', dup['code'] == 10304, extra=str(dup)[:120])
    # tampered signature rejected by mock recomputation
    from replica.domain.rundata import build_upload_entity, sign_upload_entity
    ent = sign_upload_entity(build_upload_entity(run, {'uid': 'x'}))
    ent['signature'] = '0' * 32
    inner = {'uid': 'x', 'token': '', 'DeviceId': '',
             'current_timestamp': __import__('time').time_ns() // 1_000_000,
             'geeToken': 'G01-1-abcdef0123456789', 'entity': ent,
             'run_data_': run}
    import requests as _rq
    _h = C.upload_headers()
    _h['appVersion'] = '7.3.70'
    tr = _rq.post(
        f'http://127.0.0.1:{isrv.server_port}/api/v70260/runnings/save/record',
        json=C.hybrid_encrypt(inner, pub), headers=_h, timeout=10).json()
    check('bad signature rejected', tr['code'] == 500, extra=str(tr)[:160])
    # bad Geeid rejected
    import requests as rq
    bad_headers = C.upload_headers()
    bad_headers['Geeid'] = 'BAD'
    bad_headers['appVersion'] = '7.3.70'
    rr = rq.post(f'http://127.0.0.1:{isrv.server_port}/api/v70260/runnings/save/record',
                 json=C.hybrid_encrypt({'run_data_': run}, pub),
                 headers=bad_headers, timeout=10).json()
    check('bad Geeid rejected', rr['code'] == 500)
    # invalid run data rejected
    bad_run = dict(run)
    bad_run['allLocJson'] = bad_run['allLocJson'][:1]
    up2 = api.uploadRunningData('stu01', bad_run, 'G01-123-abcdef0123456789')
    check('short track rejected', up2['code'] == 500, extra=str(up2)[:160])

    # 7. backend 65530
    check('adminLogin', api.adminLogin('a', 'p')['code'] == 200)
    check('redeem', api.redeemSecretKey('stu01', 'KM-001')['code'] == 200)
    check('balance 1.0',
          api.admini_getAccountBalance('stu01', None)['data']['balance'] == 1.0)
    check('transfer', api.transfer_mileage('a', 'p', 'b', 0.5)['code'] == 200)
    ords = api.get_users_ordrs(None, None, 1, None, None)
    check('orders shape', ords['code'] == 200 and 'totalPages' in ords['data'])
    ag = api.addProxyAgent('n', 'u', 'p', 'x')
    check('agent add/list', ag['code'] == 200 and
          api.getProxyAgents(None, None)['data']['total'] == 1)
    check('recharge addr ERC20',
          api.getProxyRechargeAddress(None, None)['data']['chain'] == 'ERC20')
    check('updates', api.checkForUpdates()['data']['hasUpdate'] is False)
    import urllib.request
    ca = urllib.request.urlopen(
        f'http://127.0.0.1:{bsrv.server_port}/get_ca', timeout=10)
    check('get_ca 200', ca.status == 200)

    isrv.shutdown()
    bsrv.shutdown()
    print(f'\n==== {len(PASS)} passed, {len(FAIL)} failed ====')
    if FAIL:
        print('FAILED:', FAIL)
        raise SystemExit(1)


if __name__ == '__main__':
    main()

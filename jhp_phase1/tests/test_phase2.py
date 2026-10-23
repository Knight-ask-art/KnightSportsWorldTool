"""Phase-2 e2e: boot the runnable server, drive the HTTP bridge like the UI."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from replica.server import serve

PASS, FAIL = [], []


def check(name, cond, extra=''):
    (PASS if cond else FAIL).append(name)
    print(('PASS ' if cond else 'FAIL ') + name +
          (f' [{extra}]' if extra and not cond else ''))


BASE = None


def call(method, args=None):
    r = requests.post(BASE + '/bridge/' + method, json={'args': args or []},
                      timeout=15)
    return r.json()


def main():
    global BASE
    tmp = tempfile.mkdtemp(prefix='jhp_phase2_')
    srv = serve(0, store_dir=tmp, quiet=True)
    BASE = f'http://127.0.0.1:{srv.server_address[1]}'

    check('index served',
          requests.get(BASE + '/', timeout=10).status_code == 200)
    check('unknown api 404',
          call('no_such_api')['code'] == 404)

    check('addStudent', call('addStudentAccount', ['stu01', {'id': 'stu01'}])['code'] == 200)
    g = call('get_geevalidate', [{'geetest_challenge': 'c',
                                  'geetest_validate': 'v'}])
    check('geevalidate', g['code'] == 200, extra=str(g)[:160])
    lg = call('studentLogin', ['stu01', g['data']['uuid']])
    check('login', lg['code'] == 200, extra=str(lg)[:200])
    det = call('getStudentAccountDetail', ['stu01'])
    check('detail logged in',
          det['code'] == 200 and det['data'].get('isLoggedIn') is True)

    b = call('build_run_data', [2.5, 6.0, 172])
    check('build via bridge',
          b['code'] == 200 and len(b['data']['allLocJson']) >= 25)
    sv = call('save_sport_data',
              ['stu01', 2.5, 6.0, 172, b['data'], None, None, None])
    check('save', sv['code'] == 200)
    ls = call('get_sava_sport_datas', ['stu01', 1, 20])
    check('paged list',
          ls['code'] == 200 and ls['data']['total'] == 1
          and ls['data']['totalPages'] == 1)

    up = call('uploadRunningData', ['stu01', b['data'], 'G01-1-abcdef0123456789'])
    check('upload', up['code'] == 200 and up['data'].get('serverCheckStatus') == 'pass',
          extra=str(up)[:200])
    recs = call('get_student_records', ['stu01', None])
    check('records array',
          recs['code'] == 200 and isinstance(recs['data'], list)
          and len(recs['data']) == 1)
    one = call('get_one_record', [recs['data'][0]['id'], None, None, None])
    check('one record', one['code'] == 200)

    t = call('run_task_submit', ['stu01', 2.5, 6.0, 'G01-2-abcdef0123456789'])
    check('one-click task', t['code'] == 200)
    # find task via kill-targeted query is not exposed; verify via backend state is out of scope —
    # instead verify tasks endpoints reject unknown + accept flow through repeat path:
    check('unknown task', call('get_run_task_uuid_info', ['nope'])['code'] == 500)

    check('schools', isinstance(call('getSchoolList', [])['data'], list))
    check('points', call('getCampusRunPoints', ['s', 24.87, 118.67])['code'] == 200)
    check('apple db', 'iPhone 15' in str(call('get_Apple_inspector', [])['data']))
    # The build no longer ships a hard-coded AMap key; an empty string is the
    # honest answer until the user sets JHP_AMAP_KEY.
    check('amap key is a string',
          isinstance(call('getAmapKey', [])['data'].get('key'), str))
    check('rate', call('getUsdtExchangeRate', [])['data']['chain'] == 'ERC20')
    check('redeem+balance',
          call('redeemSecretKey', ['stu01', 'KM-1'])['code'] == 200
          and call('admini_getAccountBalance', ['stu01', None])['data']['balance'] == 1.0)
    check('orders', 'totalPages' in call('get_users_ordrs', [None, None, 1, None, None])['data'])
    check('updates', call('checkForUpdates', [])['data']['hasUpdate'] is False)
    check('vm stub code 600', call('connectVM', ['a', 'b', 'c', 'd'])['code'] == 600)

    srv.shutdown()
    print(f'\n==== {len(PASS)} passed, {len(FAIL)} failed ====')
    if FAIL:
        print('FAILED:', FAIL)
        raise SystemExit(1)


if __name__ == '__main__':
    main()

"""Full pure-protocol login: checkGeeUse -> GT4 -> geevalidate -> login.

Everything here is local: the slider is solved by replica/gt4.py, the envelopes
by replica/netsec.py. No emulator, no unidbg, no browser.

Order of operations (mirrors the official client):
  1. POST api/v65/security/checkGeeUse {username, uuid, unid, type:2}
     data == true  -> this login needs no captcha, skip to step 4
     data == false -> captcha required
  2. solve GT4 -> {lotNumber, captchaOutput, passToken, genTime}
  3. POST api/v70270/security/geevalidate  (9 fields, same uuid as step 1)
     error 10000 -> ticket accepted; 10003 -> retry from step 2
  4. POST api/v70100/login  (Authorization: Basic + envelope body)
     -> uid / token / unid
"""

from __future__ import annotations

import base64
import json
import time
import uuid

from ..transport import gt4 as GT4
from ..transport.realclient import Account, RealClient

GEE_CHECK = 'api/v65/security/checkGeeUse'
GEE_VALIDATE = 'api/v70270/security/geevalidate'
LOGIN = 'api/v70100/login'


def _log(msg):
    print(msg, flush=True)


def login(username: str, password: str, device_model: str = 'NCO-AL00',
          os_version: str = '12', captcha_id: str | None = None,
          max_captcha_rounds: int = 3, log=_log) -> dict:
    """Returns {'uid','token','unid','profile'} or raises RuntimeError."""
    acct = Account(name=username, uid='-1', token='')
    acct.deviceName = device_model
    acct.osVersion = os_version
    client = RealClient()

    login_uuid = str(uuid.uuid4())

    # ---- 1. does this login need a captcha? -----------------------------
    needs_captcha = True
    try:
        chk = client.request(acct, GEE_CHECK, {
            'username': username,
            'uuid': login_uuid,
            'unid': 0,
            'type': 2,
        })
        biz = chk.get('json') or {}
        if biz.get('error') == 10000:
            needs_captcha = not bool(biz.get('data'))
        log('[gee] checkGeeUse error=%s data=%r -> captcha %s'
            % (biz.get('error'), biz.get('data'),
               'REQUIRED' if needs_captcha else 'NOT required'))
    except Exception as exc:  # noqa: BLE001
        log('[gee] checkGeeUse failed (%s) -> assume captcha required' % exc)

    # ---- 2/3. captcha + geevalidate ------------------------------------
    if needs_captcha:
        ok = False
        for rnd in range(1, max_captcha_rounds + 1):
            log('[gt4] round %d: solving slider...' % rnd)
            creds = GT4.solve_gt4(captcha_id, log=log)
            log('[gt4] ticket lot=%s' % creds['lotNumber'][:12])

            body = {
                'lotNumber': creds['lotNumber'],
                'captchaOutput': creds['captchaOutput'],
                'passToken': creds['passToken'],
                'genTime': creds['genTime'],
                'isOffline': False,
                'osType': 1,
                'businessType': 0,
                'uuid': login_uuid,
                'username': username,
            }
            res = client.request(acct, GEE_VALIDATE, body)
            biz = res.get('json') or {}
            err = biz.get('error')
            log('[geevalidate] error=%s message=%s' % (err, biz.get('message')))
            if err == 10000:
                ok = True
                break
            if err != 10003:
                log('[geevalidate] non-10003 failure, continuing anyway')
                ok = True
                break
            time.sleep(2)
        if not ok:
            raise RuntimeError('geevalidate kept returning 10003 after %d rounds'
                               % max_captcha_rounds)

    # ---- 4. login ------------------------------------------------------
    body = {
        'device_model': device_model,
        'os_version': os_version,
        'mac_address': '',
        'imei': '',
        'loginType': 0,
        'username': username,
        'password': password,
        'uuid': login_uuid,
        'osType': '0',
    }
    headers_extra = {
        'Authorization': 'Basic ' + base64.b64encode(
            ('%s:%s' % (username, password)).encode()).decode(),
    }
    res = client.request(acct, LOGIN, body, extra_headers=headers_extra,
                         method='POST', timeout=60)
    biz = res.get('json') or {}
    log('[login] HTTP %s error=%s message=%s'
        % (res.get('status'), biz.get('error'), biz.get('message')))

    if biz.get('error') != 10000:
        raise RuntimeError('login rejected: error=%s %s'
                           % (biz.get('error'), biz.get('message')))

    data = biz.get('data')
    # `data` may be a JSON-encoded string (same quirk as save/record); a raw
    # .get() on it would raise AttributeError instead of the intended message.
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:  # noqa: BLE001
            data = {}
    if not isinstance(data, dict):
        data = {}
    uid = data.get('uid')
    token = data.get('token')
    if not uid or not token:
        raise RuntimeError('login response missing uid/token: %s'
                           % json.dumps(biz, ensure_ascii=False)[:200])

    return {'uid': str(uid), 'token': token,
            'unid': data.get('unid'), 'profile': data}


if __name__ == '__main__':
    import argparse

    ap = argparse.ArgumentParser(description='pure-protocol login')
    ap.add_argument('--user', required=True)
    ap.add_argument('--pass', dest='password', required=True)
    ap.add_argument('--captcha-id', default=None)
    ap.add_argument('--out', default=None, help='write session json here')
    a = ap.parse_args()

    try:
        sess = login(a.user, a.password, captcha_id=a.captcha_id)
    except Exception as e:  # noqa: BLE001
        print('\nLOGIN FAILED:', type(e).__name__, e)
        raise SystemExit(1)

    print('\n=== LOGIN OK ===')
    print('  uid   =', sess['uid'])
    print('  token =', sess['token'][:16] + '...')
    print('  unid  =', sess['unid'])
    print('  name  =', sess['profile'].get('name'))
    print('  campus=', sess['profile'].get('campusName'))
    if a.out:
        with open(a.out, 'w', encoding='utf-8') as f:
            json.dump(sess, f, ensure_ascii=False, indent=1)
        print('  saved ->', a.out)

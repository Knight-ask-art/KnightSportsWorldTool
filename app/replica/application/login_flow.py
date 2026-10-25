"""Full pure-protocol login: checkGeeUse -> GT4 -> geevalidate -> login.

Everything here is local: the slider is solved by :mod:`replica.transport.gt4`,
the envelopes by :mod:`replica.transport.netsec`.  No emulator, no unidbg, no
browser in the normal path.

Order of operations (mirrors the official client):
  1. POST api/v65/security/checkGeeUse {username, uuid, unid, type:2}
     data == true  -> this login needs no captcha, skip to step 4
     data == false -> captcha required
  2. solve GT4 -> {lotNumber, captchaOutput, passToken, genTime}
  3. POST api/v70270/security/geevalidate  (9 fields, same uuid as step 1)
     error 10000 -> ticket accepted
     error 10003 -> ticket scored too low, solve again
     error 10007 -> endpoint-level risk control, retrying is pointless
  4. POST api/v70100/login  (Authorization: Basic + envelope body)
     -> uid / token / unid

``login_with_ticket`` is the escape hatch: the UI shows the same GeeTest images
and lets the operator place the slice, then hands us the four credentials.  That
path never runs the local CV, so a stale matcher cannot block a login -- and it
still builds ``w`` with the same payload module, so it is one protocol
implementation with two ways to find the gap.
"""

from __future__ import annotations

import base64
import json
import time
import uuid

from ..transport import gt4 as GT4
from ..transport.realclient import Account, RealClient
from . import device_profile as DP

GEE_CHECK = 'api/v65/security/checkGeeUse'
GEE_VALIDATE = 'api/v70270/security/geevalidate'
LOGIN = 'api/v70100/login'

#: One solve cycle: both captcha ids, one try each.  A 10003 after that goes
#: straight to the manual widget — retrying a scored-low ticket on the same IP
#: is how the old code ended up firing up to 18 solves per login.
MAX_CAPTCHA_ROUNDS = 1


class LoginError(RuntimeError):
    """Anything that stopped the login.  Message is safe to show in the UI."""


class CaptchaFailed(LoginError):
    """We could not produce a ticket the business server accepts."""


class RiskControlError(LoginError):
    """The server refused before it even looked at the credentials."""


def _log(msg):
    print(msg, flush=True)


def _check_needs_captcha(client, acct, username: str, login_uuid: str,
                         log) -> bool:
    try:
        chk = client.request(acct, GEE_CHECK, {
            'username': username,
            'uuid': login_uuid,
            'unid': 0,
            'type': 2,
        })
        biz = chk.get('json') or {}
        if biz.get('error') == 10000:
            needs = not bool(biz.get('data'))
            log('[gee] checkGeeUse error=%s data=%r -> captcha %s'
                % (biz.get('error'), biz.get('data'),
                   'REQUIRED' if needs else 'NOT required'))
            return needs
    except Exception as exc:  # noqa: BLE001
        log('[gee] checkGeeUse failed (%s) -> assume captcha required' % exc)
    return True


def _validate_ticket(client, acct, username: str, login_uuid: str,
                     creds: dict, log) -> int:
    """POST the four credentials; return the business error code."""
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
    return err if isinstance(err, int) else -1


def _submit_login(client, acct, username: str, password: str,
                  login_uuid: str, log) -> dict:
    body = {
        'device_model': acct.deviceName,
        'os_version': acct.osVersion,
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
        raise LoginError('login rejected: error=%s %s'
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
        raise LoginError('login response missing uid/token: %s'
                         % json.dumps(biz, ensure_ascii=False)[:200])
    return {'uid': str(uid), 'token': token,
            'unid': data.get('unid'), 'profile': data}


def _solve(solver, captcha_id, log) -> dict:
    try:
        return solver(captcha_id=captcha_id, log=log)
    except GT4.GeeRejected as exc:
        raise CaptchaFailed(
            '极验拒绝了本次滑块（%s）。这通常是解题器过期了，'
            '可以点「手动滑一下」自己把缺口对上。' % exc) from exc
    except GT4.GeeUnavailable as exc:
        raise CaptchaFailed('连不上极验服务器（%s），请检查网络后重试。'
                            % exc) from exc
    except GT4.GeeError as exc:
        raise CaptchaFailed('极验自动解题失败：%s；可以点「手动滑一下」。'
                            % exc) from exc


def _account(username: str, device_model: str, os_version: str,
             device: dict | None) -> Account:
    # `name` is only a label, but keep it as the username: that is what the
    # pre-split code passed and nothing here should drift.
    acct = Account(name=username, uid='-1', token='')
    acct.deviceName = device_model
    acct.osVersion = os_version
    if device:
        DP.apply_to_account(acct, device)
    return acct


def login(username: str, password: str, device_model: str = 'NCO-AL00',
          os_version: str = '12', captcha_id: str | None = None,
          max_captcha_rounds: int = MAX_CAPTCHA_ROUNDS, log=_log,
          device: dict | None = None, solver=None) -> dict:
    """Returns {'uid','token','unid','profile'} or raises ``LoginError``.

    ``device`` is the installation profile resolved by
    :mod:`replica.application.device_profile`.  When supplied it replaces the
    legacy model/os defaults, so login and submission use the same phone.

    ``solver`` is injectable for tests; it must accept
    ``(captcha_id=..., log=...)`` and return the four credentials.
    """
    solver = solver or GT4.solve_gt4
    acct = _account(username, device_model, os_version, device)
    client = RealClient()

    # The uuid ties checkGeeUse -> geevalidate -> login together.  The server
    # records "this uuid passed the captcha"; a different one gets 10003.
    login_uuid = str(uuid.uuid4())

    if _check_needs_captcha(client, acct, username, login_uuid, log):
        rounds = max(1, int(max_captcha_rounds))
        for rnd in range(1, rounds + 1):
            log('[gt4] round %d/%d: solving slider...' % (rnd, rounds))
            creds = _solve(solver, captcha_id, log)
            log('[gt4] ticket lot=%s' % str(creds['lotNumber'])[:12])
            err = _validate_ticket(client, acct, username, login_uuid,
                                   creds, log)
            if err == 10000:
                break
            if err == 10007:
                raise RiskControlError(
                    '服务端风控拒绝了本次验证（10007）。重试没用，'
                    '请换网络或等一段时间再试。')
            if err != 10003:
                raise LoginError('geevalidate 失败：error=%s' % err)
            if rnd == rounds:
                raise CaptchaFailed(
                    '极验票连续 %d 次被判低信任（10003）。'
                    '这通常是解题器跟不上服务端了，请点「手动滑一下」。' % rounds)
            time.sleep(2)

    return _submit_login(client, acct, username, password, login_uuid, log)


def login_with_ticket(username: str, password: str, ticket: dict,
                      device_model: str = 'NCO-AL00', os_version: str = '12',
                      log=_log, device: dict | None = None) -> dict:
    """Login with credentials produced by the manual slider path.

    The operator dragged the slice onto the gap; ``creds`` is what GeeTest's
    ``/verify`` returned for that distance.  We only do the two server
    round-trips, exactly like the automatic path does.
    """
    required = ('lotNumber', 'captchaOutput', 'passToken', 'genTime')
    missing = [k for k in required if not (ticket or {}).get(k)]
    if missing:
        raise LoginError('手动验证凭证不完整，缺少 %s' % missing)

    acct = _account(username, device_model, os_version, device)
    client = RealClient()
    login_uuid = str(uuid.uuid4())
    needs = _check_needs_captcha(client, acct, username, login_uuid, log)

    # If the server says this login does not need a captcha, sending the
    # ticket anyway can trip a parameter check.  Skip straight to login.
    if not needs:
        return _submit_login(client, acct, username, password, login_uuid, log)

    creds = {k: ticket[k] for k in required}
    err = _validate_ticket(client, acct, username, login_uuid, creds, log)
    if err == 10007:
        raise RiskControlError(
            '服务端风控拒绝了本次验证（10007）。请换网络或稍后再试。')
    if err != 10000:
        raise CaptchaFailed('geevalidate 拒绝了手动验证的票（error=%s）' % err)

    return _submit_login(client, acct, username, password, login_uuid, log)


if __name__ == '__main__':
    import argparse

    ap = argparse.ArgumentParser(description='pure-protocol login')
    ap.add_argument('--user', required=True)
    ap.add_argument('--pass', dest='password', required=True)
    ap.add_argument('--captcha-id', default=None)
    ap.add_argument('--out', default=None, help='write session json here')
    a = ap.parse_args()

    try:
        store = DP.default_store_dir()
        device = DP.load_or_create(store, legacy_session=DP.read_session(store))
        sess = login(a.user, a.password, captcha_id=a.captcha_id,
                     device=device)
    except Exception as e:  # noqa: BLE001
        print('\nLOGIN FAILED:', type(e).__name__, e)
        raise SystemExit(1)

    print('\n=== LOGIN OK ===')
    print('  uid   =', sess['uid'])
    print('  token =', sess['token'][:16] + '...')
    print('  unid  =', sess['unid'])
    print('  name  =', sess['profile'].get('name'))
    print('  campus=', sess['profile'].get('campusName'))
    print('  device=', device['deviceName'], '/',
          ('iOS' if device.get('platform') == 'ios' else 'Android'),
          device['osVersion'], '/', DP.mask_device_id(device['DeviceId']))
    if a.out:
        with open(a.out, 'w', encoding='utf-8') as fh:
            json.dump(sess, fh, ensure_ascii=False, indent=1)
        print('  saved ->', a.out)

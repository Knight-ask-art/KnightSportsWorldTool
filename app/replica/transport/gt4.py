"""GeeTest v4 slider — orchestration only.

The heavy lifting lives in two sibling modules, deliberately split:

    gt4_image.py    PNG -> gap x  (Canny + ZNCC)
    gt4_payload.py  gap -> `w`    (PoW, dynamic key, AES)
    gt4_rsa.py      the RSA half of `w` (hard-coded; refresh hook is off)

Upstream changes the field set far more often than the image pipeline, so a
field-only update must not require re-validating CV — and a CV change must not
be able to silently reshape the payload.

The flow is the same as before:

    GET  /load    -> bg/slice images + lot_number/payload/process_token/pow_detail
    (image)       -> gap x
    (PoW)         -> nonce so that sha256(pow_msg_base + nonce) starts with "00"
    (w)           -> hex(AES-128-CBC(payload)) || hex(RSA-PKCS1v15(str16))
    GET  /verify  -> {lotNumber, captchaOutput, passToken, genTime}

What is new here is **evidence**.  GeeTest answering ``success`` only means the
ticket is well formed; the app server scores it and may still reject with
10003.  When something fails, this module can now leave behind the `/load`
JSON (including the JS version), both images, the exact `w` plaintext and the
`/verify` reply — so the next incident is a diff, not another 49 rounds.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .gt4_image import dimensions, gap_distance, locate_gap
from .gt4_image import self_test as _image_self_test
from .gt4_payload import (build_payload, build_w, dynamic_key, passtime_for,
                          pow_msg, str16)
from .gt4_payload import self_test as _payload_self_test
from .gt4_rsa import (active_source as rsa_source, modulus_bits,
                      refresh as refresh_rsa)
from .gt4_rsa import self_test as _rsa_self_test

GEE_HOST = 'https://gcaptcha4.geetest.com'
STATIC_HOST = 'https://static.geetest.com/'
UA_WEB = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
          '(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36')

#: bizType=0 captcha id used by the iOS/web login chain (upstream constant).
CAPTCHA_ID_BIZ0 = '8c065103d81f5fd3efec8ad3e3a84c30'

#: candidate ids, tried in order when the caller does not pin one.  The second
#: value is the one present in our 7.2.70/7.3.11 resources (gt4_captcha_login).
CAPTCHA_ID_CANDIDATES = [
    CAPTCHA_ID_BIZ0,
    '3b02ad39bd099fd3a8336d9347a189ab',
]

LOAD_FIELDS = ('bg', 'slice', 'lot_number', 'payload', 'process_token',
               'pow_detail')


# --------------------------------------------------------------------------
# error taxonomy
# --------------------------------------------------------------------------

class GeeError(RuntimeError):
    """Base class: anything that stopped us from producing a ticket."""


class GeeUnavailable(GeeError):
    """Network/HTTP problem.  Retrying the same captcha id may help."""


class GeeSchemaChanged(GeeError):
    """``/load`` no longer looks like the schema we ship for."""


class GeeRejected(GeeError):
    """GeeTest itself said no (``status != success`` or ``forbidden``).

    Retrying the same captcha id is pointless; try the next id, then fall back
    to a human.  Deliberately distinct from :class:`GeeUnavailable` so the
    retry policy does not hammer a server that already refused us.
    """

    def __init__(self, message: str, *, status: str = '', code=''):
        super().__init__(message)
        self.status = status
        self.code = code


class GeeImageError(GeeError):
    """Local CV failed (blank slice, unexpected size)."""


# --------------------------------------------------------------------------
# diagnostics
# --------------------------------------------------------------------------

def data_root() -> str:
    """Same rule as ``device_profile.default_store_dir``, without the layering."""
    env = os.environ.get('JHP_DATA_DIR')
    if env:
        return env
    from ..build_info import app_root
    return os.path.join(app_root(), 'data')


def dump_dir() -> str:
    return os.environ.get('JHP_GT4_DUMP_DIR') or os.path.join(
        data_root(), 'diagnostics', 'gt4')


def dump_policy() -> str:
    return str(os.environ.get('JHP_GT4_DUMP', 'fail')).strip().lower() or 'fail'


def _redact(value):
    """Shorten anything that is a credential, keep everything else intact."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            low = key.lower()
            if low in ('pass_token', 'passtoken', 'captcha_output',
                       'captchaoutput'):
                out[key] = (str(item)[:8] + '…') if item else ''
            else:
                out[key] = _redact(item)
        return out
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def write_dump(trace: dict, policy: str | None = None) -> str:
    """Persist one attempt under ``data/diagnostics/gt4/<stamp>/``."""
    policy = policy or dump_policy()
    ok = bool(trace.get('ok'))
    if policy == 'off' or (policy != 'always' and ok):
        return ''
    # Microseconds + captcha id: two ids can fail inside the same second, and a
    # second-resolution stamp would silently overwrite the first attempt.
    stamp = _dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    cid = str(trace.get('captcha_id') or '')[:8]
    base = os.path.join(dump_dir(), stamp + ('-' + cid if cid else ''))
    try:
        os.makedirs(base, exist_ok=True)
        meta = _redact({k: v for k, v in trace.items()
                        if k not in ('bg', 'slice')})
        with open(os.path.join(base, 'attempt.json'), 'w',
                  encoding='utf-8') as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=1)
        for name in ('bg', 'slice'):
            blob = trace.get(name)
            if isinstance(blob, bytes) and blob:
                with open(os.path.join(base, name + '.png'), 'wb') as fh:
                    fh.write(blob)
    except Exception:  # noqa: BLE001 - diagnostics must never break a login
        return ''
    return base


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _get(url: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={'User-Agent': UA_WEB})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as exc:
        raise GeeUnavailable('HTTP %s from %s' % (exc.code, url.split('?')[0])) from exc
    except urllib.error.URLError as exc:
        raise GeeUnavailable('网络错误: %s' % exc.reason) from exc
    except OSError as exc:
        raise GeeUnavailable('网络错误: %s' % exc) from exc


def _text(url: str, timeout: float = 30.0) -> str:
    """Never let a stray byte turn a protocol error into a UnicodeDecodeError."""
    return _get(url, timeout).decode('utf-8', 'replace')


def _jsonp(text: str) -> dict:
    start, end = text.find('('), text.rfind(')')
    if start < 0 or end <= start:
        raise GeeSchemaChanged('JSONP 格式变了: %s' % text[:120])
    try:
        return json.loads(text[start + 1:end])
    except ValueError as exc:
        raise GeeSchemaChanged('JSONP 解析失败: %s' % text[:120]) from exc


def _summarise(root: dict) -> tuple[str, object]:
    status = str(root.get('status') or '')
    code = root.get('code')
    if code is None and isinstance(root.get('data'), dict):
        code = root['data'].get('code')
    return status, code


# --------------------------------------------------------------------------
# main entry
# --------------------------------------------------------------------------

def solve_gt4(captcha_id: str | None = None, attempts: int = 1,
              log=None, dump: bool = True,
              trace_out: dict | None = None) -> dict:
    """Return ``{'lotNumber','captchaOutput','passToken','genTime', ...}``.

    Retry policy (deliberately boring): try each captcha id once by default.
    A ``GeeRejected`` moves straight to the next id; only a transport error
    earns a re-try.  Hammering the same id is both useless and noisy — the
    caller is expected to fall back to a human after this returns.
    """
    log = log or (lambda *_: None)
    ids = [captcha_id] if captcha_id else list(CAPTCHA_ID_CANDIDATES)
    attempts = max(1, int(attempts))
    last = None
    for cid in ids:
        for attempt in range(1, attempts + 1):
            trace = trace_out if trace_out is not None else {}
            trace.clear()
            trace.update({'captcha_id': cid, 'attempt': attempt, 'ok': False})
            try:
                return _solve_once(cid, log, trace, dump)
            except GeeRejected as exc:
                last = exc
                trace.update({'error': str(exc),
                              'error_type': type(exc).__name__})
                log('[gt4] cid=%s rejected (%s); next id' % (cid[:8], exc))
                if dump:
                    write_dump(trace)
                break
            except GeeError as exc:
                last = exc
                trace['error'] = str(exc)
                trace['error_type'] = type(exc).__name__
                log('[gt4] cid=%s attempt %d/%d failed: %s'
                    % (cid[:8], attempt, attempts, exc))
                if dump:
                    write_dump(trace)
                if attempt < attempts:
                    time.sleep(1.5)
    raise last or GeeError('GT4 failed')


def load_challenge(captcha_id: str | None = None, log=None,
                   trace: dict | None = None) -> dict:
    """``GET /load`` + both images, in one record.

    Split out of :func:`_solve_once` so the manual fallback can reuse the exact
    same protocol step.  The manual path never runs the CV: a human looks at the
    gap and reports the distance, and :func:`submit_distance` finishes the job
    with the same ``w`` builder.  One protocol implementation, two ways to find
    the number.
    """
    log = log or (lambda *_: None)
    trace = trace if trace is not None else {}
    cid = captcha_id or CAPTCHA_ID_CANDIDATES[0]
    challenge = uuid.uuid4().hex
    cb = 'geetest_%d' % int(time.time() * 1000)
    load_url = ('%s/load?callback=%s&captcha_id=%s&challenge=%s'
                '&client_type=web&risk_type=slide&lang=zh'
                % (GEE_HOST, cb, cid, challenge))
    root = _jsonp(_text(load_url))
    data = root.get('data')
    if not isinstance(data, dict):
        raise GeeSchemaChanged('/load 没有 data 对象: %s'
                               % json.dumps(root, ensure_ascii=False)[:160])

    missing = [f for f in LOAD_FIELDS if f not in data]
    if missing:
        raise GeeSchemaChanged(
            '/load 缺少字段 %s（新版本可能改了协议）；现有字段：%s'
            % (missing, sorted(data.keys())[:12]))

    # Version signals.  These are the cheapest upgrade detector GeeTest gives
    # us, and the old code threw them away.
    static_path = str(data.get('static_path') or '')
    trace.update({
        'captcha_id': cid,
        'static_path': static_path,
        'captcha_mode': data.get('captcha_mode'),
        'check_device': data.get('check_device'),
        'load_keys': sorted(data.keys()),
        'lot_number': data['lot_number'],
        'load': root,
    })
    if static_path:
        log('[gt4] js=%s mode=%s check_device=%s'
            % (static_path, data.get('captcha_mode'), data.get('check_device')))
        try:
            info = refresh_rsa(static_path, os.path.join(data_root(), 'cache', 'gt4'),
                               log=log)
            trace['rsa'] = info
        except Exception as exc:  # noqa: BLE001 - never fatal
            trace['rsa'] = {'error': str(exc)[:120]}
    trace['rsa_bits'] = modulus_bits()
    trace['rsa_source'] = rsa_source()

    pow_detail = data.get('pow_detail')
    if not isinstance(pow_detail, dict):
        raise GeeSchemaChanged('/load 的 pow_detail 不是对象（%s）'
                               % type(pow_detail).__name__)

    bg = _get(STATIC_HOST + data['bg'])
    sl = _get(STATIC_HOST + data['slice'])
    trace['bg'], trace['slice'] = bg, sl
    try:
        width, height = dimensions(bg)
    except ValueError as exc:
        raise GeeImageError('背景图解码失败: %s' % exc) from exc

    return {
        'captcha_id': cid,
        'lot_number': data['lot_number'],
        'payload': data['payload'],
        'process_token': data['process_token'],
        'pow_detail': pow_detail,
        'static_path': static_path,
        'bg': bg,
        'slice': sl,
        'bg_width': width,
        'bg_height': height,
        'slice_width': dimensions(sl)[0],
        'slice_height': dimensions(sl)[1],
        '_started': time.time(),
    }


def submit_distance(challenge: dict, distance: int, log=None,
                    trace: dict | None = None, dump: bool = True) -> dict:
    """``GET /verify`` for a known distance; returns the four credentials.

    ``distance`` is in **image pixels** (``setLeft``), exactly what
    :func:`replica.transport.gt4_image.locate_gap` returns and exactly what the
    manual UI reports after the human lines the slice up with the gap.
    """
    log = log or (lambda *_: None)
    trace = trace if trace is not None else {}
    cid = challenge['captcha_id']
    lot = challenge['lot_number']
    bg_width = int(challenge['bg_width'])
    try:
        dist = int(distance)
    except (TypeError, ValueError) as exc:
        raise GeeError('滑块距离不是整数: %r' % (distance,)) from exc
    if dist < 0 or dist >= bg_width:
        raise GeeError('滑块距离 %d 超出图片宽度 %d' % (dist, bg_width))

    pow_detail = challenge['pow_detail']
    pmsg = pow_msg(pow_detail, cid, lot)
    psign = hashlib.sha256(pmsg.encode()).hexdigest()
    key16 = str16()
    # Build the plaintext once.  `build_w` would otherwise re-derive a random
    # passtime and the dumped payload would not be the one we actually sent.
    passtime = passtime_for(dist)
    try:
        plaintext = build_payload(dist, lot_number=lot, pow_msg=pmsg,
                                  pow_sign=psign, bg_width=bg_width,
                                  passtime=passtime)
        w = build_w(dist, lot, pmsg, psign, key16, bg_width=bg_width,
                    passtime=passtime)
    except ValueError as exc:
        raise GeeSchemaChanged('w 字段构造失败: %s' % exc) from exc
    trace['payload'] = plaintext
    trace['payload_keys'] = list(json.loads(plaintext).keys())

    cb2 = 'geetest_%d' % int(time.time() * 1000)
    verify_url = ('%s/verify?callback=%s&captcha_id=%s&client_type=web'
                  '&lot_number=%s&risk_type=slide&payload=%s'
                  '&process_token=%s&payload_protocol=1&pt=1&w=%s'
                  % (GEE_HOST, cb2, cid,
                     urllib.parse.quote(lot),
                     urllib.parse.quote(challenge['payload']),
                     urllib.parse.quote(challenge['process_token']),
                     urllib.parse.quote(w)))
    vroot = _jsonp(_text(verify_url))
    trace['verify'] = vroot
    status, code = _summarise(vroot)
    if status != 'success':
        raise GeeRejected(
            'GT4 /verify 拒绝: status=%s code=%s' % (status, code),
            status=status, code=code)

    vdata = vroot.get('data')
    # A wrong answer is reported as status="success" with
    # data.result="fail" (plus fail_count) and no `seccode`.  Treating that as
    # a schema change sent every miss down the "upstream changed the protocol"
    # path, which is the one diagnosis that is almost always wrong.
    verdict = ''
    if isinstance(vdata, dict):
        verdict = str(vdata.get('result') or '').strip().lower()
    if verdict and verdict not in ('success', 'ok', 'pass'):
        raise GeeRejected(
            'GT4 /verify 判定未通过: result=%s fail_count=%s'
            % (verdict, vdata.get('fail_count')),
            status=verdict, code=vdata.get('fail_count'))
    sec = vdata.get('seccode') if isinstance(vdata, dict) else None
    if not isinstance(sec, dict):
        raise GeeSchemaChanged('/verify 成功但 seccode 不是对象: %s'
                               % json.dumps(vroot, ensure_ascii=False)[:160])
    creds = {
        'lotNumber': sec.get('lot_number'),
        'captchaOutput': sec.get('captcha_output'),
        'passToken': sec.get('pass_token'),
        'genTime': sec.get('gen_time'),
    }
    if not all(creds.values()):
        raise GeeSchemaChanged('/verify 成功但凭证缺字段: %s'
                               % sorted(sec.keys()))
    started = trace.get('_started') or challenge.get('_started') or time.time()
    trace.update({'ok': True, 'elapsed_ms': int((time.time() - started) * 1000),
                  'captcha_output': creds['captchaOutput'],
                  'pass_token': creds['passToken']})
    if dump:
        write_dump(trace)
    return creds


def _solve_once(cid: str, log, trace: dict, dump: bool) -> dict:
    started = time.time()
    ch = load_challenge(cid, log=log, trace=trace)
    trace['_started'] = started

    try:
        gap = locate_gap(ch['bg'], ch['slice'])
    except ValueError as exc:
        raise GeeImageError('缺口识别失败: %s' % exc) from exc
    dist = gap['x']
    trace.update({'gap': dist, 'gap_y': gap['y'],
                  'bg_width': gap['bg_width'],
                  'bg_height': gap['bg_height'], 'source': 'solver'})
    log('[gt4] gap distance = %d px (bg %dx%d)'
        % (dist, gap['bg_width'], gap['bg_height']))
    return submit_distance(ch, dist, log=log, trace=trace, dump=dump)


# --------------------------------------------------------------------------
# canary: is GeeTest still willing to talk to us?
# --------------------------------------------------------------------------

def probe(captcha_id: str | None = None, log=None) -> dict:
    """Solve a slider and stop before ``geevalidate``.

    This never touches the account, so a failure here means "the solver or
    GeeTest changed", while a pass here plus a 10003 later means "the business
    server scored our ticket low".  That distinction is what rounds 44-49 were
    missing.
    """
    started = time.time()
    trace: dict = {}
    try:
        creds = solve_gt4(captcha_id, log=log, dump=False, trace_out=trace)
        return {
            'ok': True, 'stage': 'verify',
            'captcha_id': captcha_id or CAPTCHA_ID_CANDIDATES[0],
            'lot_number': creds['lotNumber'],
            'static_path': trace.get('static_path', ''),
            'bg_width': trace.get('bg_width'),
            'gap': trace.get('gap'),
            'elapsed_ms': int((time.time() - started) * 1000),
            'rsa_source': rsa_source(), 'rsa_bits': modulus_bits(),
        }
    except GeeError as exc:
        return {
            'ok': False, 'stage': type(exc).__name__,
            'captcha_id': captcha_id or CAPTCHA_ID_CANDIDATES[0],
            'static_path': trace.get('static_path', ''),
            'error': str(exc),
            'elapsed_ms': int((time.time() - started) * 1000),
            'rsa_source': rsa_source(), 'rsa_bits': modulus_bits(),
        }


# --------------------------------------------------------------------------
# offline self-check
# --------------------------------------------------------------------------

def self_test() -> list[tuple[str, bool, str]]:
    out = []
    out.extend(_image_self_test())
    out.extend(_payload_self_test())
    out.extend(_rsa_self_test())

    # The dynamic key is the field that caused months of 10003.
    lot = 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6'
    out.append(('dynamic key is 8 chars', len(dynamic_key(lot)) == 8,
                dynamic_key(lot)))

    # Error classes must stay distinguishable: the retry policy keys off them.
    out.append(('rejected carries status',
                getattr(GeeRejected('x', status='fail'), 'status') == 'fail', ''))
    out.append(('schema error is not rejected',
                not issubclass(GeeSchemaChanged, GeeRejected), ''))

    # A dump must be able to run without touching the real data directory.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        old = os.environ.get('JHP_GT4_DUMP_DIR')
        os.environ['JHP_GT4_DUMP_DIR'] = tmp
        try:
            path = write_dump({'ok': False, 'error': 'x',
                               'pass_token': 'secret-value'},
                              policy='fail')
            wrote = bool(path) and os.path.isfile(
                os.path.join(path, 'attempt.json'))
            body = ''
            if wrote:
                with open(os.path.join(path, 'attempt.json'),
                          encoding='utf-8') as fh:
                    body = fh.read()
        finally:
            if old is None:
                os.environ.pop('JHP_GT4_DUMP_DIR', None)
            else:
                os.environ['JHP_GT4_DUMP_DIR'] = old
    out.append(('failure leaves a dump', wrote, path or 'none'))
    out.append(('credentials redacted', 'secret-value' not in body, ''))

    # Success must not dump unless asked to.
    with tempfile.TemporaryDirectory() as tmp:
        old = os.environ.get('JHP_GT4_DUMP_DIR')
        os.environ['JHP_GT4_DUMP_DIR'] = tmp
        try:
            path = write_dump({'ok': True}, policy='fail')
        finally:
            if old is None:
                os.environ.pop('JHP_GT4_DUMP_DIR', None)
            else:
                os.environ['JHP_GT4_DUMP_DIR'] = old
    out.append(('success is silent by default', path == '', path))
    return out


if __name__ == '__main__':
    import argparse

    ap = argparse.ArgumentParser(description='GeeTest v4 solver')
    ap.add_argument('--canary', action='store_true',
                    help='solve one slider and stop before geevalidate')
    ap.add_argument('--json', action='store_true', help='machine-readable')
    ap.add_argument('--captcha-id', default=None)
    args = ap.parse_args()

    if args.canary:
        report = probe(args.captcha_id, log=lambda m: None)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=1))
        else:
            print('RESULT:', 'PASS' if report['ok'] else 'FAIL')
            for key in ('stage', 'captcha_id', 'static_path', 'rsa_source',
                        'rsa_bits', 'elapsed_ms', 'error'):
                if report.get(key) not in (None, ''):
                    print('  %-12s %s' % (key, report[key]))
        raise SystemExit(0 if report['ok'] else 1)

    ok = True
    for name, passed, detail in self_test():
        print('%-34s %s   %s' % (name, 'PASS' if passed else 'FAIL', detail))
        ok = ok and passed
    print('\nRESULT:', 'PASS' if ok else 'FAIL')
    raise SystemExit(0 if ok else 1)

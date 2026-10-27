"""Real-server client for 好想跑 (Coaixy 7.3.70 protocol).

REAL mode only — no mocks. Every request goes through:
  headers(24 fields, tokenSign=MD5) + headerSign=w30f(headers) [unidbg]
  body=w3270(base64(signed entity)) [unidbg]  ->  POST https://run.gxapp.iydsj.com/...

Response classification (from archive/unidbg-era/STATUS.md, all real-observed):
  200 + 0 bytes            -> soft reject (stale timeStamp etc.)
  200 + envelope(len>0)    -> ACCEPTED
  200/4xx + Spring JSON    -> explicit error
"""
import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, asdict

from .routes import request_user_info
from .sidecar import get_sidecar

HOST = 'run.gxapp.iydsj.com'
BASE = f'https://{HOST}'
SUBMIT_URL = f'{BASE}/api/v70260/runnings/save/record'
LOGIN_URL = f'{BASE}/api/v70100/login'
# Session teardown.  Official APK endpoint (v6), not a v70100 guess:
#   POST https://run.gxapp.iydsj.com/api/v6/user/logout
# It uses the normal signed headers (uid/token/tokenSign) and a `{}` body.
LOGOUT_URL = f'{BASE}/api/v6/user/logout'
SALT5 = '2slhe02lsfiwowlcixisla_sls-_slaor'


@dataclass
class Account:
    """One student = one real session (uid/token from a logged-in capture)."""
    name: str
    uid: str
    token: str
    schoolId: int = 12345
    schoolName: str = '广州软件学院'
    campusName: str = '江门校区'
    unid: int = 47203
    runMode: int = 1
    # device fingerprint (must stay consistent per account once submitted)
    platform: str = 'android'
    DeviceId: str = 'afa505ae1e60377972564c220fb24311'
    deviceName: str = 'Xiaomi 2304FPN6DG'
    osVersion: str = '9'
    cpuModel: str = 'x86_64'
    physicPixel: str = '900x1600'
    logicPixel: str = '600x1066'
    appInstallTime: str = '1785436971435'
    appUpdateTime: str = '1788581965711'
    isRoot: str = '1'
    idfa: str = ''
    # The server enforces a minimum client version: api/v70103/runModePolicy
    # answers `11019 ERROR:版本过低，请更新至7.3.10版本` for anything older, and
    # the official CDN ships sw_7.3.11.apk as the current build.
    appVersion: str = '7.3.11'
    # Run area centre (GCJ).  0/0 means "not resolved yet": the caller must
    # ask the server for this account's geofence and use its centre.  There is
    # deliberately no built-in campus default — the old 24.87845/118.673878
    # (Xiamen) and the 22.410787/112.940804 (江门) constants both ended up
    # submitting for the wrong campus when the session had no coordinates.
    center_lat: float = 0.0
    center_lng: float = 0.0

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> 'Account':
        known = {f for f in Account.__dataclass_fields__}
        return Account(**{k: v for k, v in d.items() if k in known})


def token_sign(ts: str, token: str, uid: str) -> str:
    link = f'timeStamp={ts}&token={token}&uid={uid}'
    return hashlib.md5((link + SALT5).encode()).hexdigest()


def _java_str_hash(s: str) -> int:
    h = 0
    for c in s:
        h = (h * 31 + ord(c)) & 0xFFFFFFFF
    return h


def java_hashmap_json(d: dict) -> str:
    """Gson-style JSON of a Java HashMap<String,String> (bucket iteration order).

    Java HashMap capacity is the next power of two with size <= 0.75*capacity;
    entries iterate by bucket index, then by insertion order within a bucket.
    """
    cap = 16
    while len(d) > cap * 0.75:
        cap *= 2
    buckets: dict[int, list[str]] = {}
    for k in d:
        h = _java_str_hash(k)
        h ^= h >> 16                      # HashMap.hash()
        buckets.setdefault(h & (cap - 1), []).append(k)
    ordered = []
    for b in sorted(buckets):
        ordered.extend(buckets[b])
    body = ",".join(
        '"%s":"%s"' % (k, str(d[k]).replace('\\', '\\\\').replace('"', '\\"'))
        for k in ordered)
    return "{" + body + "}"


def build_headers(acct: Account, ts: str, nonce: str) -> dict:
    """Build the signed header set for this account's platform.

    Android keeps the byte-for-byte proven 23-field set.  iOS follows the
    native app's field set/order and types (see ``ios_headers``).
    """
    if str(getattr(acct, 'platform', 'android') or 'android').lower() == 'ios':
        from .ios_headers import build_ios_headers
        return build_ios_headers(acct, ts, nonce)
    h = {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'appVersion': acct.appVersion,
        'physicPixel': acct.physicPixel,
        'logicPixel': acct.logicPixel,
        'isRoot': acct.isRoot,
        'osType': '0',
        'uid': acct.uid,
        'IMEI': '',
        'timeStamp': ts,
        'blMac': '',
        'nonce': nonce,
        'token': acct.token,
        'tokenSign': token_sign(ts, acct.token, acct.uid),
        'cpuModel': acct.cpuModel,
        'deviceName': acct.deviceName,
        'appInstallTime': acct.appInstallTime,
        'androidId': '',
        'DeviceId': acct.DeviceId,
        'appUpdateTime': acct.appUpdateTime,
        'wifiMac': '',
        'CustomDeviceId': '',
        'osVersion': acct.osVersion,
    }
    # NOTE: the app sends exactly these 23 headers. It does NOT send Host or
    # User-Agent (captured headerSign plaintext has neither), and adding them
    # changes the signed JSON -> server answers 10003 "验证失败".
    return h


def user_agent(acct: Account | None = None,
               platform: str | None = None,
               os_version: str = '') -> str:
    """A wire-only User-Agent for this platform.

    This is deliberately not part of the signed header set: Android's
    headerSign has no UA, and the server accepts unsigned extras (runes/runef
    prove that).  Without it urllib advertises ``Python-urllib/x.y``, which is
    a louder signal than any device field.

    iOS's Darwin major must match the advertised iOS major; an iOS 16 profile
    sending an iOS 18 Darwin string is another synthetic pair.
    """
    name = platform or getattr(acct, 'platform', 'android') or 'android'
    if str(name).lower() == 'ios':
        version = os_version or getattr(acct, 'osVersion', '') or '18.1'
        try:
            major = int(str(version).split('.')[0])
        except (TypeError, ValueError):
            major = 18
        cfnetwork, darwin = {
            16: ('1408.0.4', '22.5.0'),
            17: ('1474', '23.0.0'),
            18: ('1498.700.2', '24.1.0'),
            26: ('3826.400.72', '25.0.0'),
        }.get(major, ('1498.700.2', '24.1.0'))
        return f'CFNetwork/{cfnetwork} Darwin/{darwin}'
    return 'okhttp/4.12.0'


def classify(status: int, body: bytes) -> tuple[str, str]:
    """-> (verdict, detail). verdict in accepted/soft-reject/error/empty."""
    if status == 200 and len(body) == 0:
        return 'soft-reject', '200 + 0 bytes (stale timeStamp / mild refusal)'
    if status == 200 and len(body) > 0:
        head = body[:64]
        if head.startswith(b'{') and (b'"r"' in body[:32] or b'encrypted' in body[:200]):
            return 'accepted', f'200 + envelope ({len(body)} bytes)'
        return 'accepted', f'200 + {len(body)} bytes'
    snippet = body[:200].decode('utf-8', 'replace')
    return 'error', f'HTTP {status}: {snippet}'


def as_dict(v) -> dict:
    """Server payloads sometimes carry JSON as a string (`data` field)."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:  # noqa: BLE001
            return {}
    return v if isinstance(v, dict) else {}


_as_dict = as_dict


class RealClient:
    """Real-server client with a pluggable crypto backend.

    backend='netsec'  (default) — pure Python, see replica/netsec.py. No JVM,
                                 no unidbg, no oracle.apk.
    backend='unidbg'            — legacy native path, kept as a fallback.

    Override with the JHP_CRYPTO environment variable. Both backends have been
    observed to produce byte-identical server behaviour.
    """

    def __init__(self, sidecar=None, backend: str | None = None,
                 proxy: str | None = None):
        self.backend = (backend or os.environ.get('JHP_CRYPTO') or 'netsec').lower()
        self._ns = None
        self._sc = None
        if self.backend == 'unidbg':
            self._sc = sidecar or get_sidecar()
        else:
            from . import netsec
            self._ns = netsec.EnvelopeSession()

        # Outbound proxy.  The run-detail page shows the *IP* location, so an
        # exit in a different city from the campus is the single loudest signal
        # a record can carry (upstream issues #2 / #16).  Installing a global
        # opener also routes the OBS PUT through the same exit.
        self.proxy = proxy or os.environ.get('JHP_PROXY') or None
        if self.proxy:
            import urllib.request
            handler = urllib.request.ProxyHandler({'http': self.proxy,
                                                   'https': self.proxy})
            urllib.request.install_opener(urllib.request.build_opener(handler))
        self.timeout = float(os.environ.get('JHP_HTTP_TIMEOUT') or 30.0)

    # ---- request crypto ----

    def header_sign(self, headers: dict, platform: str = 'android') -> str:
        # The Android app does: HashMap map = new HashMap(); put every request
        # header; new Gson().toJson(map); encryptString(json); addHeader
        # ("headerSign",...).  The JSON key order is Java HashMap bucket order
        # (String.hashCode), NOT insertion order.
        #
        # The iOS app builds a typed JSON object in field order instead.  Using
        # the Android serializer there would stringify uid/isRoot and change
        # the bytes, so the two platforms must not share this step.
        if str(platform or 'android').lower() == 'ios':
            from .ios_headers import ios_header_json
            hjson = ios_header_json(headers)
        else:
            hjson = java_hashmap_json(headers)
        if self._ns is not None:
            from . import netsec
            return netsec.build_envelope(self._ns, hjson, 'observed')['json']
        return self._sc.w3270_sig(hjson)

    def encrypt_body(self, plain: str) -> str:
        # Outer keys follow the "insert" order [d,h,k,p,t] for request bodies.
        if self._ns is not None:
            from . import netsec
            return netsec.build_envelope(self._ns, plain, 'insert')['json']
        return self._sc.w3270(plain)

    def decrypt_body(self, response: str) -> str:
        """Decrypt a full response envelope -> plaintext JSON string."""
        if self._ns is not None:
            from . import netsec
            return netsec.decrypt_response(response, self._ns)['plaintext']
        from . import nskcodec
        return nskcodec.decrypt_d(self._sc.session_key(), response.strip())

    def decrypt_payload(self, response: str) -> object:
        """Decrypt a response envelope and return the inner `data` field."""
        if self._ns is not None:
            from . import netsec
            return netsec.decrypt_response(response, self._ns)['business']
        from . import nskcodec
        import base64 as _b64
        import json as _json
        plain = nskcodec.decrypt_d(self._sc.session_key(), response.strip())
        raw = _json.loads(plain)['data']
        try:
            return _json.loads(_b64.b64decode(raw))
        except Exception:
            return raw

    # ---- real requests ----

    def _post(self, url: str, headers: dict, body: str,
              timeout: float = 30.0, platform: str = 'android',
              extra_headers: dict | None = None):
        hdrs = dict(headers)
        hdrs['headerSign'] = self.header_sign(headers, platform)
        # Wire-only defaults.  iOS's signed set does not include these; the
        # HTTP client still needs them, and they must not enter headerSign.
        hdrs.setdefault('Accept', 'application/json')
        hdrs.setdefault('Content-Type', 'application/json')
        hdrs.setdefault('User-Agent', user_agent(
            platform=platform, os_version=str(headers.get('osVersion') or '')))
        if extra_headers:
            hdrs.update(extra_headers)
        req = urllib.request.Request(url, data=body.encode('utf-8'),
                                     headers=hdrs, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def decode_envelope(self, response: bytes | str) -> str | None:
        """Decode a NetSecKit response envelope into business JSON text.

        netsec backend: r/s/v verified fail-closed (RSA-1024 PKCS#1 type-1
        signature over MD5 of the inner envelope), then two AES layers.
        Unencrypted replies (401/404 plain JSON) pass through unchanged.
        """
        text = response.decode('utf-8', 'replace') if isinstance(response, bytes) else response
        text = text.strip()
        if not text.startswith('{'):
            return None
        if self._ns is not None:
            from . import netsec
            dec = netsec.decrypt_response(text, self._ns)
            return json.dumps(dec['business'], ensure_ascii=False)
        import base64 as _b64
        pt = self._sc.dec(text)
        try:
            return _b64.b64decode(pt).decode('utf-8', 'replace')
        except Exception:
            return pt

    #: Transport-level failures worth retrying on an idempotent call.
    RETRYABLE = (urllib.error.URLError, TimeoutError, ConnectionError, OSError)
    #: Backoff base in seconds (tests set this to 0).
    RETRY_BASE = 0.6

    def request(self, acct: Account, path: str, payload: dict | None = None,
                method: str = 'POST', timeout: float | None = None,
                extra_headers: dict | None = None, retries: int = 0) -> dict:
        """Authenticated call with optional transport retries.

        `retries` is only safe for **idempotent** calls (policy / points /
        records / violation list).  Never pass it for save/record: retrying a
        submission can create a second run.
        """
        last = None
        for attempt in range(retries + 1):
            try:
                return self._request_once(acct, path, payload, method, timeout,
                                          extra_headers)
            except self.RETRYABLE as e:
                last = e
                if attempt >= retries:
                    break
                time.sleep(self.RETRY_BASE * (2 ** attempt))
        raise last

    def _request_once(self, acct: Account, path: str, payload: dict | None = None,
                      method: str = 'POST', timeout: float | None = None,
                      extra_headers: dict | None = None) -> dict:
        """One authenticated call against the real server, envelope-decoded.

        `extra_headers` are merged in AFTER the signed set is serialised — they
        therefore do NOT participate in headerSign (that is exactly how the app
        handles Authorization on the login call).

        Returns {'status', 'raw_len', 'plain', 'json', 'error'} and never raises
        for server-side errors — only transport failures propagate.
        """
        ts = str(int(time.time() * 1000))
        nonce = str(uuid.uuid4())
        platform = str(getattr(acct, 'platform', 'android') or 'android').lower()
        headers = build_headers(acct, ts, nonce)
        signature = self.header_sign(headers, platform)
        wire = dict(headers)
        # Wire-only fields: never signed.  Objective-C/iOS and okhttp both add
        # a real UA and Content-Type; Python's defaults are a detection signal.
        wire.setdefault('Accept', 'application/json')
        if method.upper() != 'GET':
            wire.setdefault('Content-Type', 'application/json')
        wire.setdefault('User-Agent', user_agent(acct))
        if extra_headers:
            wire.update(extra_headers)
        wire['headerSign'] = signature
        if timeout is None:
            timeout = self.timeout
        url = f'{BASE}/{path.lstrip("/")}'
        if method.upper() == 'GET':
            req = urllib.request.Request(url, headers=wire, method='GET')
        else:
            body = self.encrypt_body(
                json.dumps(payload or {}, separators=(',', ':')))
            req = urllib.request.Request(url, data=body.encode(),
                                         headers=wire, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                status, data = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, data = e.code, e.read()
        out = {'status': status, 'raw_len': len(data), 'plain': None,
               'json': None, 'error': None}
        try:
            plain = self.decode_envelope(data)
        except Exception as e:  # noqa: BLE001
            out['error'] = f'decode failed: {e}'
            return out
        out['plain'] = plain
        if plain:
            try:
                out['json'] = json.loads(plain)
            except Exception:
                pass
        return out

    def submit_run(self, acct: Account, run: dict, ent: dict, signed: dict,
                   policy_ts: int | None = None) -> dict:
        """Full real submission.

        `runes` = <policy timestamp><uid> and `runef` = <run uuid><start ms> are
        the two anti-cheat headers the Android client adds on this call (see
        upstream submit.rs L254-264).  They are NOT part of headerSign.

        run/entity/signed come from replica.rundata (generate_run_data /
        build_upload_entity / sign_upload_entity).
        """
        extra = {
            'runes': '%s%s' % (policy_ts or int(time.time() * 1000), acct.uid),
            'runef': '%s%s' % (signed.get('uuid', ''), signed.get('startTime', '')),
        }
        res = self.request(acct, 'api/v70260/runnings/save/record', signed,
                           extra_headers=extra)
        data = _as_dict(res.get('json'))
        # `data` can arrive as a JSON-encoded string; failing to unwrap it used
        # to yield rrid=None, which the caller reports as "rejected" and would
        # then allow a retry of an already-accepted record.
        payload = _as_dict(data.get('data'))
        error = data.get('error')
        if res.get('error'):
            verdict = 'decode-failed'
        elif res.get('status') != 200 or not res.get('raw_len'):
            verdict = 'empty'
        elif error == 10000:
            verdict = 'accepted'
        elif error is None:
            verdict = 'unknown'
        else:
            verdict = 'rejected'
        return {
            'verdict': verdict,
            'status': res.get('status'),
            'resp_len': res.get('raw_len'),
            'plains': res.get('plain'),
            'error': error,
            'message': data.get('message'),
            'rrid': payload.get('rrid'),
            'uuid': payload.get('uuid'),
            'completed': payload.get('completed'),
            'runes': extra['runes'],
            'runef': extra['runef'],
        }

    def login(self, username: str, password: str, acct: Account,
              smscode: str | None = None) -> dict:
        """Real login POST. NOTE: response body is NetSecKit-encrypted and
        cannot be parsed yet — the returned token cannot be extracted until
        response decryption is solved. Provided for completeness/testing."""
        ts = str(int(time.time() * 1000))
        nonce = str(uuid.uuid4())
        platform = str(getattr(acct, 'platform', 'android') or 'android').lower()
        headers = build_headers(acct, ts, nonce)
        if platform != 'ios':
            # Not logged in yet: the Android app sends uid "-1" and an EMPTY
            # tokenSign (see the captured headerSign plaintext), not a
            # computed MD5.  iOS simply omits uid/token/tokenSign.
            headers['uid'] = '-1'
            headers['token'] = ''
            headers['tokenSign'] = ''
        body_obj = {
            'loginType': 1,
            'device_model': acct.deviceName,
            'os_version': acct.osVersion,
            'uuid': nonce,
        }
        if smscode:
            body_obj['smscode'] = smscode
        auth = 'Basic ' + base64.b64encode(
            f'{username}:{password}'.encode()).decode()
        plain = json.dumps(body_obj, separators=(',', ':'))
        body = self.encrypt_body(plain)
        status, data = self._post(LOGIN_URL, headers, body, platform=platform,
                                  extra_headers={'Authorization': auth})
        verdict, detail = classify(status, data)
        out = {'verdict': verdict, 'detail': detail, 'status': status,
               'resp_len': len(data), 'resp_head': data[:80].decode('utf-8', 'replace')}
        try:
            text = data.decode('utf-8', 'replace').strip()
            if text.startswith('{'):
                out['raw'] = text
                out['resp_plain'] = self.decrypt_body(text)
                try:
                    out['resp'] = json.loads(out['resp_plain'])
                except Exception:
                    out['resp'] = out['resp_plain']
        except Exception as e:
            out['decrypt_error'] = repr(e)
        return out

    def logout(self, acct: Account, token: str) -> dict:
        """Release the session on the server so we don't leave a live login."""
        ts = str(int(time.time() * 1000))
        nonce = str(uuid.uuid4())
        platform = str(getattr(acct, 'platform', 'android') or 'android').lower()
        headers = build_headers(acct, ts, nonce)
        if platform == 'ios':
            uid = int(acct.uid or -1)
            headers['uid'] = uid
            headers['studentId'] = uid
            headers['token'] = token or ''
            headers['tokenSign'] = token_sign(ts, headers['token'], uid)
        else:
            headers['uid'] = acct.uid or ''
            headers['token'] = token or ''
            headers['tokenSign'] = token_sign(ts, headers['token'],
                                              headers['uid'])
        body = self.encrypt_body(json.dumps({}, separators=(',', ':')))
        try:
            status, data = self._post(LOGOUT_URL, headers, body,
                                      platform=platform)
        except Exception as e:
            return {'ok': False, 'error': repr(e)}
        out = {'ok': True, 'status': status, 'resp_len': len(data)}
        try:
            text = data.decode('utf-8', 'replace').strip()
            if text.startswith('{'):
                out['resp_plain'] = self.decrypt_body(text)
        except Exception:
            pass
        return out

    def logout_and_verify(self, acct: Account) -> dict:
        """Logout, then prove the token no longer works (401 / business error).

        Never claim success just because the HTTP call returned 200.  The
        verification is the point: a server that ignores logout must not leave
        us thinking the session is gone.
        """
        token = acct.token or ''
        res = self.logout(acct, token)
        if not res.get('ok') or res.get('status') not in (200, 204):
            return {'ok': False, 'stage': 'logout', 'logout': res,
                    'verify': None}
        try:
            chk = request_user_info(self, acct, method='GET', retries=0)
        except Exception as e:  # noqa: BLE001
            return {'ok': False, 'stage': 'verify-error', 'logout': res,
                    'verify': {'error': repr(e)}}

        status = chk.get('status')
        # An HTTP 401/403 is an explicit rejection of this token and is enough
        # to prove logout.  A 5xx, a transport/decrypt error, or an empty body
        # proves nothing and must never be reported as `verified`.
        if status in (401, 403):
            return {'ok': True, 'stage': 'verified',
                    'logout': res, 'verify': chk}
        if status is None or (isinstance(status, int) and status >= 500):
            return {'ok': False, 'stage': 'verify-error',
                    'logout': res, 'verify': chk}
        if chk.get('error'):
            return {'ok': False, 'stage': 'verify-error',
                    'logout': res, 'verify': chk}

        obj = chk.get('json')
        if not isinstance(obj, dict) or not obj:
            return {'ok': False, 'stage': 'verify-error',
                    'logout': res, 'verify': chk}
        err = obj.get('error')
        # 10121 is the observed "logged in elsewhere" business rejection;
        # that is an invalid session too.  Other business errors (including
        # 500) are ambiguous, so they are not proof of logout.
        if err in (401, 403, 10121):
            return {'ok': True, 'stage': 'verified',
                    'logout': res, 'verify': chk}
        if err == 10000:
            return {'ok': False, 'stage': 'still-alive',
                    'logout': res, 'verify': chk}
        return {'ok': False, 'stage': 'verify-error',
                'logout': res, 'verify': chk}

# --------------------------------------------------------------------------
# self test
# --------------------------------------------------------------------------

def self_test() -> list[tuple[str, bool, str]]:
    out = []

    out.append(('as_dict unwraps string', as_dict('{"a":1}') == {'a': 1},
                'ok'))
    out.append(('as_dict None -> {}', as_dict(None) == {}, '{}'))
    out.append(('as_dict list -> {}', as_dict([1]) == {}, '{}'))
    out.append(('as_dict broken -> {}', as_dict('{x') == {}, '{}'))

    class _Fake(RealClient):
        def __init__(self, resp):
            RealClient.__init__(self, backend='netsec')
            self._resp = resp

        def request(self, *a, **k):
            return self._resp

    acct = Account(name='t', uid='1', token='x')
    ent = {'uuid': 'u-1', 'startTime': 1700000000000}

    ok_resp = {'status': 200, 'raw_len': 12, 'error': None,
               'json': {'error': 10000, 'data': '{"rrid":123,"uuid":"u"}'}}
    r = _Fake(ok_resp).submit_run(acct, {}, {}, ent)
    out.append(('string data unwrapped -> rrid', r['rrid'] == 123, r['rrid']))
    out.append(('verdict accepted', r['verdict'] == 'accepted', r['verdict']))
    out.append(('runes ends with uid', str(r['runes']).endswith('1'),
                r['runes']))
    out.append(('runef = uuid+start', r['runef'] == 'u-11700000000000',
                r['runef']))

    rej = {'status': 200, 'raw_len': 12, 'error': None,
           'json': {'error': 10121, 'message': 'busy'}}
    r2 = _Fake(rej).submit_run(acct, {}, {}, ent)
    out.append(('verdict rejected', r2['verdict'] == 'rejected',
                r2['verdict']))
    out.append(('no rrid on rejection', r2['rrid'] is None, r2['rrid']))

    dec = {'status': 200, 'raw_len': 0, 'error': 'decode failed', 'json': None}
    r3 = _Fake(dec).submit_run(acct, {}, {}, ent)
    out.append(('verdict decode-failed', r3['verdict'] == 'decode-failed',
                r3['verdict']))

    # ---- transport retries (idempotent calls only) ----------------------
    import urllib.error

    class _Flaky(RealClient):
        RETRY_BASE = 0.0

        def __init__(self, fails):
            RealClient.__init__(self, backend='netsec')
            self.fails = fails
            self.calls = 0

        def _request_once(self, *a, **k):
            self.calls += 1
            if self.calls <= self.fails:
                raise urllib.error.URLError('boom')
            return {'status': 200, 'raw_len': 2, 'plain': '{}',
                    'json': {'ok': 1}, 'error': None}

    flaky = _Flaky(2)
    ok = flaky.request(acct, 'x', retries=2)
    out.append(('retry recovers after 2 failures',
                ok.get('json') == {'ok': 1} and flaky.calls == 3,
                'calls=%d' % flaky.calls))

    strict = _Flaky(2)
    raised = False
    try:
        strict.request(acct, 'x', retries=0)
    except urllib.error.URLError:
        raised = True
    out.append(('retries=0 propagates', raised, 'raised'))
    out.append(('retries=0 makes one call', strict.calls == 1,
                'calls=%d' % strict.calls))

    hard = _Flaky(5)
    gave_up = False
    try:
        hard.request(acct, 'x', retries=1)
    except urllib.error.URLError:
        gave_up = True
    out.append(('gives up after retries', gave_up and hard.calls == 2,
                'calls=%d' % hard.calls))

    # ---- logout must be verified, not assumed ---------------------------
    class _LogoutFake(RealClient):
        def __init__(self, logout_res, verify_res):
            RealClient.__init__(self, backend='netsec')
            self._logout_res = logout_res
            self._verify_res = verify_res

        def logout(self, acct, token):
            return self._logout_res

        def request(self, *a, **k):
            return self._verify_res

    ok_logout = {'ok': True, 'status': 200}
    dead = _LogoutFake(ok_logout, {'status': 401, 'error': None,
                                   'plain': None}).logout_and_verify(acct)
    out.append(('logout verified by 401', dead['ok'] and
                dead['stage'] == 'verified', dead['stage']))

    alive = _LogoutFake(ok_logout, {'status': 200, 'error': None,
                                    'json': {'error': 10000}}).logout_and_verify(acct)
    out.append(('logout refuses still-alive token', not alive['ok'] and
                alive['stage'] == 'still-alive', alive['stage']))

    failed = _LogoutFake({'ok': False, 'error': 'x'}, {}).logout_and_verify(acct)
    out.append(('logout failure is not success', not failed['ok'] and
                failed['stage'] == 'logout', failed['stage']))

    decode_bad = _LogoutFake(ok_logout, {
        'status': 200, 'error': 'decode failed', 'json': None,
    }).logout_and_verify(acct)
    out.append(('logout decode error is not verified',
                not decode_bad['ok'] and decode_bad['stage'] == 'verify-error',
                decode_bad['stage']))

    server_err = _LogoutFake(ok_logout, {
        'status': 500, 'error': None, 'json': {'error': 500},
    }).logout_and_verify(acct)
    out.append(('logout HTTP 500 is not verified',
                not server_err['ok'] and server_err['stage'] == 'verify-error',
                server_err['stage']))

    ambiguous = _LogoutFake(ok_logout, {
        'status': 200, 'error': None, 'json': {'error': 500},
    }).logout_and_verify(acct)
    out.append(('logout business 500 is not verified',
                not ambiguous['ok'] and ambiguous['stage'] == 'verify-error',
                ambiguous['stage']))

    other_device = _LogoutFake(ok_logout, {
        'status': 200, 'error': None, 'json': {'error': 10121},
    }).logout_and_verify(acct)
    out.append(('logout 10121 is verified',
                other_device['ok'] and other_device['stage'] == 'verified',
                other_device['stage']))
    return out


if __name__ == '__main__':
    ok = True
    for name, passed, detail in self_test():
        print('%-34s %s   %s' % (name, 'PASS' if passed else 'FAIL', detail))
        ok = ok and passed
    print('\nRESULT:', 'PASS' if ok else 'FAIL')
    raise SystemExit(0 if ok else 1)

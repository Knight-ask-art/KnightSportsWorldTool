"""Clean-room crypto primitives for the replica, derived from binary evidence.

Evidence sources (00201.0.0 main.dll + Phase-0 analysis):
- 11-step hybrid upload encryption (AES-CBC + RSA PKCS1v15), payload fields
  {encrypted_data, encrypted_key, aes_iv}  -- see DIFF_REPORT.md
- runschool request signing: sort keys -> "k=v&k2=v2" -> MD5
- Runschool/iOS salts and the app host: owned by ``hxp-core`` in a release
  build (see ``protocol_constants``); a source checkout lazily reads them from
  the dev-only ``_dev_secrets`` module.
- Upload headers: calculateBadge/Appid/Client-Type/Geeid (G01-{ms}-{16hex})
- IMEI: 15-digit, Luhn-valid (binary evidence: generate_imei)
NOTE: RSA keypair below is freshly generated for the mock loop. The original
product's embedded private key is NOT reused.
"""
import base64
import gzip
import hashlib
import json
import os
import random

from Crypto.Cipher import AES
from Crypto.Cipher import PKCS1_v1_5 as PKCS1_v15
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad, unpad

IPHONE_UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) '
             'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 '
             'Mobile/15E148 Safari/604.1')
SWCAMPUS_UA = 'SWCampus/1 CFNetwork/1406.0.4 Darwin/22.4.0'

#: Raised when a frozen build has no native core.  The message deliberately
#: names neither a key nor a filesystem path.
_NATIVE_UNAVAILABLE = '安全组件未就绪，请重新安装软件后再试'


class NativeCryptoRequired(RuntimeError):
    """A frozen build cannot fall back to the readable Python protocol."""


def native_required() -> bool:
    """True inside a packaged build; monkeypatched by tests."""
    try:
        from ..build_info import is_compiled
        return bool(is_compiled())
    except Exception:  # noqa: BLE001 - build_info is optional at import time
        return False


def _rustcore():
    from ..transport import rustcore
    return rustcore


def _native_call(op: str, **args):
    """Call the sidecar or fail closed, without leaking internals."""
    rustcore = _rustcore()
    try:
        core = rustcore.get()
        if core is None:
            raise rustcore.RustCoreUnavailable('native core unavailable')
        return core.call(op, **args)
    except NativeCryptoRequired:
        raise
    except Exception:  # noqa: BLE001 - missing/crashed sidecar is one failure
        raise NativeCryptoRequired(_NATIVE_UNAVAILABLE) from None


def _source_core():
    """The sidecar when a source checkout has one built; never raises."""
    rustcore = _rustcore()
    try:
        return rustcore.get()
    except (rustcore.RustCoreUnavailable, rustcore.RustCoreError):
        return None


def _dev():
    """Lazy dev-only constants.  Frozen builds must not reach this."""
    from . import _dev_secrets
    return _dev_secrets


def dev_fallback(name: str):
    """A dev-only constant, or fail closed in a frozen build."""
    if native_required():
        raise NativeCryptoRequired(_NATIVE_UNAVAILABLE)
    return getattr(_dev(), name)


def protocol_constant(name: str) -> str:
    """Host/app id/runec key: native in a release, dev module in a checkout."""
    if name not in ('host', 'app_id', 'runec_key'):
        raise KeyError(name)
    if native_required():
        values = _native_call('protocol_constants')
        if not isinstance(values, dict) or name not in values:
            raise NativeCryptoRequired(_NATIVE_UNAVAILABLE)
        return str(values[name])
    return str(_dev().PROTOCOL_CONSTANTS[name])


def sign_with_suffix(ts: str, token: str, uid: str) -> str:
    """MD5('timeStamp=..&token=..&uid=..' + suffix) on the native side."""
    args = {'ts': str(ts), 'token': str(token), 'uid': str(uid)}
    if native_required():
        return _native_call('token_sign_ios', **args)
    rustcore = _rustcore()
    core = _source_core()
    if core is not None:
        try:
            return core.call('token_sign_ios', **args)
        except (rustcore.RustCoreError, rustcore.RustCoreUnavailable):
            pass
    link = f'timeStamp={args["ts"]}&token={args["token"]}&uid={args["uid"]}'
    return hashlib.md5((link + _dev().RUNSCHOOL_SECRET).encode()).hexdigest()


def runschool_sign(params: dict) -> str:
    """Sort keys -> 'k=v&...' -> MD5. Evidence: str_sign/get_sign."""
    args = {'params': {str(k): str(v) for k, v in params.items()}}
    if native_required():
        return _native_call('runschool_sign', **args)
    rustcore = _rustcore()
    core = _source_core()
    if core is not None:
        try:
            return core.call('runschool_sign', **args)
        except (rustcore.RustCoreError, rustcore.RustCoreUnavailable):
            pass
    query = '&'.join(f'{k}={params[k]}' for k in sorted(params))
    return hashlib.md5(query.encode()).hexdigest()


def points_sign(host: str, path: str) -> str:
    """Campus checkpoint sign: MD5('http://' + host + path + salt)."""
    if native_required():
        return _native_call('points_sign', host=str(host), path=str(path))
    rustcore = _rustcore()
    core = _source_core()
    if core is not None:
        try:
            return core.call('points_sign', host=str(host), path=str(path))
        except (rustcore.RustCoreError, rustcore.RustCoreUnavailable):
            pass
    return hashlib.md5(
        ('http://%s%s%s' % (host, path, _dev().RUNSCHOOL_SECRET)).encode()
    ).hexdigest()


# ---- official-app signing (Coaixy 7.2.70 decompiled source, verified) ----
# SignCode.getSign(Params) = MD5(linkString + salt), linkString = keys sorted
# CASE-INSENSITIVELY joined as k=v&k=v, null values and key 'sign' filtered
# out, ALL values stringified (String.valueOf semantics).
#
# RECOVERED STATIC SALT (7.3.70 resources.arsc, R.string.md5_sign_salt):
#   tokenSign = getSign({timeStamp,token,uid}, salt=<native TOKEN_SIGN_SALT>)


def token_sign(time_stamp: str, token: str, uid: str,
               salt: str | None = None) -> str:
    """HttpUtil/CommonParamInterceptor tokenSign (salt: recovered static)."""
    args = {'ts': str(time_stamp), 'token': str(token), 'uid': str(uid)}
    if salt is not None:
        args['salt'] = str(salt)
    if native_required():
        return _native_call('token_sign', **args)
    if salt is None:
        rustcore = _rustcore()
        core = _source_core()
        if core is not None:
            try:
                return core.call('token_sign', **args)
            except (rustcore.RustCoreError, rustcore.RustCoreUnavailable):
                pass
        salt = _dev().TOKEN_SIGN_SALT
    return _official_sign_py({'timeStamp': time_stamp, 'token': token,
                              'uid': uid}, salt)


def official_link_string(params: dict) -> str:
    items = [(k, v) for k, v in params.items()
             if v is not None and k.lower() != 'sign']
    items.sort(key=lambda kv: kv[0].lower())
    return '&'.join(f'{k}={v}' for k, v in items)


def official_link_string_natural(params: dict) -> str:
    """SignCode's 1-arg createLinkString uses Collections.sort() -> plain
    String natural order (CASE-SENSITIVE), unlike the 2-arg overload which
    uses compareToIgnoreCase. getPreSign() goes through the 1-arg one."""
    items = [(k, v) for k, v in params.items()
             if v is not None and k.lower() != 'sign']
    items.sort(key=lambda kv: kv[0])
    return '&'.join(f'{k}={v}' for k, v in items)


def official_presign(params: dict) -> str:
    """SignCode.getPreSign == UploadFormatEntity.originalSign."""
    return official_link_string_natural(params)


def _official_sign_py(params: dict, salt: str) -> str:
    return hashlib.md5((official_link_string(params) + salt).encode('utf-8')
                       ).hexdigest()


def official_sign(params: dict, salt: str | None = None) -> str:
    """SignCode.getSign == UploadFormatEntity.signature.

    ``salt=None`` means the recovered default; a frozen build asks the native
    core for it, a source checkout reads the dev-only module.
    """
    if native_required():
        args = {'params': params}
        if salt is not None:
            args['salt'] = str(salt)
        return _native_call('official_sign', **args)
    if salt is None:
        salt = _dev().SIGN_SALT_STANDIN
    return _official_sign_py(params, salt)


# UploadSignEntity 31 signed fields (decompiled field order; sort makes order
# irrelevant, kept for documentation).
UPLOAD_SIGN_FIELDS = [
    'address', 'avgPower', 'avgStepFreq', 'calorie', 'complete', 'errorCode',
    'faceCheck', 'geeToken', 'getPrize', 'goalId', 'policy', 'selDistance',
    'selRunTime', 'selectedUnid', 'speed', 'sportType', 'startTime', 'status',
    'stopTime', 'themeId', 'totalAscent', 'totalDis', 'totalSteps',
    'totalTime', 'uid', 'unCompleteReason', 'unauthorized', 'useMobilityTools',
    'uuid', 'validDis', 'validTime',
]


def jbool(v) -> str:
    """Java String.valueOf(boolean)."""
    return 'true' if v else 'false'


def make_geeid() -> str:
    from . import clock
    ms = clock.now_ms()
    return f'G01-{ms}-{os.urandom(8).hex()}'


def upload_headers() -> dict:
    return {
        'calculateBadge': 'true',
        'Appid': protocol_constant('app_id'),
        'Client-Type': '4',
        'Geeid': make_geeid(),
        'User-Agent': IPHONE_UA,
    }


def hybrid_encrypt(obj: dict, rsa_public_pem: bytes) -> dict:
    """11-step upload encryption. Returns JSON-serialisable payload."""
    json_data = json.dumps(obj, separators=(',', ':')).encode()
    compressed = gzip.compress(json_data)
    b64 = base64.b64encode(compressed)
    aes_key = os.urandom(16)
    aes_iv = os.urandom(16)
    cipher = AES.new(aes_key, AES.MODE_CBC, aes_iv)
    encrypted_data = cipher.encrypt(pad(b64, AES.block_size))
    pub = RSA.import_key(rsa_public_pem)
    encrypted_key = PKCS1_v15.new(pub).encrypt(aes_key)
    return {
        'encrypted_data': base64.b64encode(encrypted_data).decode(),
        'encrypted_key': base64.b64encode(encrypted_key).decode(),
        'aes_iv': base64.b64encode(aes_iv).decode(),
    }


def hybrid_decrypt(payload: dict, rsa_private_pem: bytes) -> dict:
    """Inverse of hybrid_encrypt (mock-server side)."""
    priv = RSA.import_key(rsa_private_pem)
    aes_key = PKCS1_v15.new(priv).decrypt(
        base64.b64decode(payload['encrypted_key']), sentinel=None)
    if aes_key is None or len(aes_key) != 16:
        raise ValueError('RSA unwrap failed')
    aes_iv = base64.b64decode(payload['aes_iv'])
    encrypted_data = base64.b64decode(payload['encrypted_data'])
    plain = unpad(AES.new(aes_key, AES.MODE_CBC, aes_iv).decrypt(encrypted_data),
                  AES.block_size)
    return json.loads(gzip.decompress(base64.b64decode(plain)).decode())


def generate_imei(rng: random.Random | None = None) -> str:
    """15-digit Luhn-valid IMEI."""
    rng = rng or random
    digits = [rng.randint(0, 9) for _ in range(14)]
    total = 0
    for i, d in enumerate(digits):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    digits.append((10 - total % 10) % 10)
    return ''.join(map(str, digits))


def luhn_valid(imei: str) -> bool:
    if len(imei) != 15 or not imei.isdigit():
        return False
    digits = list(map(int, imei))
    total = 0
    for i, d in enumerate(digits):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def ensure_keypair(path: str) -> tuple[bytes, bytes]:
    """Load or freshly generate a 2048-bit RSA pair for the mock loop."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    priv_p = path + '.priv.pem'
    pub_p = path + '.pub.pem'
    if os.path.exists(priv_p) and os.path.exists(pub_p):
        with open(priv_p, 'rb') as f:
            priv = f.read()
        with open(pub_p, 'rb') as f:
            pub = f.read()
        return priv, pub
    key = RSA.generate(2048)
    priv = key.export_key()
    pub = key.publickey().export_key()
    with open(priv_p, 'wb') as f:
        f.write(priv)
    with open(pub_p, 'wb') as f:
        f.write(pub)
    return priv, pub


# ---- source compatibility -------------------------------------------------
# Older callers import these names directly.  They are resolved lazily so the
# plaintext never lives in a module that ships; frozen builds either get the
# value from the native core (host/app id/runec key) or fail closed (salts).
_NATIVE_ATTRS = {
    'APP_ID': 'app_id',
    'RUNEC_KEY': 'runec_key',
}
_DEV_ATTRS = {
    'RUNSCHOOL_SECRET': 'RUNSCHOOL_SECRET',
    'TOKEN_SIGN_SALT': 'TOKEN_SIGN_SALT',
    'SIGN_SALT_STANDIN': 'SIGN_SALT_STANDIN',
}


def __getattr__(name: str):
    if name in _NATIVE_ATTRS:
        return protocol_constant(_NATIVE_ATTRS[name])
    if name == 'UPLOAD_URL':
        return 'https://%s/api/v70260/runnings/save/record' % (
            protocol_constant('host'))
    if name in _DEV_ATTRS:
        return dev_fallback(_DEV_ATTRS[name])
    raise AttributeError('module %r has no attribute %r' % (__name__, name))

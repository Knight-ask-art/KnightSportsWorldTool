"""Clean-room crypto primitives for the replica, derived from binary evidence.

Evidence sources (00201.0.0 main.dll + Phase-0 analysis):
- 11-step hybrid upload encryption (AES-CBC + RSA PKCS1v15), payload fields
  {encrypted_data, encrypted_key, aes_iv}  -- see DIFF_REPORT.md
- runschool request signing: sort keys -> "k=v&k2=v2" -> MD5
- runschool secret: '2slhe02lsfiwowlcixisla_sls-_slaor' (chr-list obfuscated in old
  build; byte-identical algorithm retained in new runschool_request_List)
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
import time

from Crypto.Cipher import AES
from Crypto.Cipher import PKCS1_v1_5 as PKCS1_v15
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad, unpad

# ---- binary-evidence constants -------------------------------------------
RUNSCHOOL_SECRET = '2slhe02lsfiwowlcixisla_sls-_slaor'
RUNEC_KEY = 'd8dba5641eace5ea41cc502ae30d3bb2'
APP_ID = 'prj7sr2xk2bntjccmzbjckps7m9ogmlz'
UPLOAD_URL = 'https://run.gxapp.iydsj.com/api/v70260/runnings/save/record'
IPHONE_UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) '
             'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 '
             'Mobile/15E148 Safari/604.1')
SWCAMPUS_UA = 'SWCampus/1 CFNetwork/1406.0.4 Darwin/22.4.0'


def runschool_sign(params: dict) -> str:
    """Sort keys -> 'k=v&...' -> MD5. Evidence: str_sign/get_sign."""
    query = '&'.join(f'{k}={params[k]}' for k in sorted(params))
    return hashlib.md5(query.encode()).hexdigest()


# ---- official-app signing (Coaixy 7.2.70 decompiled source, verified) ----
# SignCode.getSign(Params) = MD5(linkString + salt), linkString = keys sorted
# CASE-INSENSITIVELY joined as k=v&k=v, null values and key 'sign' filtered
# out, ALL values stringified (String.valueOf semantics).
#
# RECOVERED STATIC SALT (7.3.70 resources.arsc, R.string.md5_sign_salt):
#   tokenSign = getSign({timeStamp,token,uid}, salt=TOKEN_SIGN_SALT)
TOKEN_SIGN_SALT = '&wh2016_swcampus'
# Submit-signature salt = native SportJniUtils.getSecretKey(5); NOT
# recoverable statically -> tool-equivalent stand-in (UNVERIFIED vs server).
SIGN_SALT_STANDIN = RUNSCHOOL_SECRET


def token_sign(time_stamp: str, token: str, uid: str,
               salt: str = TOKEN_SIGN_SALT) -> str:
    """HttpUtil/CommonParamInterceptor tokenSign (salt: recovered static)."""
    return official_sign({'timeStamp': time_stamp, 'token': token,
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


def official_sign(params: dict, salt: str = SIGN_SALT_STANDIN) -> str:
    """SignCode.getSign == UploadFormatEntity.signature."""
    return hashlib.md5((official_link_string(params) + salt).encode('utf-8')
                       ).hexdigest()


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
    ms = int(time.time() * 1000)
    return f'G01-{ms}-{os.urandom(8).hex()}'


def upload_headers() -> dict:
    return {
        'calculateBadge': 'true',
        'Appid': APP_ID,
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

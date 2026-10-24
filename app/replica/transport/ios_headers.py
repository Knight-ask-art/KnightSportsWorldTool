"""iOS request headers, matching the reference client's iOS builder.

The Android header (23 fields, Java HashMap order) is the proven path.  The
iOS app sends a *different* set: no IMEI/androidId/wifiMac/blMac, but an
optional IDFA and a studentId; most values are typed (uid is a number,
isRoot a boolean) rather than strings.  ``build_ios_headers`` follows the
field order in NekoSportsWorldTool ``src/crypto/header.rs``.

iOS submit (``save/record``) is our own extension.  The reference client
forces an Android header there, so treat iOS submission as unverified until a
full login + submit + readback + OBS run has actually passed.
"""

from __future__ import annotations

import hashlib
import json

#: The iOS app version observed in the reference client's iOS header.
IOS_APP_VERSION = '7.3.40'
TOKEN_SIGN_SUFFIX = '2slhe02lsfiwowlcixisla_sls-_slaor'


def token_sign(ts: str, token: str, uid) -> str:
    link = f'timeStamp={ts}&token={token}&uid={uid}'
    return hashlib.md5((link + TOKEN_SIGN_SUFFIX).encode()).hexdigest()


def _uid_int(uid) -> int:
    try:
        return int(str(uid))
    except (TypeError, ValueError):
        return -1


def build_ios_headers(acct, ts: str, nonce: str, idfa: str = '') -> dict:
    """Build the iOS header plaintext for one request.

    ``acct`` is a ``realclient.Account``.  Values that belong to the device
    (model, OS, pixels, CPU) come from the account/profile, not from constants:
    a hard-coded 1080x1920 / x86_64 pair is the reference simulator, not a
    real iPhone.
    """
    uid = _uid_int(acct.uid)
    token = acct.token or ''
    device_id = acct.DeviceId or ''
    idfa = idfa or getattr(acct, 'idfa', '') or ''
    headers = {
        'osType': '1',
        'DeviceId': device_id,
        'deviceName': acct.deviceName or 'iPhone',
        'CustomDeviceId': f'{device_id}_iOS_sportsWorld_campus',
        'osVersion': acct.osVersion or '18.1',
    }
    if idfa:
        headers['IDFA'] = idfa
    headers.update({
        'logicPixel': acct.logicPixel,
        'physicPixel': acct.physicPixel,
        'cpuModel': acct.cpuModel or 'arm64e',
        'appVersion': IOS_APP_VERSION,
        'isRoot': False,
        'appInstallTime': int(acct.appInstallTime or 0),
        'nonce': nonce,
    })
    if uid >= 1:
        headers['uid'] = uid
    if token:
        headers['token'] = token
    headers['timeStamp'] = ts
    headers['studentId'] = uid if uid >= 1 else 0
    if uid >= 1 and token:
        headers['tokenSign'] = token_sign(ts, token, uid)
    return headers


def ios_header_json(headers: dict) -> str:
    """Serialize iOS headers the way the native builder does.

    Insertion order, no whitespace, and native JSON types: ``uid`` and
    ``studentId`` are numbers, ``isRoot`` is a boolean.  The Android path uses
    Java HashMap bucket order; sharing that serializer here would turn every
    typed value into a string and break the iOS signature.
    """
    return json.dumps(headers, separators=(',', ':'), ensure_ascii=False)

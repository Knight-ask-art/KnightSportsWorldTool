"""iOS request headers, matching the reference client's iOS builder.

The Android header (23 fields, Java HashMap order) is the proven path.  The
iOS app sends a *different* set: no IMEI/androidId/wifiMac/blMac, but an
optional IDFA and a studentId; most values are typed (uid is a number,
isRoot a boolean) rather than strings.  ``build_ios_headers`` follows the
field order in NekoSportsWorldTool ``src/crypto/header.rs``.

iOS submit (``save/record``) is our own extension.  The reference client
forces an Android header there and its audit calls that a hard contradiction
(same token, iOS at login, Android at submit), so we keep the whole chain on
one platform instead: login, policy, points, submit and OBS all use the same
iOS identity.  That chain has been verified on a real device (rrid
``1344757460``, 2026-10-25: ``status=0`` / ``isCheat=0`` / ``policy=0``).
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid

#: The current iOS App Store build (version history: 7.4.0, 2026-10-26).
#: The previous value was 7.3.40, which is what the 2025 reference client
#: captured; keeping it after the 7.4.0 client shipped makes the signed
#: ``appVersion`` and UA older than the data shape we send.
IOS_APP_VERSION = '7.4.0'
TOKEN_SIGN_SUFFIX = '2slhe02lsfiwowlcixisla_sls-_slaor'

#: Fallback install age when the profile has no appInstallTime (the reference
#: builder uses 3 days on iOS and 90 days on Android).  Sending 0 would be the
#: one value no real installation can report.
IOS_INSTALL_FALLBACK_DAYS = 3

#: Minted once at import, not lazily: two bridge threads handling the first
#: requests at the same time could otherwise each mint one and disagree.
_FALLBACK_DEVICE_ID = str(uuid.uuid4()).upper()


def token_sign(ts: str, token: str, uid) -> str:
    try:
        from ..transport import rustcore
        core = rustcore.get()
        if core is not None:
            return core.call('token_sign_ios', ts=str(ts), token=str(token),
                             uid=str(uid))
    except rustcore.RustCoreUnavailable:
        pass
    link = f'timeStamp={ts}&token={token}&uid={uid}'
    return hashlib.md5((link + TOKEN_SIGN_SUFFIX).encode()).hexdigest()


def _uid_int(uid) -> int:
    try:
        return int(str(uid))
    except (TypeError, ValueError):
        return -1


def _stable_device_id(value) -> str:
    """DeviceId, never empty.

    An empty id would also make ``CustomDeviceId`` just
    ``_iOS_sportsWorld_campus``.  The fallback is minted once per process so it
    does not drift between requests (a per-request UUID is itself a signal).
    """
    text = str(value or '').strip()
    return text or _FALLBACK_DEVICE_ID


def _install_ms(acct, ts: str) -> int:
    """appInstallTime as a number; missing/0 falls back like the reference."""
    try:
        value = int(getattr(acct, 'appInstallTime', 0) or 0)
    except (TypeError, ValueError):
        value = 0
    if value > 0:
        return value
    try:
        now = int(ts)
    except (TypeError, ValueError):
        now = int(time.time() * 1000)
    return now - IOS_INSTALL_FALLBACK_DAYS * 86_400_000


def build_ios_headers(acct, ts: str, nonce: str, idfa: str = '') -> dict:
    """Build the iOS header plaintext for one request.

    ``acct`` is a ``realclient.Account``.  Values that belong to the device
    (model, OS, pixels, CPU) come from the account/profile, not from constants:
    a hard-coded 1080x1920 / x86_64 pair is the reference simulator, not a
    real iPhone.
    """
    uid = _uid_int(acct.uid)
    token = acct.token or ''
    device_id = _stable_device_id(getattr(acct, 'DeviceId', ''))
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
        'appInstallTime': _install_ms(acct, ts),
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

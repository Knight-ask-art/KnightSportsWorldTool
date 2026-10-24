"""One stable device identity per installation.

Why this module exists
----------------------
The service clusters accounts by the request-header fingerprint.  A single
compiled-in ``DeviceId`` makes every copy of the tool look like the same phone;
generating a fresh id on every launch is worse because the account appears to
change phone every day.  The only stable shape is:

* create the profile once per data directory;
* keep it forever (login, submit, OBS readback all use the same one);
* never rotate silently.  An explicit ``rotate()`` backs up the old profile.

Existing data directories are not rotated on upgrade.  If a session predates
this module, the profile freezes the fingerprint that was already used for
submission.  ``rotate()`` is the one-time, operator-approved exception.

New installations draw from a coherent Android/iOS pool (``JHP_IOS_CHANCE``,
default 30).  Once written, the profile is stable regardless of platform: the
same DeviceId and device fields are used for login, submission and readback.

Reference points (checked 2026-10-24)
-------------------------------------
* NekoSportsWorldTool ``src/crypto/header.rs``: appInstallTime is read from a
  persisted identity, not regenerated per request.
* NekoSportsWorldTool ``src/ui/device.rs``: randomise fills a buffer; the
  operator must press Save, and the UI warns not to change devices often.
* FunSportWorld ``funsport/config.py``: a missing identity.json gets one random
  UUID + install time + MAC, then is written atomically and reused.
* react-native-device-info ``getInstanceId``: a random UUID stored in
  SharedPreferences, stable for the app installation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import tempfile
import time
import uuid

from .guard import _file_lock

PROFILE_FILENAME = 'device_profile.json'
PROFILE_VERSION = 1
LOCK_FILENAME = '.device_profile.lock'

#: The fingerprint the current build already submits with.  Keep it byte for
#: byte when freezing an existing installation; changing any of these fields
#: would make an upgrade look like a new phone.
LEGACY_DEVICE = {
    'platform': 'android',
    'DeviceId': 'afa505ae1e60377972564c220fb24311',
    'deviceName': 'Xiaomi 2304FPN6DG',
    'osVersion': '9',
    'cpuModel': 'x86_64',
    'physicPixel': '900x1600',
    'logicPixel': '600x1066',
    'isRoot': '1',
    'appInstallTime': '1785436971435',
    'appUpdateTime': '1788581965711',
}

#: Real Android models with the OS versions that shipped on them.  Keeping
#: model, OS and both resolutions in one row avoids impossible pairs such as
#: Pixel 8 + Android 12, or a 1440p panel with a 1080p physical resolution.
#:
#: ``physicPixel`` is the panel resolution; ``logicPixel`` is the dp size the
#: app reads from DisplayMetrics (not the same number!).  Older builds used
#: 1080x2400 for both, which is an easy synthetic signal.
ANDROID_MODELS = (
    # Redmi K50 Ultra: 6.67" 1220x2712, MIUI 13 -> HyperOS
    ('22081212C', ('13', '14'), '1220x2712', '444x986'),
    # Xiaomi 13: 6.36" 1080x2400, Android 13 -> 15
    ('2211133C', ('13', '14', '15'), '1080x2400', '393x873'),
    # Xiaomi 14: 6.36" 1200x2670, Android 14 -> 15
    ('23127PN0CC', ('14', '15'), '1200x2670', '400x890'),
    # OnePlus Ace 3: 6.78" 1240x2772, Android 14 -> 15
    ('PJE110', ('14', '15'), '1240x2772', '441x986'),
    # Galaxy S24 (China): 6.2" 1080x2340, Android 14 -> 15
    ('SM-S9210', ('14', '15'), '1080x2340', '384x832'),
    # Pixel 8: 6.2" 1080x2400, Android 14 -> 15
    ('Pixel 8', ('14', '15'), '1080x2400', '411x914'),
    # vivo X100 Pro: 6.78" 1260x2800, Android 14 -> 15
    ('V2309A', ('14', '15'), '1260x2800', '420x933'),
    # Redmi K60: 6.67" 1220x2712, Android 13 -> 14
    ('23013RK75C', ('13', '14'), '1220x2712', '444x986'),
)

#: iOS profiles used when the random pool picks Apple.  Model, OS, physical
#: pixels and logical pixels stay in one row so the fingerprint cannot pair an
#: iPhone 15 with an iPhone 8 resolution.  ``cpuModel`` uses arm64e rather than
#: the reference simulator's x86_64: an iPhone running iOS 18 with x86_64 is an
#: obvious emulator signal.  The submit path for iOS is still unverified;
#: ``JHP_IOS_CHANCE`` keeps the exposure controllable (default 30%).
IOS_MODELS = (
    ('iPhone 13', ('16.7.10', '17.7.2', '18.1'), '1170x2532', '390x844'),
    ('iPhone 14', ('16.7.10', '17.7.2', '18.1'), '1170x2532', '390x844'),
    ('iPhone 15', ('17.5.1', '18.0.1', '18.1', '18.2', '26.5.2'),
     '1179x2556', '393x852'),
    ('iPhone 15 Pro', ('17.5.1', '18.0.1', '18.1', '18.2', '26.5.2'),
     '1179x2556', '393x852'),
    ('iPhone 15 Pro Max', ('17.5.1', '18.0.1', '18.1', '18.2', '26.5.2'),
     '1290x2796', '430x932'),
    ('iPhone 16', ('18.0.1', '18.1', '18.2', '26.5.2'),
     '1179x2556', '393x852'),
    ('iPhone 16 Pro', ('18.0.1', '18.1', '18.2', '26.5.2'),
     '1206x2622', '402x874'),
    ('iPhone 16 Pro Max', ('18.0.1', '18.1', '18.2', '26.5.2'),
     '1320x2868', '440x956'),
)
IOS_CPU_MODEL = 'arm64e'
IOS_CHANCE_DEFAULT = 30
#: Most real iPhones deny App Tracking Transparency, so IDFA is all zeros.
_IDFA_ZERO = '00000000-0000-0000-0000-000000000000'

_DEVICE_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$')
_IDFA_RE = re.compile(r'^[A-Za-z0-9-]{8,64}$')
_PIXEL_RE = re.compile(r'^\d{3,5}x\d{3,5}$')
_DIGITS_RE = re.compile(r'^\d{10,16}$')

_SOURCES = frozenset({
    'generated', 'legacy-default', 'legacy-session', 'rotated', 'session',
})


class DeviceProfileError(RuntimeError):
    """The profile exists but cannot be used; do not silently replace it."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _profile_path(store_dir: str) -> str:
    return os.path.join(os.path.abspath(store_dir), PROFILE_FILENAME)


def _lock_path(store_dir: str) -> str:
    return os.path.join(os.path.abspath(store_dir), LOCK_FILENAME)


def _session_path(store_dir: str) -> str:
    return os.path.join(os.path.abspath(store_dir), 'accounts_emu.json')


def default_store_dir() -> str:
    """The CLI's data root, matching http_server._default_store_dir()."""
    env = os.environ.get('JHP_DATA_DIR')
    if env:
        return os.path.abspath(os.path.expanduser(env))
    import sys
    if getattr(sys, 'frozen', False):
        return os.path.join(os.path.dirname(sys.executable), 'data')
    # app/replica/application/device_profile.py -> app
    app_dir = os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(app_dir, 'data')


def read_session(store_dir: str) -> dict:
    try:
        data = _read_json(_session_path(store_dir), {})
    except DeviceProfileError:
        # A corrupt session must not block profile generation; the login gate
        # already has its own error handling.
        return {}
    return data if isinstance(data, dict) else {}


def _read_json(path: str, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        raise DeviceProfileError(
            f'读取 {os.path.basename(path)} 失败: {e}')


def _write_json_atomic(path: str, data: dict) -> None:
    folder = os.path.dirname(os.path.abspath(path)) or '.'
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.device-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _as_digits(value, label: str) -> str:
    text = str(value if value is not None else '').strip()
    if not _DIGITS_RE.match(text):
        raise DeviceProfileError(f'{label} 不是毫秒时间戳')
    return text


def validate_profile(profile) -> dict:
    """Return a normalised copy, or raise if the file is not usable.

    Failing closed matters: silently generating a new profile on a corrupt
    file would rotate the device without the operator seeing it.
    """
    if not isinstance(profile, dict):
        raise DeviceProfileError('设备指纹不是 JSON 对象')
    version = profile.get('version', PROFILE_VERSION)
    try:
        version = int(version)
    except (TypeError, ValueError):
        raise DeviceProfileError('设备指纹版本无效')
    if version > PROFILE_VERSION:
        raise DeviceProfileError(
            f'设备指纹版本 {version} 高于当前程序支持的 {PROFILE_VERSION}')

    platform = str(profile.get('platform') or 'android').strip().lower()
    if platform not in ('android', 'ios'):
        raise DeviceProfileError('设备指纹平台必须是 android 或 ios')

    device_id = str(profile.get('DeviceId') or '').strip()
    if not _DEVICE_ID_RE.match(device_id):
        raise DeviceProfileError('DeviceId 格式无效')

    device_name = str(profile.get('deviceName') or '').strip()
    if not device_name or len(device_name) > 64:
        raise DeviceProfileError('deviceName 无效')

    os_version = str(profile.get('osVersion') or '').strip()
    if not os_version or len(os_version) > 16:
        raise DeviceProfileError('osVersion 无效')

    cpu_model = str(profile.get('cpuModel') or '').strip()
    if not cpu_model or len(cpu_model) > 32:
        raise DeviceProfileError('cpuModel 无效')

    physic = str(profile.get('physicPixel') or '').strip()
    logic = str(profile.get('logicPixel') or '').strip()
    if not _PIXEL_RE.match(physic) or not _PIXEL_RE.match(logic):
        raise DeviceProfileError('分辨率格式无效')

    is_root = str(profile.get('isRoot') if profile.get('isRoot') is not None
                  else '0').strip()
    if is_root not in ('0', '1'):
        raise DeviceProfileError('isRoot 必须是 0 或 1')

    idfa = str(profile.get('idfa') or '').strip()
    if idfa and not _IDFA_RE.match(idfa):
        raise DeviceProfileError('idfa 格式无效')

    install = _as_digits(profile.get('appInstallTime'), 'appInstallTime')
    update = _as_digits(profile.get('appUpdateTime'), 'appUpdateTime')
    if int(install) > int(update):
        raise DeviceProfileError('appInstallTime 晚于 appUpdateTime')

    source = str(profile.get('source') or 'unknown').strip()
    if source not in _SOURCES:
        source = 'unknown'

    try:
        created_at = int(profile.get('created_at') or _now_ms())
    except (TypeError, ValueError):
        raise DeviceProfileError('created_at 无效')

    out = {
        'version': PROFILE_VERSION,
        'platform': platform,
        'source': source,
        'created_at': created_at,
        'DeviceId': device_id,
        'deviceName': device_name,
        'osVersion': os_version,
        'cpuModel': cpu_model,
        'physicPixel': physic,
        'logicPixel': logic,
        'isRoot': is_root,
        'appInstallTime': install,
        'appUpdateTime': update,
    }
    if idfa:
        out['idfa'] = idfa
    if profile.get('rotated_at'):
        try:
            out['rotated_at'] = int(profile['rotated_at'])
        except (TypeError, ValueError):
            pass
    if profile.get('rotated_from'):
        out['rotated_from'] = str(profile['rotated_from'])[:32]
    return out


def _ios_chance() -> int:
    """iOS share for new random profiles (0-100, default 30).

    iOS submit is not yet verified end-to-end; the default keeps the exposure
    bounded.  Set ``JHP_IOS_CHANCE=50`` for an even mix, ``0`` to disable, or
    ``100`` to force iOS on new installs.
    """
    raw = os.environ.get('JHP_IOS_CHANCE')
    if raw is None or str(raw).strip() == '':
        return IOS_CHANCE_DEFAULT
    try:
        return max(0, min(100, int(str(raw).strip())))
    except (TypeError, ValueError):
        return IOS_CHANCE_DEFAULT


def generate_android_profile(*, now_ms: int | None = None,
                             source: str = 'generated') -> dict:
    """Create one coherent Android profile.  Never called per request."""
    now = int(now_ms if now_ms is not None else _now_ms())
    model, os_choices, physic, logic = secrets.choice(ANDROID_MODELS)
    os_version = secrets.choice(os_choices)
    # A real installation is older than the first run.  30-400 days keeps
    # install history plausible without every copy sharing one offset.  The
    # app is current (7.3.11), so appUpdateTime is recent and later than the
    # install; an install from last year with update == install would imply
    # the app was never updated since 7.3.11 shipped.
    install = now - (30 + secrets.randbelow(371)) * 86_400_000
    update = now - (1 + secrets.randbelow(21)) * 86_400_000
    if update <= install:
        update = install + 86_400_000
        if update > now:
            update = now
            install = now - 86_400_000
    return {
        'version': PROFILE_VERSION,
        'platform': 'android',
        'source': source if source in _SOURCES else 'generated',
        'created_at': now,
        'DeviceId': secrets.token_hex(16),
        'deviceName': model,
        'osVersion': os_version,
        'cpuModel': 'arm64-v8a',
        'physicPixel': physic,
        'logicPixel': logic,
        'isRoot': '0',
        'appInstallTime': str(install),
        'appUpdateTime': str(update),
    }


def generate_ios_profile(*, now_ms: int | None = None,
                         source: str = 'generated') -> dict:
    """Create one coherent iOS profile.  Never called per request."""
    now = int(now_ms if now_ms is not None else _now_ms())
    model, os_choices, physic, logic = secrets.choice(IOS_MODELS)
    os_version = secrets.choice(os_choices)
    # Fresh installs are common on iOS; keep install/update equal because the
    # reference iOS header has no appUpdateTime and a fresh install has no
    # update history yet.
    install = now - (2 + secrets.randbelow(60)) * 86_400_000
    idfa = (_IDFA_ZERO if secrets.randbelow(100) < 80
            else str(uuid.uuid4()).upper())
    return {
        'version': PROFILE_VERSION,
        'platform': 'ios',
        'source': source if source in _SOURCES else 'generated',
        'created_at': now,
        'DeviceId': str(uuid.uuid4()).upper(),
        'deviceName': model,
        'osVersion': os_version,
        'cpuModel': IOS_CPU_MODEL,
        'physicPixel': physic,
        'logicPixel': logic,
        'isRoot': '0',
        'idfa': idfa,
        'appInstallTime': str(install),
        'appUpdateTime': str(install),
    }


def generate_profile(*, now_ms: int | None = None,
                     source: str = 'generated',
                     platform: str | None = None) -> dict:
    """Create a random Android or iOS profile.

    ``platform`` forces one platform (used by rotate / tests).  When omitted,
    ``JHP_IOS_CHANCE`` controls the iOS share (default 30).
    """
    if platform is None:
        platform = ('ios' if secrets.randbelow(100) < _ios_chance()
                    else 'android')
    platform = str(platform).strip().lower()
    if platform == 'ios':
        return generate_ios_profile(now_ms=now_ms, source=source)
    if platform == 'android':
        return generate_android_profile(now_ms=now_ms, source=source)
    raise ValueError("platform must be 'android' or 'ios'")


def profile_from_session(session: dict | None) -> dict | None:
    """Freeze the identity an older build was already submitting with.

    A session without ``DeviceId`` used the compiled-in defaults.  A session
    with one (manual import / CLI) wins for that field.
    """
    if not isinstance(session, dict):
        return None
    if not (session.get('token') or session.get('uid')):
        return None
    platform = str(session.get('platform') or 'android').strip().lower()
    if platform == 'ios':
        profile = generate_ios_profile(source='legacy-session')
        profile['created_at'] = _now_ms()
    else:
        profile = dict(LEGACY_DEVICE)
        profile['version'] = PROFILE_VERSION
        profile['source'] = 'legacy-default'
        profile['created_at'] = _now_ms()
    if session.get('DeviceId'):
        profile['DeviceId'] = str(session['DeviceId'])
        profile['source'] = 'legacy-session'
    for src, dst in (('platform', 'platform'),
                     ('deviceName', 'deviceName'),
                     ('osVersion', 'osVersion'),
                     ('cpuModel', 'cpuModel'),
                     ('physicPixel', 'physicPixel'),
                     ('logicPixel', 'logicPixel'),
                     ('isRoot', 'isRoot'),
                     ('idfa', 'idfa'),
                     ('appInstallTime', 'appInstallTime'),
                     ('appUpdateTime', 'appUpdateTime')):
        if session.get(src) not in (None, ''):
            profile[dst] = str(session[src])
    return validate_profile(profile)


def read_profile(store_dir: str) -> dict | None:
    path = _profile_path(store_dir)
    if not os.path.exists(path):
        return None
    return validate_profile(_read_json(path, None))


def save_profile(store_dir: str, profile: dict) -> dict:
    profile = validate_profile(profile)
    _write_json_atomic(_profile_path(store_dir), profile)
    return profile


def load_or_create(store_dir: str, legacy_session: dict | None = None) -> dict:
    """Read the profile, or create the safe default for this data directory.

    * profile exists -> use it (never regenerate);
    * no profile, legacy session -> freeze the legacy fingerprint;
    * no profile, no session -> generate a random profile.
    """
    store_dir = os.path.abspath(store_dir)
    os.makedirs(store_dir, exist_ok=True)
    current = read_profile(store_dir)
    if current is not None:
        return current

    with _file_lock(_lock_path(store_dir)):
        current = read_profile(store_dir)
        if current is not None:
            return current
        legacy = profile_from_session(legacy_session)
        profile = legacy if legacy is not None else generate_profile()
        return save_profile(store_dir, profile)


def _backup_path(store_dir: str, now: int) -> str:
    stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(now / 1000))
    base = os.path.join(os.path.abspath(store_dir),
                        'device_profile.backup-' + stamp)
    candidate = base + '.json'
    n = 1
    while os.path.exists(candidate):
        candidate = f'{base}-{n}.json'
        n += 1
    return candidate


def rotate(store_dir: str, legacy_session: dict | None = None,
           *, force: bool = False,
           platform: str | None = None) -> tuple[dict, str | None]:
    """Explicitly replace this installation's fingerprint.

    Returns ``(new_profile, backup_path)``.  The old profile (or the legacy
    fingerprint derived from the session) is written to a timestamped backup
    before the new file is installed.  Refuses to rotate when a profile exists
    unless ``force`` is true, so a normal call cannot accidentally change an
    account that already has history.
    """
    store_dir = os.path.abspath(store_dir)
    os.makedirs(store_dir, exist_ok=True)
    with _file_lock(_lock_path(store_dir)):
        old = None
        corrupt = False
        try:
            old = read_profile(store_dir)
        except DeviceProfileError:
            if not force:
                raise
            # Explicit rotation is also the repair path for a corrupt file:
            # keep the bytes for inspection, then install a new profile.
            corrupt = True
        if old is None:
            old = profile_from_session(legacy_session)
        if old is not None and not force:
            raise DeviceProfileError('设备指纹已存在；更换需要显式确认')

        backup = None
        if corrupt:
            backup = _backup_path(store_dir, _now_ms()).replace(
                '.json', '.corrupt.json')
            shutil.copy2(_profile_path(store_dir), backup)
        if old is not None:
            legacy_backup = _backup_path(store_dir, _now_ms())
            _write_json_atomic(legacy_backup, old)
            if backup is None:
                backup = legacy_backup

        new = generate_profile(source='rotated', platform=platform)
        new['rotated_at'] = _now_ms()
        new['rotated_from'] = (old or {}).get('source', 'none')
        return save_profile(store_dir, new), backup


def effective_device(profile: dict, session: dict | None = None) -> dict:
    """Merge an explicit session identity over the installation profile.

    ``real_set_session``/CLI still support a manual DeviceId.  If one is
    present, login and submit must both see it; otherwise the two paths would
    drift apart again.
    """
    device = dict(validate_profile(profile))
    if not isinstance(session, dict):
        return device
    if not session.get('DeviceId'):
        return device
    device['DeviceId'] = str(session['DeviceId'])
    device['source'] = 'session'
    for key in ('platform', 'deviceName', 'osVersion', 'cpuModel',
                'physicPixel', 'logicPixel', 'isRoot', 'idfa',
                'appInstallTime', 'appUpdateTime'):
        if session.get(key) not in (None, ''):
            device[key] = str(session[key])
    return validate_profile(device)


def apply_to_account(acct, device: dict) -> None:
    """Copy every device field onto a realclient.Account."""
    device = validate_profile(device)
    acct.platform = device['platform']
    acct.DeviceId = device['DeviceId']
    acct.deviceName = device['deviceName']
    acct.osVersion = device['osVersion']
    acct.cpuModel = device['cpuModel']
    acct.physicPixel = device['physicPixel']
    acct.logicPixel = device['logicPixel']
    acct.appInstallTime = device['appInstallTime']
    acct.appUpdateTime = device['appUpdateTime']
    acct.isRoot = device['isRoot']
    acct.idfa = device.get('idfa', '')


def mask_device_id(value: str) -> str:
    value = str(value or '')
    if len(value) <= 8:
        return value[:2] + '…'
    return value[:4] + '…' + value[-4:]


def public_view(profile: dict, session: dict | None = None) -> dict:
    device = effective_device(profile, session)
    return {
        'device_id_present': True,
        'device_id_masked': mask_device_id(device['DeviceId']),
        'device_platform': device['platform'],
        'device_name': device['deviceName'],
        'device_os': device['osVersion'],
        'device_source': device['source'],
        'device_rotated_at': device.get('rotated_at'),
    }


def repair_pixels(store_dir: str) -> tuple[dict, str | None, list[str]]:
    """Correct only the two pixel fields for a known model.

    This does **not** rotate the DeviceId.  It exists for profiles created by
    the early generator, which wrote the physical resolution into
    ``logicPixel``.  The old profile is backed up first.  Any other mismatch
    (OS, CPU, root) is left for an explicit rotate, because changing those is
    a larger fingerprint change.
    """
    store_dir = os.path.abspath(store_dir)
    os.makedirs(store_dir, exist_ok=True)
    with _file_lock(_lock_path(store_dir)):
        profile = read_profile(store_dir)
        if profile is None:
            raise DeviceProfileError('没有设备指纹文件')
        table = (ANDROID_MODELS if profile['platform'] == 'android'
                 else IOS_MODELS)
        row = next((r for r in table if r[0] == profile['deviceName']), None)
        if row is None:
            raise DeviceProfileError(
                f'未知机型 {profile["deviceName"]}，不能自动修正像素')
        _, _, physic, logic = row
        old = dict(profile)
        changes = []
        if profile['physicPixel'] != physic:
            changes.append(f'physicPixel {profile["physicPixel"]} -> {physic}')
            profile['physicPixel'] = physic
        if profile['logicPixel'] != logic:
            changes.append(f'logicPixel {profile["logicPixel"]} -> {logic}')
            profile['logicPixel'] = logic
        if not changes:
            return profile, None, []
        backup = _backup_path(store_dir, _now_ms())
        _write_json_atomic(backup, old)
        return save_profile(store_dir, profile), backup, changes


def audit_profile(profile: dict) -> list[str]:
    """Return human-readable coherence problems for a profile.

    This is deliberately advisory: legacy fingerprints are frozen on purpose,
    so an old install can legitimately fail the modern model table.  New
    profiles must pass with an empty list.
    """
    p = validate_profile(profile)
    issues: list[str] = []
    if p['platform'] == 'android':
        row = next((r for r in ANDROID_MODELS if r[0] == p['deviceName']), None)
        if row is None:
            issues.append(f'unknown Android model {p["deviceName"]!r}')
        else:
            _, versions, physic, logic = row
            if p['osVersion'] not in versions:
                issues.append(
                    f'{p["deviceName"]} cannot run Android {p["osVersion"]}')
            if p['physicPixel'] != physic:
                issues.append(
                    f'{p["deviceName"]} physical pixels are {physic}, '
                    f'not {p["physicPixel"]}')
            if p['logicPixel'] != logic:
                issues.append(
                    f'{p["deviceName"]} logical pixels are {logic}, '
                    f'not {p["logicPixel"]}')
        if p['cpuModel'] != 'arm64-v8a':
            issues.append(f'Android cpuModel is {p["cpuModel"]!r}')
        if p.get('idfa'):
            issues.append('Android profile carries an IDFA')
    elif p['platform'] == 'ios':
        row = next((r for r in IOS_MODELS if r[0] == p['deviceName']), None)
        if row is None:
            issues.append(f'unknown iOS model {p["deviceName"]!r}')
        else:
            _, versions, physic, logic = row
            if p['osVersion'] not in versions:
                issues.append(
                    f'{p["deviceName"]} cannot run iOS {p["osVersion"]}')
            if p['physicPixel'] != physic:
                issues.append(
                    f'{p["deviceName"]} physical pixels are {physic}, '
                    f'not {p["physicPixel"]}')
            if p['logicPixel'] != logic:
                issues.append(
                    f'{p["deviceName"]} logical pixels are {logic}, '
                    f'not {p["logicPixel"]}')
        if p['cpuModel'] != IOS_CPU_MODEL:
            issues.append(f'iOS cpuModel is {p["cpuModel"]!r}')
        if not p.get('idfa'):
            issues.append('iOS profile has no IDFA')
    if p['isRoot'] != '0':
        issues.append('isRoot is not 0')
    if int(p['appUpdateTime']) < int(p['appInstallTime']):
        issues.append('appInstallTime is later than appUpdateTime')
    return issues


def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='Inspect or rotate the per-installation device profile.')
    ap.add_argument('--data-dir', required=True,
                    help='directory containing accounts_emu.json')
    ap.add_argument('--rotate', action='store_true',
                    help='back up the old profile and generate a new one')
    ap.add_argument('--platform', choices=('android', 'ios', 'random'),
                    default='random',
                    help='platform for --rotate (default: random)')
    ap.add_argument('--show', action='store_true',
                    help='print the effective profile (masked)')
    ap.add_argument('--audit', action='store_true',
                    help='print coherence warnings for the current profile')
    ap.add_argument('--repair-pixels', action='store_true',
                    help='fix only physical/logical pixels for a known model; '
                         'backs up the old profile and keeps DeviceId')
    args = ap.parse_args(argv)

    store_dir = os.path.abspath(os.path.expanduser(args.data_dir))
    session = read_session(store_dir)

    if args.repair_pixels:
        try:
            profile, backup, changes = repair_pixels(store_dir)
        except DeviceProfileError as e:
            print(f'ERROR {e}')
            return 1
        if not changes:
            print('OK    pixels already match the model table')
        else:
            for change in changes:
                print(f'FIXED {change}')
            if backup:
                print(f'backup: {backup}')
        print(json.dumps(public_view(profile, session), ensure_ascii=False,
                         indent=1))
        return 0

    if args.rotate:
        platform = None if args.platform == 'random' else args.platform
        new, backup = rotate(store_dir, legacy_session=session, force=True,
                             platform=platform)
        label = 'iOS' if new['platform'] == 'ios' else 'Android'
        print(f'rotated: {mask_device_id(new["DeviceId"])}  '
              f'source={new["source"]}  platform={new["platform"]}')
        print(f'device : {new["deviceName"]} / {label} {new["osVersion"]}')
        if backup:
            print(f'backup : {backup}')
        return 0

    profile = load_or_create(store_dir, legacy_session=session)
    if args.audit:
        issues = audit_profile(profile)
        if issues:
            for issue in issues:
                print(f'WARN  {issue}')
        else:
            print('OK    profile is coherent')
    print(json.dumps(public_view(profile, session), ensure_ascii=False,
                     indent=1))
    return 0


# --------------------------------------------------------------------------
# self test (offline)
# --------------------------------------------------------------------------

def self_test() -> list[tuple[str, bool, str]]:
    out = []
    root = tempfile.mkdtemp(prefix='jhp_device_')
    try:
        a = load_or_create(os.path.join(root, 'a'))
        b = load_or_create(os.path.join(root, 'b'))
        out.append(('device id has a stable shape',
                    bool(re.fullmatch(r'[0-9A-Fa-f-]{16,64}', a['DeviceId'])),
                    a['DeviceId'][:8] + '…'))
        out.append(('two installs differ', a['DeviceId'] != b['DeviceId'],
                    'different'))
        out.append(('profile is stable',
                    load_or_create(os.path.join(root, 'a')) == a, 'reloaded'))

        android = generate_profile(platform='android')
        out.append(('android profile is coherent',
                    not audit_profile(android)
                    and android['isRoot'] == '0'
                    and android['cpuModel'] == 'arm64-v8a',
                    f'{android["deviceName"]} / {android["osVersion"]}'))

        ios = generate_profile(platform='ios')
        out.append(('ios profile is coherent',
                    not audit_profile(ios)
                    and ios['cpuModel'] == IOS_CPU_MODEL
                    and bool(re.fullmatch(
                        r'[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-'
                        r'[0-9A-F]{4}-[0-9A-F]{12}', ios['DeviceId'])),
                    f'{ios["deviceName"]} / iOS {ios["osVersion"]}'))

        broken = dict(ios)
        broken['physicPixel'] = '1080x1920'
        out.append(('audit catches impossible pixels',
                    bool(audit_profile(broken)), 'warned'))

        legacy_dir = os.path.join(root, 'legacy')
        os.makedirs(legacy_dir)
        _write_json_atomic(os.path.join(legacy_dir, 'accounts_emu.json'),
                           {'uid': '1', 'token': 'tok'})
        legacy = load_or_create(legacy_dir,
                                legacy_session=read_session(legacy_dir))
        out.append(('legacy session freezes default',
                    legacy['DeviceId'] == LEGACY_DEVICE['DeviceId']
                    and legacy['source'] == 'legacy-default',
                    legacy['DeviceId'][:8] + '…'))

        new, backup = rotate(legacy_dir,
                             legacy_session=read_session(legacy_dir),
                             force=True)
        with open(backup, encoding='utf-8') as f:
            saved = json.load(f)
        out.append(('rotate backs up old id',
                    new['DeviceId'] != legacy['DeviceId']
                    and saved['DeviceId'] == legacy['DeviceId'],
                    'backup ok'))
        out.append(('rotate marks source', new['source'] == 'rotated',
                    new['source']))

        view = public_view(new)
        out.append(('public view masks id',
                    new['DeviceId'] not in json.dumps(view)
                    and view['device_id_masked'].startswith(
                        new['DeviceId'][:4]),
                    view['device_id_masked']))

        bad = os.path.join(root, 'bad')
        os.makedirs(bad)
        with open(os.path.join(bad, PROFILE_FILENAME), 'w',
                  encoding='utf-8') as f:
            f.write('{broken')
        try:
            load_or_create(bad)
            out.append(('corrupt profile fails closed', False, 'no error'))
        except DeviceProfileError:
            out.append(('corrupt profile fails closed', True, 'error'))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return out


if __name__ == '__main__':
    raise SystemExit(_cli())

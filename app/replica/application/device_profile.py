"""One stable device identity per installation.

Why this module exists
----------------------
The service clusters accounts by the request-header fingerprint.  A single
compiled-in ``DeviceId`` makes every copy of the tool look like the same phone;
generating a fresh id on every launch is worse because the account appears to
change phone every day.  The only stable shape is:

* create the profile once per data directory;
* keep it forever (login, submit, OBS readback all use the same one);
* never rotate a **valid** profile silently.  An explicit ``rotate()`` backs up
  the old profile.

The one exception is a profile that cannot be sent: the old compiled-in
fingerprint shared by every installation (``LEGACY_DEVICE``) and fields that
cannot coexist (``x86_64`` CPU, ``isRoot=1``, wrong model/OS/pixels).  Those are
migrated once on load, with a ``device_profile.backup-*.json`` backup and a
forced re-login.  A valid profile with a unique ``DeviceId`` is never changed.

New installations default to Android.  ``JHP_IOS_CHANCE`` can opt into a
mixed pool, and iOS remains selectable when rotating or defining a custom
device.  Once written, the profile is stable regardless of platform: the same
DeviceId and device fields are used for login, submission and readback.

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
#: One-shot notice consumed by the desktop UI after a legacy fingerprint was
#: repaired.  It is deliberately a separate file: ``device_profile.json`` is
#: validated as a strict schema and must never grow UI-only fields.
MIGRATION_FILENAME = 'device_migration.json'

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

#: Human-readable labels for the UI.  The wire still uses the model code.
ANDROID_LABELS = {
    '22081212C': 'Redmi K50 Ultra',
    '2211133C': 'Xiaomi 13',
    '23127PN0CC': 'Xiaomi 14',
    'PJE110': 'OnePlus Ace 3',
    'SM-S9210': 'Galaxy S24',
    'Pixel 8': 'Pixel 8',
    'V2309A': 'vivo X100 Pro',
    '23013RK75C': 'Redmi K60',
}

#: iOS profiles used when the random pool picks Apple.  Model, OS, physical
#: pixels and logical pixels stay in one row so the fingerprint cannot pair an
#: iPhone 15 with an iPhone 8 resolution.  ``cpuModel`` uses arm64e rather than
#: the reference simulator's x86_64: an iPhone running iOS 18 with x86_64 is an
#: obvious emulator signal.  ``physicPixel`` is always an exact 2x/3x multiple
#: of ``logicPixel``, which is the pair the native client reports.
#:
#: The iOS chain (login -> policy -> points -> submit -> OBS) is verified on a
#: real device (rrid 1344757460, 2026-10-25); ``JHP_IOS_CHANCE`` is a
#: distribution knob, not an exposure limit.
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
#: New installations default to Android.  iOS stays available through the
#: rotate dialog (`platform=ios`), the custom-device form, or
#: `JHP_IOS_CHANCE=1..100` for operators who explicitly want the iOS share.
IOS_CHANCE_DEFAULT = 0
#: Most real iPhones deny App Tracking Transparency, so IDFA is all zeros.
_IDFA_ZERO = '00000000-0000-0000-0000-000000000000'

_DEVICE_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$')
_IDFA_RE = re.compile(r'^[A-Za-z0-9-]{8,64}$')
_PIXEL_RE = re.compile(r'^\d{3,5}x\d{3,5}$')
_DIGITS_RE = re.compile(r'^\d{10,16}$')

_SOURCES = frozenset({
    'generated', 'legacy-default', 'legacy-session', 'rotated', 'session',
    'migrated', 'repaired', 'custom',
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
    from ..build_info import app_root
    return os.path.join(app_root(), 'data')


def read_session(store_dir: str) -> dict:
    try:
        from . import session_store
        data = session_store.read(_session_path(store_dir))
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


def _native_core():
    """Shared sidecar, or None when this checkout has no binary.

    `JHP_RUST_CORE=1` (and frozen builds) must fail loudly, so
    `RustCoreRequired` is not swallowed.
    """
    from ..transport import rustcore
    try:
        return rustcore.get()
    except rustcore.RustCoreRequired:
        raise
    except rustcore.RustCoreUnavailable:
        return None


def validate_profile(profile) -> dict:
    """Return a normalised copy, or raise if the file is not usable.

    The native core owns the schema checks; `_validate_profile` is the Python
    reference for source checkouts and tests.
    """
    from ..transport import rustcore
    core = _native_core()
    if core is not None:
        try:
            return core.call('device_validate', profile=profile,
                             now_ms=_now_ms())
        except rustcore.RustCoreError as exc:
            raise DeviceProfileError(str(exc))
    return _validate_profile(profile)


def _validate_profile(profile) -> dict:
    """Python reference implementation of the profile schema.

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
    if profile.get('custom_at'):
        try:
            out['custom_at'] = int(profile['custom_at'])
        except (TypeError, ValueError):
            pass
    if profile.get('custom_from'):
        out['custom_from'] = str(profile['custom_from'])[:32]
    for stamp in ('migrated_at', 'repaired_at'):
        if profile.get(stamp):
            try:
                out[stamp] = int(profile[stamp])
            except (TypeError, ValueError):
                pass
    for text_key in ('migrated_from', 'migration_reason',
                     'repaired_from', 'repair_reason',
                     'previous_device_masked'):
        if profile.get(text_key):
            out[text_key] = str(profile[text_key])[:64]
    return out


def _ios_chance() -> int:
    """iOS share for new random profiles (0-100, default 0).

    New installations default to Android.  iOS still has three entry points:
    ``JHP_IOS_CHANCE=1..100`` for a mixed pool, ``rotate(platform='ios')``, and
    the custom-device form.  ``100`` forces iOS on new installs.
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
    core = _native_core()
    if core is not None:
        return core.call('device_generate', now_ms=now, source=source,
                         platform='android')
    model, os_choices, physic, logic = secrets.choice(ANDROID_MODELS)
    os_version = secrets.choice(os_choices)
    # A real installation is older than the first run.  30-400 days keeps
    # install history plausible without every copy sharing one offset.  The
    # app is current (7.4.0), so appUpdateTime is recent and later than the
    # install; an install from last year with update == install would imply
    # the app was never updated since 7.4.0 shipped.
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
    core = _native_core()
    if core is not None:
        return core.call('device_generate', now_ms=now, source=source,
                         platform='ios')
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
    the pool is Android by default; ``JHP_IOS_CHANCE`` opts into a mixed pool.
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
    """Build a clean profile from an old session.

    Older builds did not persist a profile and used a compiled-in Android
    fingerprint for every installation.  Freezing that fingerprint is exactly
    what made all upgraded users look like one rooted x86 device, so it must
    never be reused.  A unique ``DeviceId`` that the session already carries is
    preserved; every other field is checked and repaired to a coherent profile.
    """
    if not isinstance(session, dict):
        return None
    if not (session.get('token') or session.get('uid')):
        return None
    platform = str(session.get('platform') or 'android').strip().lower()
    if platform == 'ios':
        profile = generate_ios_profile(source='legacy-session')
    else:
        profile = generate_android_profile(source='legacy-session')

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

    session_id = str(session.get('DeviceId') or '').strip()
    shared_id = (not session_id
                 or session_id.lower() == LEGACY_DEVICE['DeviceId'].lower())
    if not shared_id:
        # A manual/imported id is the one piece of history worth keeping.
        profile['DeviceId'] = session_id

    # The session may carry a real model plus the old synthetic CPU/root
    # fields, or a field an old build wrote in the wrong type.  Repair when
    # possible; rebuild from the clean base when the copied field is invalid.
    try:
        issues = audit_profile(profile)
    except DeviceProfileError:
        profile = (generate_ios_profile(source='legacy-session')
                   if platform == 'ios'
                   else generate_android_profile(source='legacy-session'))
        if not shared_id:
            profile['DeviceId'] = session_id
        issues = audit_profile(profile)

    if issues:
        profile, _ = repair_profile(profile, reason='legacy-session')
        if shared_id:
            # The id was also the shared legacy constant; the new random id
            # inside the repaired profile is the migration, not a field repair.
            profile['source'] = 'migrated'
            profile['migrated_from'] = 'legacy-default'
            profile['migrated_at'] = _now_ms()
            profile['migration_reason'] = 'shared-legacy-device'
            profile['previous_device_masked'] = _mask_device(
                LEGACY_DEVICE['DeviceId'])
            for stale in ('repaired_at', 'repaired_from', 'repair_reason'):
                profile.pop(stale, None)
        else:
            profile['source'] = 'repaired'
            profile['repaired_from'] = 'legacy-session'
    elif shared_id:
        # No unique identity exists.  Mark the new random profile as a
        # migration so the UI can explain the one-time logout.
        profile['source'] = 'migrated'
        profile['migrated_from'] = 'legacy-default'
        profile['migrated_at'] = _now_ms()
        profile['migration_reason'] = 'shared-legacy-device'
    return validate_profile(profile)


def read_profile(store_dir: str) -> dict | None:
    path = _profile_path(store_dir)
    if not os.path.exists(path):
        return None
    return validate_profile(_read_json(path, None))


def save_profile(store_dir: str, profile: dict) -> dict:
    profile = validate_profile(profile)
    if is_shared_legacy(profile):
        raise DeviceProfileError(
            '拒绝保存旧版共享设备指纹；请生成独立设备身份')
    _write_json_atomic(_profile_path(store_dir), profile)
    return profile


def load_or_create(store_dir: str, legacy_session: dict | None = None) -> dict:
    """Read the profile, or create a coherent one for this data directory.

    Existing legacy profiles are migrated once, with a backup.  The detailed
    variant :func:`load_or_create_ex` also returns the migration notice used by
    the desktop shell to force a re-login.
    """
    return load_or_create_ex(store_dir, legacy_session=legacy_session)[0]


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
        new = save_profile(store_dir, new)
        clear_migration_marker(store_dir)
        return new, backup


def device_catalog() -> dict:
    """Example model/OS combinations for the custom-device form.

    The form is free-form now: this catalog only supplies default pixel/CPU
    suggestions and the rotate presets.  ``audit_profile`` validates custom
    profiles against platform rules (aspect ratio, 2x/3x scale, CPU) instead
    of requiring a known model.
    """
    android = []
    for name, versions, physic, logic in ANDROID_MODELS:
        android.append({
            'deviceName': name,
            'label': ANDROID_LABELS.get(name, name),
            'osVersions': list(versions),
            'physicPixel': physic,
            'logicPixel': logic,
            'cpuModel': 'arm64-v8a',
        })
    ios = []
    for name, versions, physic, logic in IOS_MODELS:
        ios.append({
            'deviceName': name,
            'label': name,
            'osVersions': list(versions),
            'physicPixel': physic,
            'logicPixel': logic,
            'cpuModel': IOS_CPU_MODEL,
        })
    return {
        'android': android,
        'ios': ios,
        'default_install_days': 90,
        'max_install_days': 3650,
    }


def build_custom_profile(*, platform, device_name, os_version, device_id,
                         install_days=None, idfa=None,
                         physic_pixel=None, logic_pixel=None,
                         cpu_model=None,
                         now_ms: int | None = None) -> dict:
    """Validate a custom device without writing it.

    Model, OS, pixels and CPU are operator-supplied.  Missing pixel/CPU values
    fall back to the matching catalog row (or the platform default) so older
    callers keep working.  Raises :class:`DeviceProfileError` with a
    user-facing message when the combination is not sendable.
    """
    platform = str(platform or '').strip().lower()
    if platform not in ('android', 'ios'):
        raise DeviceProfileError('平台必须是 android 或 ios')
    name = str(device_name or '').strip()
    os_version = str(os_version or '').strip()
    if not name:
        raise DeviceProfileError('请填写机型')
    if not os_version:
        raise DeviceProfileError('请填写系统版本')
    rows = ANDROID_MODELS if platform == 'android' else IOS_MODELS
    row = next((r for r in rows if r[0] == name), None)
    fallback = {
        'android': ('1080x2400', '393x873', 'arm64-v8a'),
        'ios': ('1179x2556', '393x852', IOS_CPU_MODEL),
    }[platform]
    physic = str(physic_pixel if physic_pixel not in (None, '') else
                 (row[2] if row else fallback[0])).strip()
    logic = str(logic_pixel if logic_pixel not in (None, '') else
                (row[3] if row else fallback[1])).strip()
    cpu = str(cpu_model if cpu_model not in (None, '') else fallback[2]).strip()

    device_id = str(device_id or '').strip()
    if not _DEVICE_ID_RE.match(device_id):
        raise DeviceProfileError(
            'DeviceId 需为 1–64 位字母/数字/._:-，且以字母或数字开头')

    if install_days in (None, ''):
        days = 90
    else:
        try:
            days = int(str(install_days).strip())
        except (TypeError, ValueError):
            raise DeviceProfileError('安装天数必须是整数')
        if days < 0 or days > 3650:
            raise DeviceProfileError('安装天数需在 0–3650 之间')

    now = int(now_ms if now_ms is not None else _now_ms())
    install = now - days * 86400000
    profile = {
        'version': PROFILE_VERSION,
        'platform': platform,
        'source': 'custom',
        'created_at': now,
        'DeviceId': device_id,
        'deviceName': name,
        'osVersion': os_version,
        'cpuModel': cpu,
        'physicPixel': physic,
        'logicPixel': logic,
        'isRoot': '0',
        'appInstallTime': str(install),
        'appUpdateTime': str(now),
    }
    if platform == 'ios':
        profile['idfa'] = str(idfa or _IDFA_ZERO)
    profile = validate_profile(profile)
    issues = audit_profile(profile)
    if issues:
        raise DeviceProfileError('自定义设备不自洽: ' + '；'.join(issues))
    return profile


def set_custom(store_dir: str, profile: dict) -> tuple[dict, str | None]:
    """Install a pre-validated custom profile, backing up the old one.

    The caller is responsible for ending the local session beforehand; this
    function only touches ``device_profile.json`` and its backup.
    """
    candidate = validate_profile(profile)
    issues = audit_profile(candidate)
    if issues:
        raise DeviceProfileError('自定义设备不自洽: ' + '；'.join(issues))
    if is_shared_legacy(candidate):
        raise DeviceProfileError('自定义设备不能使用旧版共享 DeviceId')

    store_dir = os.path.abspath(store_dir)
    os.makedirs(store_dir, exist_ok=True)
    with _file_lock(_lock_path(store_dir)):
        old = None
        corrupt = False
        try:
            old = read_profile(store_dir)
        except DeviceProfileError:
            corrupt = True
        backup = None
        if corrupt:
            backup = _backup_path(store_dir, _now_ms()).replace(
                '.json', '.corrupt.json')
            shutil.copy2(_profile_path(store_dir), backup)
        elif old is not None:
            backup = _backup_path(store_dir, _now_ms())
            _write_json_atomic(backup, old)

        candidate = dict(candidate)
        candidate['source'] = 'custom'
        candidate['custom_at'] = _now_ms()
        candidate['custom_from'] = (old or {}).get('source', 'none')
        new = save_profile(store_dir, candidate)
        clear_migration_marker(store_dir)
        return new, backup


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
    # A platform override must not inherit the other platform's identifier.
    if device['platform'] == 'android':
        device.pop('idfa', None)
    elif not device.get('idfa'):
        device['idfa'] = _IDFA_ZERO
    return validate_profile(device)


def apply_to_account(acct, device: dict) -> None:
    """Copy every device field onto a realclient.Account."""
    device = validate_profile(device)
    if is_shared_legacy(device):
        raise DeviceProfileError(
            '旧版共享设备指纹不能用于登录或提交；请先修复设备指纹')
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
        'device_physic_pixel': device['physicPixel'],
        'device_logic_pixel': device['logicPixel'],
        'device_cpu': device['cpuModel'],
        'device_is_root': device['isRoot'],
        'device_source': device['source'],
        'device_rotated_at': device.get('rotated_at'),
        'device_custom_at': device.get('custom_at'),
        # Transparency for the account page: the UA and the pixel scale are
        # what actually go on the wire, and the install age is a field the
        # server can see on every request.
        'device_ua': _wire_ua(device),
        'device_scale': _pixel_scale(device.get('physicPixel'),
                                     device.get('logicPixel'),
                                     device.get('platform')),
        'device_installed_days': _install_age_days(device.get('appInstallTime')),
        'device_migrated_at': device.get('migrated_at'),
        'device_migrated_from': device.get('migrated_from'),
        'device_repaired_at': device.get('repaired_at'),
        'device_repaired_from': device.get('repaired_from'),
        'device_environment_ok': not audit_profile(device),
    }


def _wire_ua(device: dict) -> str:
    try:
        from ..transport.realclient import user_agent
        return user_agent(platform=device.get('platform'),
                          os_version=str(device.get('osVersion') or ''))
    except Exception:  # noqa: BLE001 - display only, never block the session
        return ''


def _pixel_scale(physic, logic, platform='ios'):
    """physicPixel / logicPixel when it is a whole 2x/3x, else 0.

    Only iOS reports a scale factor: Android's logicPixel is a dp size, not a
    divisor of the panel resolution, so 1170/390 style maths does not apply.
    """
    if str(platform or '').lower() != 'ios':
        return 0
    try:
        lw = int(str(logic).split('x')[0])
        pw = int(str(physic).split('x')[0])
        ratio = pw // lw if lw > 0 and pw % lw == 0 else 0
        if ratio in (2, 3):
            return ratio
    except (TypeError, ValueError, IndexError, ZeroDivisionError):
        pass
    return 0


def _install_age_days(install) -> float:
    try:
        value = int(str(install))
    except (TypeError, ValueError):
        return 0.0
    if value <= 0:
        return 0.0
    return round(max(0.0, (_now_ms() - value) / 86_400_000.0), 1)


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


def _pixel_pair(text) -> tuple[int, int]:
    try:
        width, height = str(text).strip().lower().split('x', 1)
        return int(width), int(height)
    except (TypeError, ValueError):
        return 0, 0


def _audit_custom_device(p: dict) -> list[str]:
    """Platform rules for free-form devices, instead of a model allow-list."""
    issues: list[str] = []
    if any(ord(ch) < 32 for ch in str(p.get('deviceName') or '')):
        issues.append('机型包含控制字符')
    pw, ph = _pixel_pair(p.get('physicPixel'))
    lw, lh = _pixel_pair(p.get('logicPixel'))
    if pw <= 0 or ph <= 0 or lw <= 0 or lh <= 0:
        issues.append('分辨率无效')
        return issues
    if pw >= ph:
        issues.append(f'物理分辨率 {p["physicPixel"]} 不是竖屏')
    if lw >= lh:
        issues.append(f'逻辑分辨率 {p["logicPixel"]} 不是竖屏')
    if p['platform'] == 'android':
        match = re.match(r'^(\d{1,2})(?:\.\d{1,2}){0,3}$', p['osVersion'])
        major = int(match.group(1)) if match else 0
        if not match or not 7 <= major <= 18:
            issues.append(f'Android 版本 {p["osVersion"]!r} 不在 7–18')
        if p['cpuModel'] != 'arm64-v8a':
            issues.append(f'Android cpuModel is {p["cpuModel"]!r}')
        if pw < lw or ph < lh:
            issues.append('物理分辨率小于逻辑分辨率')
        elif abs(pw / ph - lw / lh) > 0.08:
            issues.append('物理/逻辑分辨率比例不一致')
        if p.get('idfa'):
            issues.append('Android profile carries an IDFA')
    else:
        match = re.match(r'^(\d{1,2})(?:\.\d{1,2}){0,2}$', p['osVersion'])
        major = int(match.group(1)) if match else 0
        if not match or not 12 <= major <= 30:
            issues.append(f'iOS 版本 {p["osVersion"]!r} 不在 12–30')
        if p['cpuModel'] != IOS_CPU_MODEL:
            issues.append(f'iOS cpuModel is {p["cpuModel"]!r}')
        width_scale = pw / lw
        height_scale = ph / lh
        whole = round(width_scale)
        if (whole not in (2, 3)
                or abs(width_scale - whole) > 0.02
                or abs(height_scale - whole) > 0.02):
            issues.append('iOS 物理/逻辑像素必须是 2x 或 3x')
        if not p.get('idfa'):
            issues.append('iOS profile has no IDFA')
    return issues


def audit_profile(profile: dict) -> list[str]:
    """Return human-readable coherence problems for a profile.

    A profile that fails this audit is not necessarily corrupt, but it cannot
    be sent: ``cpuModel=x86_64``, ``isRoot=1`` and an impossible model/OS pair
    are exactly the environment signals the server's suspicious-record check
    looks for.  ``migrate_profile`` repairs them once, with a backup.
    """
    p = validate_profile(profile)
    issues: list[str] = []
    if p.get('source') == 'custom':
        issues.extend(_audit_custom_device(p))
    elif p['platform'] == 'android':
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


def is_shared_legacy(profile) -> bool:
    """True for the old compiled-in fingerprint shared by every installation."""
    if not isinstance(profile, dict):
        return False
    if str(profile.get('source') or '') == 'legacy-default':
        return True
    return (str(profile.get('DeviceId') or '').strip().lower()
            == LEGACY_DEVICE['DeviceId'].lower())


def _mask_device(value) -> str:
    return mask_device_id(str(value or ''))


def _normalise_times(profile: dict, now: int) -> list[str]:
    """Keep plausible install/update times; clamp future or inverted values."""
    changes = []
    install = int(profile['appInstallTime'])
    update = int(profile['appUpdateTime'])
    if install <= 0 or install > now:
        install = now - (30 + secrets.randbelow(371)) * 86_400_000
        changes.append('appInstallTime clamped')
    if update <= 0 or update > now:
        update = now - (1 + secrets.randbelow(21)) * 86_400_000
        changes.append('appUpdateTime clamped')
    if update < install:
        update = install
        changes.append('appUpdateTime raised to appInstallTime')
    profile['appInstallTime'] = str(install)
    profile['appUpdateTime'] = str(update)
    return changes


def repair_profile(profile: dict, reason: str = 'incoherent-profile'):
    """Make a coherent profile while keeping a unique ``DeviceId``.

    Used for profiles created by early generators (wrong pixels, x86_64 CPU,
    ``isRoot=1``) and for old sessions that carried a real model plus the old
    synthetic fields.  A shared legacy id is **not** repaired here: it must be
    replaced by a new random profile because every old copy carries the same
    id.  See :func:`migrate_profile`.
    """
    p = validate_profile(profile)
    issues = audit_profile(p)
    if not issues:
        return p, []

    old_source = p.get('source', 'unknown')
    old_id = p['DeviceId']
    changes = list(issues)
    platform = p['platform']
    table = ANDROID_MODELS if platform == 'android' else IOS_MODELS
    row = next((r for r in table if r[0] == p['deviceName']), None)
    if row is None:
        row = secrets.choice(table)
    model, versions, physic, logic = row

    if p['deviceName'] != model:
        p['deviceName'] = model
    if p['osVersion'] not in versions:
        p['osVersion'] = secrets.choice(versions)
    p['physicPixel'] = physic
    p['logicPixel'] = logic
    p['cpuModel'] = 'arm64-v8a' if platform == 'android' else IOS_CPU_MODEL
    p['isRoot'] = '0'
    if platform == 'android':
        p.pop('idfa', None)
    elif not p.get('idfa'):
        p['idfa'] = _IDFA_ZERO
    changes += _normalise_times(p, _now_ms())

    p['source'] = 'repaired'
    p['repaired_from'] = old_source
    p['repaired_at'] = _now_ms()
    p['repair_reason'] = str(reason or 'incoherent-profile')[:64]
    p['previous_device_masked'] = _mask_device(old_id)
    return validate_profile(p), changes


def _migration_message(kind: str) -> str:
    if kind == 'shared-legacy-device':
        return ('检测到旧版共享设备指纹（老用户共用同一台设备，且带有 '
                'x86_64/root 特征），已为本机生成独立指纹。'
                '为避免旧 token 与新设备混用，本机登录已退出，请重新登录。'
                '旧指纹已备份。')
    return ('检测到旧版设备指纹字段不一致（CPU/root/机型），'
            '已修正为本机独立指纹。请重新登录后再提交；旧指纹已备份。')


def _migration_info(profile: dict, *, kind: str, backup: str = '',
                    old_device: str = '', changes=None) -> dict:
    return {
        'kind': kind,
        'at': int(profile.get('migrated_at')
                  or profile.get('repaired_at') or _now_ms()),
        'from': str(profile.get('migrated_from')
                    or profile.get('repaired_from') or 'legacy'),
        'backup': os.path.basename(backup) if backup else '',
        'old_device_masked': (_mask_device(old_device)
                              if old_device else
                              str(profile.get('previous_device_masked') or '')),
        'message': _migration_message(kind),
        'changes': list(changes or [])[:8],
    }


def migrate_profile(store_dir: str, profile: dict):
    """Back up and repair one old profile.  Caller must hold the file lock.

    Returns ``(profile, backup_path, migration_info)``.  A no-op returns
    ``(profile, None, None)``.
    """
    old = dict(profile)
    if is_shared_legacy(old):
        platform = 'ios' if old.get('platform') == 'ios' else 'android'
        new = generate_profile(platform=platform, source='migrated')
        new['migrated_from'] = str(old.get('source') or 'legacy-default')
        new['migrated_at'] = _now_ms()
        new['migration_reason'] = 'shared-legacy-device'
        new['previous_device_masked'] = _mask_device(old.get('DeviceId'))
        backup = _backup_path(store_dir, _now_ms())
        _write_json_atomic(backup, old)
        new = save_profile(store_dir, new)
        info = _migration_info(new, kind='shared-legacy-device',
                               backup=backup, old_device=old.get('DeviceId'))
        return new, backup, info

    issues = audit_profile(old)
    if not issues:
        return old, None, None
    new, changes = repair_profile(old, reason='incoherent-profile')
    backup = _backup_path(store_dir, _now_ms())
    _write_json_atomic(backup, old)
    new = save_profile(store_dir, new)
    info = _migration_info(new, kind='incoherent-profile', backup=backup,
                           old_device=old.get('DeviceId'), changes=changes)
    return new, backup, info


def read_migration_marker(store_dir: str) -> dict | None:
    """Read the one-shot UI notice left by a profile migration."""
    path = os.path.join(os.path.abspath(store_dir), MIGRATION_FILENAME)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) and data.get('message') else None
    except Exception:  # noqa: BLE001 - a missing/broken notice is not fatal
        return None


def write_migration_marker(store_dir: str, info: dict | None) -> None:
    if not info:
        return
    _write_json_atomic(os.path.join(os.path.abspath(store_dir),
                                    MIGRATION_FILENAME), info)


def clear_migration_marker(store_dir: str) -> None:
    path = os.path.join(os.path.abspath(store_dir), MIGRATION_FILENAME)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def load_or_create_ex(store_dir: str,
                      legacy_session: dict | None = None):
    """Return ``(profile, migration_info)`` and migrate old data once.

    The migration is intentionally **not** silent: it writes
    ``device_migration.json`` so the desktop/CLI can force a re-login.  A
    profile that needs no change returns ``(profile, None)``.
    """
    store_dir = os.path.abspath(store_dir)
    os.makedirs(store_dir, exist_ok=True)
    with _file_lock(_lock_path(store_dir)):
        current = read_profile(store_dir)
        if current is not None:
            new, backup, info = migrate_profile(store_dir, current)
            if info:
                write_migration_marker(store_dir, info)
            return new, info

        legacy = profile_from_session(legacy_session)
        profile = legacy if legacy is not None else generate_profile()
        profile = save_profile(store_dir, profile)
        info = None
        if profile.get('source') == 'migrated':
            info = _migration_info(profile, kind='shared-legacy-device',
                                   old_device=LEGACY_DEVICE['DeviceId'])
        elif profile.get('source') == 'repaired':
            info = _migration_info(profile, kind='incoherent-profile')
        if info:
            write_migration_marker(store_dir, info)
        return profile, info


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
    ap.add_argument('--migrate', action='store_true',
                    help='repair a shared/incoherent legacy profile once; '
                         'backs up the old profile and prints the notice')
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

    if args.migrate:
        try:
            profile, info = load_or_create_ex(store_dir, legacy_session=session)
        except DeviceProfileError as e:
            print(f'ERROR {e}')
            return 1
        print(json.dumps({'migration': info,
                          'device': public_view(profile, session)},
                         ensure_ascii=False, indent=1))
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
        out.append(('legacy session is migrated, not frozen',
                    legacy['DeviceId'] != LEGACY_DEVICE['DeviceId']
                    and legacy['source'] == 'migrated'
                    and not audit_profile(legacy),
                    legacy['DeviceId'][:8] + '…'))
        out.append(('migration notice is written',
                    bool(read_migration_marker(legacy_dir)), 'notice'))

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

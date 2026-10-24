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
#: model and OS together avoids impossible pairs such as Pixel 8 + Android 12.
#: Pixel/CPU fields stay at the Android values the reference client uses.
ANDROID_MODELS = (
    ('22081212C', ('13', '14', '15')),
    ('OPPO PGBM10', ('13', '14')),
    ('Redmi K60', ('13', '14')),
    ('HUAWEI Mate 40', ('12', '13')),
    ('vivo V2309A', ('13', '14')),
    ('Pixel 8', ('14', '15')),
    ('SM-S9210', ('14', '15')),
    ('OnePlus ACE3', ('14', '15')),
)

_DEVICE_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$')
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
    if platform != 'android':
        # The submit path in the reference client is Android-shaped.  Keeping
        # this explicit avoids a half-iOS profile being used by accident.
        raise DeviceProfileError('当前提交链只支持 android 设备指纹')

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
    if profile.get('rotated_at'):
        try:
            out['rotated_at'] = int(profile['rotated_at'])
        except (TypeError, ValueError):
            pass
    if profile.get('rotated_from'):
        out['rotated_from'] = str(profile['rotated_from'])[:32]
    return out


def generate_profile(*, now_ms: int | None = None,
                     source: str = 'generated') -> dict:
    """Create one coherent Android profile.  Never called per request."""
    now = int(now_ms if now_ms is not None else _now_ms())
    model, os_choices = secrets.choice(ANDROID_MODELS)
    os_version = secrets.choice(os_choices)
    # A real installation is older than the first run.  30-150 days keeps
    # install/update plausible without making every copy share one offset.
    install_days = 30 + secrets.randbelow(121)
    install = now - install_days * 86_400_000
    return {
        'version': PROFILE_VERSION,
        'platform': 'android',
        'source': source if source in _SOURCES else 'generated',
        'created_at': now,
        'DeviceId': secrets.token_hex(16),
        'deviceName': model,
        'osVersion': os_version,
        'cpuModel': 'arm64-v8a',
        'physicPixel': '1080x2400',
        'logicPixel': '1080x2400',
        'isRoot': '0',
        'appInstallTime': str(install),
        'appUpdateTime': str(install),
    }


def profile_from_session(session: dict | None) -> dict | None:
    """Freeze the identity an older build was already submitting with.

    A session without ``DeviceId`` used the compiled-in defaults.  A session
    with one (manual import / CLI) wins for that field.
    """
    if not isinstance(session, dict):
        return None
    if not (session.get('token') or session.get('uid')):
        return None
    profile = dict(LEGACY_DEVICE)
    profile['version'] = PROFILE_VERSION
    profile['source'] = 'legacy-default'
    profile['created_at'] = _now_ms()
    if session.get('DeviceId'):
        profile['DeviceId'] = str(session['DeviceId'])
        profile['source'] = 'legacy-session'
    for src, dst in (('deviceName', 'deviceName'),
                     ('osVersion', 'osVersion'),
                     ('cpuModel', 'cpuModel'),
                     ('physicPixel', 'physicPixel'),
                     ('logicPixel', 'logicPixel'),
                     ('isRoot', 'isRoot'),
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
           *, force: bool = False) -> tuple[dict, str | None]:
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

        new = generate_profile(source='rotated')
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
    for key in ('deviceName', 'osVersion', 'cpuModel', 'physicPixel',
                'logicPixel', 'isRoot', 'appInstallTime', 'appUpdateTime'):
        if session.get(key) not in (None, ''):
            device[key] = str(session[key])
    return validate_profile(device)


def apply_to_account(acct, device: dict) -> None:
    """Copy every device field onto a realclient.Account."""
    device = validate_profile(device)
    acct.DeviceId = device['DeviceId']
    acct.deviceName = device['deviceName']
    acct.osVersion = device['osVersion']
    acct.cpuModel = device['cpuModel']
    acct.physicPixel = device['physicPixel']
    acct.logicPixel = device['logicPixel']
    acct.appInstallTime = device['appInstallTime']
    acct.appUpdateTime = device['appUpdateTime']
    acct.isRoot = device['isRoot']


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
        'device_name': device['deviceName'],
        'device_os': device['osVersion'],
        'device_source': device['source'],
        'device_rotated_at': device.get('rotated_at'),
    }


def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='Inspect or rotate the per-installation device profile.')
    ap.add_argument('--data-dir', required=True,
                    help='directory containing accounts_emu.json')
    ap.add_argument('--rotate', action='store_true',
                    help='back up the old profile and generate a new one')
    ap.add_argument('--show', action='store_true',
                    help='print the effective profile (masked)')
    args = ap.parse_args(argv)

    store_dir = os.path.abspath(os.path.expanduser(args.data_dir))
    session = read_session(store_dir)

    if args.rotate:
        new, backup = rotate(store_dir, legacy_session=session, force=True)
        print(f'rotated: {mask_device_id(new["DeviceId"])}  '
              f'source={new["source"]}')
        print(f'device : {new["deviceName"]} / Android {new["osVersion"]}')
        if backup:
            print(f'backup : {backup}')
        return 0

    profile = load_or_create(store_dir, legacy_session=session)
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
        out.append(('random id is 32 hex',
                    bool(re.fullmatch(r'[0-9a-f]{32}', a['DeviceId'])),
                    a['DeviceId'][:8] + '…'))
        out.append(('two installs differ', a['DeviceId'] != b['DeviceId'],
                    'different'))
        out.append(('profile is stable',
                    load_or_create(os.path.join(root, 'a')) == a, 'reloaded'))
        out.append(('generated is android',
                    a['platform'] == 'android' and a['isRoot'] == '0'
                    and a['cpuModel'] == 'arm64-v8a',
                    f'{a["deviceName"]} / {a["osVersion"]}'))

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

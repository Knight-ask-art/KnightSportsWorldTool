"""Per-account device fingerprint that stays put.

Two failure modes this prevents:

1. **A shared constant.**  The shipped default `DeviceId` is the same string for
   every user of this tool.  If that is what actually reaches the server, every
   account built on it is trivially clusterable.  The default has *already* been
   used to submit on this account, so switching it now would be its own signal —
   hence the rule below.

2. **A drifting id.**  Generating a fresh id per run makes the account look like
   a new phone every day.

Rules
-----
* first sight of an account: keep whatever the client already uses (no change);
* operator passes ``--device-id`` once: remember it and keep it forever;
* never silently rotate.

The file lives in ``jhp_phase1/data/devices/<uid>.json`` (or under
``JHP_DATA_DIR``), one per account.
"""

from __future__ import annotations

import json
import os
import time

FIELDS = ('DeviceId', 'deviceName', 'osVersion', 'cpuModel',
          'physicPixel', 'logicPixel', 'appInstallTime', 'appUpdateTime')


def _base_dir() -> str:
    base = os.environ.get('JHP_DATA_DIR')
    if not base:
        base = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
    return os.path.join(base, 'devices')


def _path(uid) -> str:
    return os.path.join(_base_dir(), '%s.json' % uid)


def get(uid) -> dict:
    try:
        with open(_path(uid), 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def remember(uid, **fields) -> dict:
    """Merge and persist. Only known fingerprint fields are stored."""
    current = get(uid)
    for k, v in fields.items():
        if k in FIELDS and v not in (None, ''):
            current[k] = str(v)
    current.setdefault('first_seen', int(time.time() * 1000))
    current['updated'] = int(time.time() * 1000)
    os.makedirs(_base_dir(), exist_ok=True)
    tmp = _path(uid) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(current, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path(uid))
    return current


def resolve(uid, explicit: str | None = None,
            defaults: dict | None = None) -> dict:
    """Return the device dict to use for this account.

    `explicit` wins (and is remembered).  Otherwise a remembered id is used;
    otherwise `defaults` (the client's built-in values) — which are *not*
    written to disk, so the behaviour of an existing account never changes
    unless the operator asks for it.
    """
    if explicit:
        return remember(uid, DeviceId=explicit)

    saved = get(uid)
    if saved.get('DeviceId'):
        return saved
    return dict(defaults or {})


# --------------------------------------------------------------------------
# self test
# --------------------------------------------------------------------------

def self_test() -> list[tuple[str, bool, str]]:
    import tempfile
    out = []
    old = os.environ.get('JHP_DATA_DIR')
    tmp = tempfile.mkdtemp(prefix='jhp_id_')
    os.environ['JHP_DATA_DIR'] = tmp
    try:
        base = {'DeviceId': 'DEFAULT', 'deviceName': 'Xiaomi 2304FPN6DG'}

        out.append(('no file -> defaults', resolve('u1', defaults=base)['DeviceId']
                    == 'DEFAULT', 'DEFAULT'))

        out.append(('nothing written for defaults', get('u1') == {}, '{}'))

        got = resolve('u1', explicit='abc123')
        out.append(('explicit remembered', got['DeviceId'] == 'abc123', 'abc123'))
        out.append(('reads back', resolve('u1', defaults=base)['DeviceId']
                    == 'abc123', 'abc123'))

        # a second account is independent
        out.append(('per-account isolation',
                    resolve('u2', defaults=base)['DeviceId'] == 'DEFAULT',
                    'DEFAULT'))

        remember('u1', deviceName='NCO-AL00', junk='x')
        out.append(('merge keeps id + filters junk',
                    get('u1')['DeviceId'] == 'abc123'
                    and get('u1')['deviceName'] == 'NCO-AL00'
                    and 'junk' not in get('u1'),
                    str(sorted(get('u1')))))

        out.append(('never rotates', resolve('u1')['DeviceId'] == 'abc123',
                    'stable'))
    finally:
        if old is None:
            os.environ.pop('JHP_DATA_DIR', None)
        else:
            os.environ['JHP_DATA_DIR'] = old
    return out


if __name__ == '__main__':
    ok = True
    for name, passed, detail in self_test():
        print('%-30s %s   %s' % (name, 'PASS' if passed else 'FAIL', detail))
        ok = ok and passed
    print('\nRESULT:', 'PASS' if ok else 'FAIL')
    raise SystemExit(0 if ok else 1)

"""Go-backed "one device, one account" guard.

The policy lives in ``go/hxp-guard`` so the rule is not a few lines of Python
that are trivial to patch out of a release.  A packaged build ships
``runtime/go/hxp-guard.exe``; a source checkout uses
``go/hxp-guard/hxp-guard.exe`` when it has been built.

Flow:
    check  before the network login (state is read, never written)
    bind   after the server accepted the login

``check`` is advisory for the UI; ``bind`` is the enforcement point because it
also refuses a different account if two logins race.  The raw phone number is
never stored: the sidecar keeps a hash plus a masked hint.

Disable with ``JHP_ACCOUNT_GUARD=0`` (debugging only).  Override the binary
with ``JHP_ACCOUNT_GUARD_PATH``.
"""
from __future__ import annotations

import json
import os
import subprocess

MESSAGE = '为防止恶意使用软件，软件请一号一设备绑定'
SUPPORT_MESSAGE = '账号绑定数据校验失败，请联系维护者重置绑定'
UNAVAILABLE_MESSAGE = '账号绑定校验失败，请重启软件后再试'
STATE_NAME = 'account_binding.json'
_EXE = 'hxp-guard.exe' if os.name == 'nt' else 'hxp-guard'
_DISABLED = ('0', 'off', 'false', 'no', 'disabled')
_TIMEOUT = 8.0


def _repo_root() -> str:
    # app/replica/application/account_guard.py -> 好想跑
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(os.path.dirname(os.path.dirname(here)))


def _candidates() -> list[str]:
    out: list[str] = []
    explicit = os.environ.get('JHP_ACCOUNT_GUARD_PATH')
    if explicit:
        out.append(explicit)
    try:
        from ..build_info import app_root, is_compiled
        root = app_root()
        compiled = is_compiled()
    except Exception:  # noqa: BLE001 - build_info is optional at import time
        root = os.path.join(_repo_root(), 'app')
        compiled = False
    out.append(os.path.join(root, 'runtime', 'go', _EXE))
    if not compiled:
        out.append(os.path.join(_repo_root(), 'go', 'hxp-guard', _EXE))
    return out


def find_guard() -> str | None:
    for path in _candidates():
        if path and os.path.isfile(path):
            return path
    return None


def enabled() -> bool:
    return (os.environ.get('JHP_ACCOUNT_GUARD') or '').strip().lower() not in _DISABLED


def _state_path(store_dir: str) -> str:
    return os.path.join(store_dir, STATE_NAME)


def check(account: str, store_dir: str) -> dict:
    """Read-only verdict for the login form."""
    return _call('check', account, store_dir)


def bind(account: str, store_dir: str) -> dict:
    """Claim the device for ``account``; idempotent for the same account."""
    return _call('bind', account, store_dir)


def status(store_dir: str) -> dict:
    """Describe the current binding (support/diagnostics)."""
    return _call('status', '', store_dir)


def attest(nonce: bytes, store_dir: str) -> bytes:
    """Run the numeric ``op --code 17`` device-attestation collector.

    The nonce goes in on stdin (one hex line), never in argv: Windows exposes
    a process command line to every same-user process (WMI/ETW/Process
    Explorer).  Returns the 32-byte opaque blob.  The nonce and blob are never
    logged and never written under ``data/``; the sidecar only *reads*
    ``install_id`` when it exists.
    """
    if not enabled():
        raise RuntimeError('account guard disabled')
    exe = find_guard()
    if not exe:
        raise RuntimeError('account guard unavailable')
    raw = bytes(nonce)
    if not raw:
        raise RuntimeError('empty nonce')
    args = [exe, 'op', '--state', _state_path(store_dir), '--code', '17']
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
    try:
        proc = subprocess.run(
            args, input=raw.hex() + '\n', check=False, capture_output=True,
            text=True, encoding='utf-8', errors='replace', timeout=_TIMEOUT,
            creationflags=flags,
        )
    except (OSError, subprocess.SubprocessError):
        # Do not attach the original exception: TimeoutExpired carries the
        # command line in its string form.
        raise RuntimeError('attest failed') from None

    text = (proc.stdout or '').strip()
    data: dict = {}
    if text:
        try:
            parsed = json.loads(text.splitlines()[-1])
            if isinstance(parsed, dict):
                data = parsed
        except ValueError:
            data = {}
    blob_hex = data.get('blob')
    if proc.returncode != 0 or not data.get('ok') or not isinstance(blob_hex, str):
        raise RuntimeError('attest failed')
    try:
        blob = bytes.fromhex(blob_hex)
    except ValueError:
        raise RuntimeError('attest failed') from None
    if len(blob) != 32:
        raise RuntimeError('attest failed')
    return blob


def _call(command: str, account: str, store_dir: str) -> dict:
    allowed = {'allowed': True, 'blocked': False, 'guard': 'off'}
    if not enabled():
        return allowed
    exe = find_guard()
    if not exe:
        # Source runs and a packaging mistake must not brick every login.
        # The release build always bundles the sidecar; a missing binary is
        # reported by the build, not by refusing customers at the door.
        return allowed

    args = [exe, command, '--state', _state_path(store_dir)]
    if command != 'status':
        args += ['--account', str(account or '')]
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
    try:
        proc = subprocess.run(
            args, check=False, capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=_TIMEOUT, creationflags=flags,
        )
    except (OSError, subprocess.SubprocessError):
        return _blocked('unavailable', UNAVAILABLE_MESSAGE)

    data: dict = {}
    text = (proc.stdout or '').strip()
    if text:
        try:
            parsed = json.loads(text.splitlines()[-1])
            if isinstance(parsed, dict):
                data = parsed
        except ValueError:
            data = {}

    if proc.returncode == 0 and data.get('allowed', True):
        return {
            'allowed': True,
            'blocked': False,
            'guard': 'ok',
            'bound': bool(data.get('bound')),
            'hint': data.get('hint') or '',
        }
    if proc.returncode == 3:
        return _blocked(
            data.get('reason') or 'account_mismatch',
            data.get('message') or MESSAGE,
            hint=data.get('hint') or '',
        )
    # Any other non-zero exit is a corrupt/tampered state or a broken
    # sidecar.  Fail closed: a crash must not become a bypass.
    return _blocked(
        data.get('reason') or 'guard_error',
        data.get('message') or SUPPORT_MESSAGE,
        hint=data.get('hint') or '',
    )


def _blocked(reason: str, message: str, hint: str = '') -> dict:
    return {
        'allowed': False,
        'blocked': True,
        'guard': 'blocked',
        'reason': reason,
        'message': message,
        'hint': hint,
    }

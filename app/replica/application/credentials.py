"""Encrypted local login store for the "remember password" checkbox.

The password must never reach disk as plaintext.  On Windows the JSON payload
is sealed with DPAPI (``CryptProtectData``), so the ciphertext is bound to the
current Windows user and cannot be decrypted by another account or machine.
On other platforms saving fails closed instead of falling back to base64 or a
hard-coded key, which would only look like encryption.

Only the local bridge reads this file; it is never included in the packaged
zip and is not part of the session file (``accounts_emu.json``).
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import tempfile
from ctypes import wintypes

FILENAME = 'saved_login.json'
LEGACY_FILENAME = 'account.json'
_VERSION = 1
_ENTROPY = b'haoxiangpao.local-login.v1'
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class CredentialError(RuntimeError):
    """The platform cannot protect or unprotect the local credential."""


class _DataBlob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD),
                ('pbData', ctypes.POINTER(ctypes.c_char))]


_API = None


def _api():
    """Lazily bind crypt32/kernel32 so importing this module is portable."""
    global _API
    if _API is not None:
        return _API
    if os.name != 'nt':
        raise CredentialError('当前系统不支持 Windows DPAPI 本地加密保存')
    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob), wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(_DataBlob)]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(_DataBlob)]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    _API = (crypt32, kernel32)
    return _API


def _blob(data: bytes):
    # create_string_buffer needs at least one byte; callers never pass empty
    # secrets, but guard anyway so a corrupt file becomes a clean error.
    raw = bytes(data or b'\x00')
    buf = ctypes.create_string_buffer(raw, len(raw))
    return (_DataBlob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))),
            buf)


def _protect(data: bytes, entropy: bytes = _ENTROPY) -> bytes:
    crypt32, kernel32 = _api()
    pin, _in = _blob(data)
    ent, _ent = _blob(entropy)
    pout = _DataBlob()
    if not crypt32.CryptProtectData(
            ctypes.byref(pin), 'haoxiangpao.local-login',
            ctypes.byref(ent), None, None, _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(pout)):
        raise CredentialError('DPAPI 加密失败: %s'
                              % ctypes.WinError(ctypes.get_last_error()))
    try:
        return ctypes.string_at(pout.pbData, pout.cbData)
    finally:
        kernel32.LocalFree(pout.pbData)


def _unprotect(data: bytes, entropy: bytes = _ENTROPY) -> bytes:
    crypt32, kernel32 = _api()
    pin, _in = _blob(data)
    ent, _ent = _blob(entropy)
    pout = _DataBlob()
    desc = wintypes.LPWSTR()
    if not crypt32.CryptUnprotectData(
            ctypes.byref(pin), ctypes.byref(desc), ctypes.byref(ent),
            None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(pout)):
        raise CredentialError('DPAPI 解密失败: %s'
                              % ctypes.WinError(ctypes.get_last_error()))
    try:
        return ctypes.string_at(pout.pbData, pout.cbData)
    finally:
        if desc:
            kernel32.LocalFree(ctypes.cast(desc, wintypes.HLOCAL))
        kernel32.LocalFree(pout.pbData)


def seal(text: str, entropy: bytes = _ENTROPY) -> str:
    """DPAPI-seal UTF-8 ``text``; base64 for a JSON-safe field."""
    raw = _protect(str(text).encode('utf-8'), entropy)
    return base64.b64encode(raw).decode('ascii')


def unseal(value: str, entropy: bytes = _ENTROPY) -> str:
    """Reverse :func:`seal`; raises :class:`CredentialError` on a foreign blob."""
    return _unprotect(base64.b64decode(str(value)), entropy).decode('utf-8')


def _path(store_dir: str) -> str:
    return os.path.join(os.path.abspath(store_dir), FILENAME)


def available() -> bool:
    """True when the current platform can protect a saved password."""
    return os.name == 'nt'


def save(store_dir: str, account: str, password: str) -> None:
    """Encrypt ``account``/``password`` into ``store_dir`` atomically.

    Raises :class:`CredentialError` on an unsupported platform or when DPAPI
    refuses the payload; never writes a plaintext fallback.
    """
    account = str(account or '').strip()
    password = str(password or '')
    if not account or not password:
        raise CredentialError('账号和密码不能为空')
    payload = json.dumps({'account': account, 'password': password},
                         ensure_ascii=False,
                         separators=(',', ':')).encode('utf-8')
    sealed = _protect(payload)
    doc = {
        'version': _VERSION,
        'scheme': 'dpapi',
        'blob': base64.b64encode(sealed).decode('ascii'),
    }

    os.makedirs(store_dir, exist_ok=True)
    path = _path(store_dir)
    fd, tmp = tempfile.mkstemp(prefix='.saved_login-', suffix='.tmp',
                               dir=store_dir)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(doc, f, ensure_ascii=False, separators=(',', ':'))
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    # A stale plaintext file from the old settings panel must not survive a
    # successful encrypted save.
    try:
        os.unlink(os.path.join(os.path.abspath(store_dir), LEGACY_FILENAME))
    except OSError:
        pass


def load(store_dir: str) -> dict | None:
    """Return ``{'account', 'password'}`` or ``None``.

    Missing, corrupt, foreign-user and unsupported-platform cases all return
    ``None``: the login page simply opens without prefilled credentials.
    """
    if not available():
        return None
    try:
        with open(_path(store_dir), encoding='utf-8') as f:
            doc = json.load(f)
        if not isinstance(doc, dict) or doc.get('scheme') != 'dpapi':
            return None
        sealed = base64.b64decode(str(doc.get('blob') or ''), validate=True)
        data = json.loads(_unprotect(sealed).decode('utf-8'))
    except (OSError, ValueError, CredentialError):
        return None
    if not isinstance(data, dict):
        return None
    account = str(data.get('account') or '').strip()
    password = str(data.get('password') or '')
    if not account or not password:
        return None
    return {'account': account, 'password': password}


def clear(store_dir: str) -> bool:
    """Remove the saved credential and any legacy plaintext file."""
    removed = False
    for name in (FILENAME, LEGACY_FILENAME):
        try:
            os.unlink(os.path.join(os.path.abspath(store_dir), name))
            removed = True
        except FileNotFoundError:
            continue
        except OSError as e:
            raise CredentialError('删除本地保存失败: %s' % e)
    return removed


def migrate_legacy(store_dir: str) -> bool:
    """Encrypt an old plaintext ``account.json`` and delete it.

    Older builds' settings panel wrote the account and password in clear text.
    Run this once at startup so an upgrade does not leave that file behind.
    Returns True only when a legacy file was successfully converted.
    """
    path = os.path.join(os.path.abspath(store_dir), LEGACY_FILENAME)
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    account = str(data.get('account') or '').strip()
    password = str(data.get('password') or '')
    if not account or not password:
        return False
    try:
        save(store_dir, account, password)
    except CredentialError:
        return False
    return True

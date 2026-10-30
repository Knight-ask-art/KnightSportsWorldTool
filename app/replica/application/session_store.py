"""At-rest protection for ``accounts_emu.json``.

The session token is the only credential in that file (the password lives in
the separate DPAPI ``saved_login.json`` store).  Seal it with DPAPI bound to
the current Windows user so a copied ``data/`` directory, a backup or a
different local account cannot impersonate the login.

Only ``token`` is encrypted; every other field stays readable JSON so the UI,
diagnostics and manual inspection keep working.  A legacy plaintext ``token``
is still accepted on read and is re-sealed on the next write or at startup
(:func:`migrate`).
"""
from __future__ import annotations

import base64
import json
import os
import tempfile

from . import credentials

#: Distinct DPAPI entropy: a blob sealed for saved_login.json cannot be
#: replayed as a session token.
ENTROPY = b'haoxiangpao.session-token.v1'
SEALED = 'token_enc'


def decode(doc: dict) -> dict:
    """Return a copy with a plaintext ``token`` (decrypting ``token_enc``)."""
    out = dict(doc or {})
    if not out.get('token') and out.get(SEALED):
        try:
            out['token'] = credentials.unseal(out[SEALED], entropy=ENTROPY)
        except Exception:  # noqa: BLE001 - foreign/corrupt blob -> logged out
            out['token'] = ''
    return out


def encode(doc: dict) -> dict:
    """Return a copy with the token sealed and the plaintext field removed."""
    out = dict(doc or {})
    token = str(out.pop('token', '') or '')
    out.pop(SEALED, None)
    if not token:
        return out
    try:
        out[SEALED] = credentials.seal(token, entropy=ENTROPY)
    except Exception:  # noqa: BLE001 - non-Windows / DPAPI unavailable
        # Never lose the login on a platform that cannot seal; the Windows
        # build retries the migration on the next start.
        out['token'] = token
    return out


def read(path: str) -> dict:
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return decode(data) if isinstance(data, dict) else {}


def _write(path: str, doc: dict) -> None:
    folder = os.path.dirname(path) or '.'
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.tmp-', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write(path: str, doc: dict) -> None:
    _write(path, encode(doc))


def migrate(path: str) -> bool:
    """Seal a legacy plaintext token in place.  True when rewritten."""
    try:
        with open(path, encoding='utf-8') as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(raw, dict) or not raw.get('token'):
        return False
    if not credentials.available():
        return False
    sealed = encode(raw)
    if not sealed.get(SEALED) or sealed.get('token'):
        return False
    _write(path, sealed)
    return True

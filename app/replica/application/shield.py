"""Four-layer device credential.

A login first has to produce a 32-byte blob in Go, mix it in Rust, and have
the C++ shield mint and check a 3-second token in **one process instance**.
Python orchestrates the layers and holds the token only long enough for the
immediate ``check``; nothing is written to disk, logged, or put in a
diagnostic bundle.

Enable/disable with ``JHP_SHIELD``:
  - unset / ``auto`` -> enabled; a source checkout without all three
    binaries skips the shield, a frozen build refuses the login;
  - ``0``/``off``/``false``/``no`` -> skip, **source checkout only**.  A
    frozen build ignores the switch: leaving it honoured made the whole gate
    skippable with one environment variable.

``JHP_SHIELD_PATH`` is a developer override and is likewise ignored in a
frozen build; otherwise it could point the sidecar at a stub that always
answers "ok".

The user-facing text is deliberately vague: it never names a layer, a
binary, a path, or a reason.
"""
# ruff: noqa: S110
# Cleanup is intentionally best-effort: errors are folded into the fixed
# message and must never be logged.

from __future__ import annotations

import os
import queue
import struct
import subprocess
import threading

from ..transport import rustcore
from . import account_guard
from . import integrity

SHIELD_MESSAGE = '设备校验未通过，请重启软件后再试'

_EXE = 'hxp-shield.exe'
_DISABLED = ('0', 'off', 'false', 'no', 'disabled')

_MAGIC = 0x53505848          # 'HXPS'
_VERSION = 1
_OP_SEAL = 0x01
_OP_CHECK = 0x02
_MAX_PAYLOAD = 4096
_HEADER = struct.Struct('<IHHI')
_SEAL_REQUEST = 16 + 32 + 32
_SEAL_RESPONSE = 32 + 8
_CHECK_REQUEST = 32 + 8
_CHECK_RESPONSE = 1
_IO_TIMEOUT = 0.5
_WAIT_TIMEOUT = 0.75


class ShieldError(RuntimeError):
    """A layer failed.  The message is intentionally generic."""


def _repo_root() -> str:
    # app/replica/application/shield.py -> 好想跑
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(os.path.dirname(os.path.dirname(here)))


def _candidates() -> list[str]:
    out: list[str] = []
    try:
        from ..build_info import app_root, is_compiled
        root = app_root()
        compiled = is_compiled()
    except Exception:  # noqa: BLE001 - build_info is optional at import time
        root = os.path.join(_repo_root(), 'app')
        compiled = False
    # A path override is a developer aid.  In a frozen build it would let
    # anyone swap in a stub that always answers "ok", so it stays a source
    # checkout feature.
    if not compiled:
        explicit = os.environ.get('JHP_SHIELD_PATH')
        if explicit:
            out.append(explicit)
    out.append(os.path.join(root, 'runtime', 'cpp', _EXE))
    if not compiled:
        out.append(os.path.join(_repo_root(), 'cpp', 'hxp-shield', _EXE))
    return out


def find_shield() -> str | None:
    for path in _candidates():
        if path and os.path.isfile(path):
            return path
    return None


def enabled() -> bool:
    """Whether the four-layer derivation runs.

    A frozen build always runs it.  ``JHP_SHIELD=0`` is a source-checkout
    debugging aid; honouring it in a release made the entire gate skippable
    with one environment variable.
    """
    if _is_frozen():
        return True
    return (os.environ.get('JHP_SHIELD') or '').strip().lower() not in _DISABLED


def selfcheck(exe: str | None = None, timeout: float = 2.0) -> dict:
    """Run the shield's own debugger/image check (``--selfcheck``).

    Diagnostic only.  An attached debugger is routine on a developer machine
    and some endpoint agents probe processes that way, so a failure here must
    never gate a login.  The verdict goes into the support bundle: it tells
    the maintainer whether a reported bug came from a patched copy.
    """
    path = exe or find_shield()
    if not path:
        return {'ok': None, 'skipped': True}
    flags = (getattr(subprocess, 'CREATE_NO_WINDOW', 0)
             if os.name == 'nt' else 0)
    try:
        proc = subprocess.run(
            [path, '--selfcheck'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, timeout=timeout, creationflags=flags,
        )
    except (OSError, subprocess.SubprocessError):
        return {'ok': False, 'reason': 'unavailable'}
    if proc.returncode == 0:
        return {'ok': True}
    # The binary prints only ``selfcheck: debugger`` or ``selfcheck: image``.
    detail = (proc.stderr or b'').decode('utf-8', 'replace').strip()
    reason = detail.split(':', 1)[-1].strip() if ':' in detail else 'unknown'
    return {'ok': False, 'reason': reason[:24] or 'unknown'}


def _is_frozen() -> bool:
    from ..build_info import is_compiled
    return is_compiled()


def _binaries() -> tuple[str | None, str | None, str | None]:
    """(shield, guard, rust) paths; ``None`` means the layer is unavailable."""
    return (
        find_shield(),
        account_guard.find_guard() if account_guard.enabled() else None,
        rustcore.find_core() if rustcore.enabled() else None,
    )


def _blocked(message: str = SHIELD_MESSAGE) -> dict:
    return {'blocked': True, 'message': message}


def _allowed(skipped: bool = False) -> dict:
    if skipped:
        return {'allowed': True, 'skipped': True}
    return {'allowed': True}


def require(store_dir: str) -> dict:
    """Run the derivation or decide that the login must be refused.

    Any exception is swallowed into the fixed message: a traceback here would
    otherwise leak a path or an internal layer name into the UI or a log.
    """
    # A shipped sidecar whose bytes changed is either a damaged install or a
    # patched one; both end in the same repair sentence.  This runs before the
    # JHP_SHIELD switch: that switch skips the derivation for local debugging,
    # it does not make a modified install valid.  Missing files are not tested
    # here -- a quarantine is not tampering, and the layer check below already
    # refuses a frozen build that cannot derive a credential.
    damage = integrity.mismatch_message()
    if damage:
        return _blocked(damage)

    if not enabled():
        return _allowed(skipped=True)

    shield_exe, guard_exe, rust_exe = _binaries()
    complete = bool(shield_exe and guard_exe and rust_exe)
    if not complete:
        if _is_frozen():
            return _blocked()
        # A source checkout without native binaries stays usable for tests
        # and for people running from a clone.
        return _allowed(skipped=True)

    nonce = bytearray(os.urandom(16))
    blob_go = bytearray()
    blob_rust = bytearray()
    try:
        blob_go = bytearray(_go_attest(nonce, store_dir))
        if len(blob_go) != 32:
            raise ShieldError('attest layer failed')
        blob_rust = bytearray(_rust_mix(blob_go, nonce))
        if len(blob_rust) != 32:
            raise ShieldError('mix layer failed')
        if not _cpp_seal_check(shield_exe, nonce, blob_go, blob_rust):
            raise ShieldError('seal layer failed')
    except Exception:  # noqa: BLE001 - must not leak layer details
        return _blocked()
    finally:
        # Best effort: overwrite the disposable inputs in place.  The token
        # itself is zeroed inside _cpp_seal_check.
        _zero(nonce, blob_go, blob_rust)
    return _allowed()


def _go_attest(nonce: bytes, store_dir: str) -> bytes:
    return account_guard.attest(nonce, store_dir)


def _rust_mix(blob: bytes, nonce: bytes) -> bytes:
    core = rustcore.get()
    if core is None:
        raise ShieldError('mix layer unavailable')
    result = core.call('shield_mix', blob=bytes(blob).hex(), nonce=nonce.hex())
    if not isinstance(result, str):
        raise ShieldError('mix layer failed')
    try:
        mixed = bytes.fromhex(result)
    except ValueError as exc:
        raise ShieldError('mix layer failed') from exc
    if len(mixed) != 32:
        raise ShieldError('mix layer failed')
    return mixed


def _frame(op: int, payload) -> bytearray:
    frame = bytearray(_HEADER.pack(_MAGIC, _VERSION, op, len(payload)))
    frame.extend(payload)
    return frame


def _read_exact(pipe, size: int, timeout: float = _IO_TIMEOUT) -> bytearray:
    """Read exactly ``size`` bytes; a blocked pipe must not hang the UI."""
    box: queue.Queue = queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            chunks = bytearray()
            while len(chunks) < size:
                chunk = pipe.read(size - len(chunks))
                if not chunk:
                    raise ShieldError('io layer failed')
                chunks.extend(chunk)
            box.put((True, chunks))
        except BaseException:  # noqa: BLE001 - reported as a generic failure
            try:
                box.put((False, bytearray()))
            except Exception:  # noqa: BLE001
                pass

    thread = threading.Thread(target=_worker, name='hxp-shield-read',
                              daemon=True)
    thread.start()
    try:
        ok, data = box.get(timeout=timeout)
    except queue.Empty:
        raise ShieldError('io timeout') from None
    if not ok:
        raise ShieldError('io layer failed')
    return data


def _read_response(proc, op: int, expected: int) -> bytearray:
    header = _read_exact(proc.stdout, _HEADER.size)
    magic, version, reply_op, length = _HEADER.unpack(header)
    if (magic != _MAGIC or version != _VERSION or reply_op != op
            or length != expected or length > _MAX_PAYLOAD):
        raise ShieldError('frame layer failed')
    return _read_exact(proc.stdout, length)


def _write(proc, data) -> None:
    try:
        proc.stdin.write(data)
        proc.stdin.flush()
    except (OSError, ValueError):
        raise ShieldError('io layer failed') from None


def _zero(*buffers) -> None:
    for buf in buffers:
        if buf is None:
            continue
        for i in range(len(buf)):
            buf[i] = 0


def _cpp_seal_check(exe: str, nonce: bytes, blob_go: bytes,
                    blob_rust: bytes, argv=None) -> bool:
    """Seal and check in one shield process.

    The token returns to Python only for the immediate same-process check; it
    is zeroed in ``finally`` and is never logged or written.  ``check`` only
    accepts a token this process instance minted, so both frames must travel
    on the same stdin/stdout pair.

    ``argv`` exists for tests that drive a fake child process; production
    callers leave it unset and the binary at ``exe`` is used.
    """
    if len(nonce) != 16 or len(blob_go) != 32 or len(blob_rust) != 32:
        raise ShieldError('frame layer failed')

    command = list(argv) if argv is not None else [exe]
    flags = (getattr(subprocess, 'CREATE_NO_WINDOW', 0)
             if os.name == 'nt' else 0)
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
    except OSError:
        raise ShieldError('seal layer unavailable') from None

    seal_payload = bytearray()
    seal_frame = bytearray()
    sealed = bytearray()
    token_view = bytearray()
    expiry = bytearray()
    check_payload = bytearray()
    check_frame = bytearray()
    verdict = bytearray()
    try:
        seal_payload.extend(nonce)
        seal_payload.extend(blob_go)
        seal_payload.extend(blob_rust)
        seal_frame = _frame(_OP_SEAL, seal_payload)
        _write(proc, seal_frame)
        sealed = _read_response(proc, _OP_SEAL, _SEAL_RESPONSE)
        token_view = sealed[:32]
        expiry = sealed[32:40]
        check_payload = token_view + expiry
        check_frame = _frame(_OP_CHECK, check_payload)
        _write(proc, check_frame)
        verdict = _read_response(proc, _OP_CHECK, _CHECK_RESPONSE)
        return verdict == b'\x01'
    finally:
        _zero(seal_payload, seal_frame, sealed, token_view, expiry,
              check_payload, check_frame, verdict)
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=_WAIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=_WAIT_TIMEOUT)
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass

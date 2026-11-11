"""Optional native core (Rust) sidecar.

The packaged build ships ``runtime/rust/hxp-core.exe``.  It owns the NetSecKit
envelope and the signing primitives, so those constants and algorithms are not
plain Python in the release.  A source checkout without a built binary keeps
using the existing Python implementation.

Enable/disable with ``JHP_RUST_CORE``:
  - unset / ``auto``  -> use the binary when it exists
  - ``0``/``off``     -> always use Python
  - ``1``/``on``      -> same as auto, but a missing binary is logged
"""
from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import threading

_EXE = 'hxp-core.exe' if os.name == 'nt' else 'hxp-core'


class RustCoreError(RuntimeError):
    """The native core ran but rejected the request."""


class RustCoreUnavailable(RustCoreError):
    """The sidecar is missing, crashed, or did not answer in time."""


class RustCoreRequired(RustCoreError):
    """JHP_RUST_CORE=1 was requested but the binary is missing."""


def _repo_root() -> str:
    # app/replica/transport/rustcore.py -> 好想跑
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(os.path.dirname(os.path.dirname(here)))


def _candidates() -> list[str]:
    out: list[str] = []
    explicit = os.environ.get('JHP_RUST_CORE_PATH')
    if explicit:
        out.append(explicit)
    try:
        from ..build_info import app_root, is_compiled
        root = app_root()
        compiled = is_compiled()
    except Exception:  # noqa: BLE001 - build_info is optional at import time
        root = os.path.join(_repo_root(), 'app')
        compiled = False
    out.append(os.path.join(root, 'runtime', 'rust', _EXE))
    if not compiled:
        out.append(os.path.join(_repo_root(), 'rust', 'hxp-core',
                                'target', 'release', _EXE))
    return out


def find_core() -> str | None:
    for path in _candidates():
        if path and os.path.isfile(path):
            return path
    return None


def enabled() -> bool:
    flag = (os.environ.get('JHP_RUST_CORE') or '').strip().lower()
    return flag not in ('0', 'off', 'false', 'no', 'disabled')


class _Core:
    def __init__(self, exe: str, timeout: float = 8.0):
        self.exe = exe
        self.timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._id = 0

    def _spawn(self) -> None:
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
        self._proc = subprocess.Popen(
            [self.exe],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding='utf-8',
            bufsize=1,
            creationflags=flags,
        )
        self._queue = queue.Queue()
        reader = threading.Thread(target=self._read, name='hxp-core', daemon=True)
        reader.start()

    def _read(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            try:
                self._queue.put(json.loads(line))
            except Exception:  # noqa: BLE001 - a broken reply must not hang the UI
                self._queue.put({'ok': False, 'error': 'bad sidecar reply'})

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.terminate()
            proc.wait(timeout=1)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def call(self, op: str, **args):
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._spawn()
            self._id += 1
            req_id = self._id
            line = json.dumps({'id': req_id, 'op': op, 'args': args},
                              ensure_ascii=False)
            try:
                assert self._proc is not None and self._proc.stdin is not None
                self._proc.stdin.write(line + '\n')
                self._proc.stdin.flush()
            except Exception as exc:  # noqa: BLE001
                self.close()
                raise RustCoreUnavailable('native core write failed: %s' % exc)
            try:
                resp = self._queue.get(timeout=self.timeout)
            except queue.Empty:
                self.close()
                raise RustCoreUnavailable('native core timed out')
            if not isinstance(resp, dict) or not resp.get('ok'):
                raise RustCoreError(
                    (resp or {}).get('error') if isinstance(resp, dict)
                    else 'native core error')
            return resp.get('data')


_core: _Core | None = None
_probed = False
_lock = threading.Lock()


def get() -> _Core | None:
    """Return the shared core, or None when Python should handle the call."""
    global _core, _probed
    if not enabled():
        return None
    with _lock:
        if _core is not None:
            return _core
        if _probed:
            return None
        _probed = True
        exe = find_core()
        if not exe:
            if (os.environ.get('JHP_RUST_CORE') or '').strip().lower() in (
                    '1', 'on', 'true', 'yes'):
                raise RustCoreRequired(
                    'JHP_RUST_CORE requested but %s is missing' % _EXE)
            return None
        core = _Core(exe)
        try:
            core.call('ping')
        except Exception:  # noqa: BLE001 - a broken binary must not block the app
            core.close()
            return None
        _core = core
        atexit.register(core.close)
        return _core


def status() -> dict:
    return {'enabled': enabled(), 'path': find_core(),
            'active': _core is not None}

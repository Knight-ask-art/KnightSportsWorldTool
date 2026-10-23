"""Persistent unidbg oracle sidecar manager.

Spawns ONE java process running OracleFlow (persistent oracle: session state!)
and serves many crypto calls over its stdin/stdout protocol:
    ENC:<base64(plaintext)>   -> OUT:<w3270 envelope {d,h,k,p}>   (body encryption)
    W30F:<base64(plaintext)>  -> OUT:<{"d":...}>                  (header encryption)
    DEC:<response string>     -> OUT:...                          (response, currently empty)
    QUIT                      -> exit

Design notes (from archive/unidbg-era/STATUS.md):
- Session state matters: ONE process serves many calls (matches real app).
- The java process needs ~seconds to boot (JNI_OnLoad + warmups); keep it alive
  for the whole app lifetime and restart it transparently if it dies.
"""
import atexit
import base64
import os
import subprocess
import sys
import threading

DEV_UNIDBG_DIR = (r'D:\C-Drive-Archive\Downloads\聚合跑最新版（点进去下载）'
                  r'\好想跑\apk_work\unidbg')
DEV_JAVA = r'D:\Codex\tools\jdk-21\jdk-21.0.11+10\bin\java.exe'


def _runtime_base() -> str:
    """Where the app lives: frozen exe dir, or the project dir in dev."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))


def find_unidbg_dir() -> str:
    base = _runtime_base()
    cands = [
        os.environ.get('JHP_UNIDBG'),
        os.path.join(base, 'runtime', 'unidbg'),
        os.path.join(base, 'apk_work', 'unidbg'),
        os.path.join(os.path.dirname(base), 'apk_work', 'unidbg'),
        DEV_UNIDBG_DIR,
    ]
    for c in cands:
        if c and os.path.isdir(os.path.join(c, 'out')):
            return c
    return DEV_UNIDBG_DIR


def find_java() -> str:
    cands = [
        os.environ.get('JHP_JAVA'),
        os.path.join(_runtime_base(), 'runtime', 'jre', 'bin', 'java.exe'),
        os.path.join(_runtime_base(), 'runtime', 'jdk', 'bin', 'java.exe'),
        DEV_JAVA,
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return DEV_JAVA


DEFAULT_UNIDBG_DIR = find_unidbg_dir()
DEFAULT_JAVA = find_java()

JARS = [
    'unidbg-api-0.9.9.jar', 'unidbg-android-0.9.9.jar', 'unidbg-dynarmic-0.9.9.jar',
    'unidbg-unicorn2-0.9.9.jar', 'unicorn-1.0.15.jar', 'capstone-3.1.8.jar',
    'jna-5.10.0.jar', 'guava-27.1-android.jar', 'slf4j-api-2.0.17.jar',
    'slf4j-simple-2.0.17.jar', 'commons-io-2.21.0.jar', 'commons-codec-1.21.0.jar',
    'dexlib2-2.5.2.jar', 'util-2.5.2.jar', 'antlr-runtime-3.5.2.jar', 'antlr-3.5.2.jar',
    'stringtemplate-3.2.1.jar', 'jcommander-1.64.jar', 'baksmali-2.5.2.jar',
    'smali-2.5.2.jar', 'fastjson-1.2.83.jar', 'native-lib-loader-2.3.5.jar',
    'commons-collections4-4.5.0.jar', 'apk-parser-2.6.10.jar', 'keystone-0.9.7.jar',
    'demumble-1.0.4.jar',
]


class SidecarError(RuntimeError):
    pass


class UnidbgSidecar:
    def __init__(self, unidbg_dir: str = DEFAULT_UNIDBG_DIR, java: str = DEFAULT_JAVA,
                 timeout: float = 120.0):
        self.unidbg_dir = unidbg_dir
        self.java = java
        self.timeout = timeout
        self._proc = None
        self._lock = threading.Lock()

    # ---- process management ----

    def _classpath(self) -> str:
        cp = os.path.join(self.unidbg_dir, 'out')
        for j in JARS:
            cp += ';' + os.path.join(self.unidbg_dir, 'libs', j)
        return cp

    def _spawn(self):
        cp = self._classpath()
        self._proc = subprocess.Popen(
            [self.java, '-cp', cp, 'OracleFlow', 'oracle.apk'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=self.unidbg_dir, text=True, encoding='utf-8', errors='replace',
            bufsize=1)
        # wait for READY (skips the boot log lines)
        deadline = __import__('time').time() + self.timeout
        while True:
            line = self._proc.stdout.readline()
            if not line:
                raise SidecarError('sidecar died during boot')
            if line.strip() == 'READY':
                return
            if __import__('time').time() > deadline:
                raise SidecarError('sidecar boot timeout')

    def start(self):
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._spawn()

    def stop(self):
        with self._lock:
            p = self._proc
            self._proc = None
        if p is not None and p.poll() is None:
            try:
                p.stdin.write('QUIT\n')
                p.stdin.flush()
                p.wait(timeout=10)
            except Exception:
                p.kill()

    # ---- crypto calls ----

    # debug lines that OracleFlow prints to stdout with an "OUT:" prefix while
    # probes are installed; these are NOT the command result.
    _DEBUG_PREFIXES = ('OUT:GATEFRAME', 'OUT:<null>', 'OUT:DEC1-ERR')

    def _call(self, cmd: str) -> str:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._spawn()
            try:
                self._proc.stdin.write(cmd + '\n')
                self._proc.stdin.flush()
                deadline = __import__('time').time() + self.timeout
                while True:
                    line = self._proc.stdout.readline()
                    if not line:
                        raise SidecarError('sidecar died mid-call')
                    if line.startswith('OUT:'):
                        # skip probe/debug output, keep waiting for the real result
                        if line.startswith(self._DEBUG_PREFIXES):
                            if __import__('time').time() > deadline:
                                raise SidecarError('sidecar call timeout')
                            continue
                        return line[4:].rstrip('\n')
                    # hook/boot noise lines are printed to stdout too; ignore them
                    if __import__('time').time() > deadline:
                        raise SidecarError('sidecar call timeout')
            except (SidecarError, OSError, ValueError):
                # kill and let the next call respawn a fresh session
                try:
                    self._proc.kill()
                except Exception:
                    pass
                self._proc = None
                raise

    def w3270(self, plaintext: str) -> str:
        """Encrypt a JSON body -> {d,h,k,p} envelope string (as the app sends)."""
        b64 = base64.b64encode(plaintext.encode('utf-8')).decode()
        return self._call('ENC:' + b64)

    def w30f(self, plaintext: str) -> str:
        """Encrypt a headers JSON -> {"d":...} string (headerSign value)."""
        b64 = base64.b64encode(plaintext.encode('utf-8')).decode()
        return self._call('W30F:' + b64)

    def w3270_sig(self, plaintext: str) -> str:
        """headerSign the way the APP does it: w3270, not w30f.

        Captured from com.netease.NetSecKit in the app -- NeteaseManager only
        uses mode 0, so headerSign is the same envelope as the request body.
        """
        b64 = base64.b64encode(plaintext.encode('utf-8')).decode()
        return self._call('ENCSIG:' + b64)

    def dec(self, response: str) -> str:
        # OracleFlow exposes the genuine w1228 decrypt entry as DEC1:
        return self._call('DEC1:' + response)

    def session_key(self) -> bytes:
        """The per-session AES-128 key NetSecKit derives (16 printable-ASCII
        bytes). Captured at the cipher's key-consuming vtable entry, so it
        matches every envelope produced by THIS session. Pure-Python decoding
        (replica.nskcodec) needs it; see archive/unidbg-era/FRIDA_NEXT.md rounds 24-30."""
        raw = self._call('KEY').strip()
        if len(raw) != 32 or any(c not in '0123456789abcdef' for c in raw.lower()):
            raise SidecarError('unexpected KEY response: %r' % raw[:40])
        return bytes.fromhex(raw)

    def encrypt_and_key(self, plaintext: str) -> tuple[str, bytes]:
        """Encrypt and return (envelope, key) so the caller never has to guess
        whether the key it holds is the one that produced this envelope."""
        env = self.w3270(plaintext)
        return env, self.session_key()


_INSTANCE: UnidbgSidecar | None = None


def get_sidecar() -> UnidbgSidecar:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = UnidbgSidecar()
        atexit.register(_INSTANCE.stop)
    return _INSTANCE

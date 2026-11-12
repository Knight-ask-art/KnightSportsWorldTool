"""Local application server: static frontend + JSON bridge API.

1. serves the frontend from ``<app>/frontend``,
2. exposes :class:`ReplicaApi` methods as ``POST /bridge/<method>``,
3. hosts the compatibility mock servers in-process.

Usage:  python run.py [--port 65520] [--no-browser]
"""

import inspect
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .. import build_info
from ..build_info import app_root
from ..compat.mocks.mock_65530 import MockBackendState, serve as serve_be
from ..compat.mocks.mock_iydsj import MockIydsjState, serve as serve_iydsj
from ..domain import crypto as C
from ..application import preferences as P
from .bridge_api import ReplicaApi


def _app_dir() -> str:
    # Nuitka/PyInstaller have no reliable package __file__; both place the
    # frontend next to the executable in a standalone build.
    return app_root()


APP_DIR = _app_dir()
FRONTEND_DIR = os.path.join(APP_DIR, 'frontend')

#: Cap on a bridge request body. The server only listens on 127.0.0.1, but an
#: unbounded read is still a one-line denial of service.
MAX_BODY = 1 << 20
#: Elevation templates are local GPX/JSON files and are often larger than a
#: normal bridge payload; they are parsed once and never stored.  JSON escaping
#: of newlines can roughly double the file, hence the headroom.
MAX_TEMPLATE_BODY = 20 << 20


def _default_store_dir() -> str:
    """Honour JHP_DATA_DIR; a compiled exe keeps data next to the exe."""
    env = os.environ.get('JHP_DATA_DIR')
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(app_root(), 'data')


STORE_DIR = _default_store_dir()
API: ReplicaApi | None = None

MIME = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript',
        '.css': 'text/css', '.json': 'application/json',
        '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
        '.webp': 'image/webp', '.ico': 'image/x-icon', '.svg': 'image/svg+xml',
        # Fonts used to be octet-stream; browsers still loaded them, but a
        # correct type keeps preload/nosniff behaviour predictable.
        '.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf'}


def login_cover_css(store_dir: str | None = None) -> str:
    """First-paint CSS for the operator's custom login cover.

    index.html links this before the body, so a custom cover is already in
    place when the login gate is first painted.  Without it the packaged
    login-bg.jpg shows for a frame under ``?boot=0`` / reduced motion.
    """
    store = store_dir or STORE_DIR
    img = os.path.join(store, 'login-cover.img')
    if not os.path.isfile(img):
        return '/* no custom login cover */\n'
    meta = {}
    try:
        with open(os.path.join(store, 'login-cover.json'),
                  encoding='utf-8') as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            meta = loaded
    except (OSError, ValueError):
        pass
    stamp = str(meta.get('updated_at') or int(os.path.getmtime(img)))
    if not stamp.isdigit():
        stamp = str(int(os.path.getmtime(img)))
    return '#loginGate{background-image:url("/login-cover?t=%s")}\n' % stamp

def theme_boot_js(store_dir: str | None = None) -> str:
    """First-paint skin script.

    The saved skin lives in ``data/ui_state.json`` because localStorage is
    scoped to the random localhost port.  This tiny script is linked before
    the stylesheets, so the page never flashes the default paper skin before
    the bridge call finishes.
    """
    store = store_dir or STORE_DIR
    skin = P.DEFAULT_SKIN
    loaded = False
    try:
        with open(os.path.join(store, 'ui_state.json'),
                  encoding='utf-8') as fh:
            data = json.load(fh)
        if isinstance(data, dict) and 'skin' in data:
            skin = P.normalize_skin(data.get('skin'))
            loaded = True
    except (OSError, ValueError):
        pass
    source = ("r.setAttribute('data-skin-source','server');"
              if loaded else '')
    return ("(function(){try{var r=document.documentElement;"
            "r.setAttribute('data-skin',%s);%s"
            "}catch(e){}})();\n" % (json.dumps(skin), source))


#: The bridge is loopback-only.  A Host/Origin from any other name is a
#: DNS-rebinding attempt, even if the socket happened to accept it.
LOOPBACK_HOSTS = {'127.0.0.1', 'localhost', '::1'}


def static_file(path: str) -> str | None:
    """Resolve a URL path to a file under FRONTEND_DIR, or None.

    realpath + trailing-separator check: a plain `str.startswith(FRONTEND_DIR)`
    also accepts a sibling directory called `<...>/frontendX`.
    """
    rel = os.path.normpath(path.lstrip('/'))
    root = os.path.realpath(FRONTEND_DIR)
    full = os.path.realpath(os.path.join(root, rel))
    if not (full == root or full.startswith(root + os.sep)):
        return None
    return full if os.path.isfile(full) else None


class Handler(BaseHTTPRequestHandler):
    server_version = 'JhpReplica/1.0'

    def log_message(self, fmt, *a):
        pass

    def _send(self, body: bytes, ctype: str, status: int = 200):
        try:
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "frame-ancestors 'none'")
            self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:
            # WinError 10053/10054: the WebView closed or navigated while the
            # response was in flight.  That is a client disconnect, not a
            # server error; do not print a traceback for it.
            return

    def _json(self, obj: dict, status: int = 200):
        self._send(json.dumps(obj, ensure_ascii=False).encode(),
                   'application/json', status)

    def _drain_request_body(self) -> None:
        """Consume a small rejected body so Windows does not reset the socket.

        Returning 403/415/404 before reading the request body can make the
        client see WinError 10053 while it is still sending; draining a bounded
        body keeps the error response readable.
        """
        try:
            n = int(self.headers.get('Content-Length', '') or 0)
        except (TypeError, ValueError):
            return
        if 0 < n <= MAX_BODY:
            try:
                self.rfile.read(n)
            except OSError:
                pass

    def _bridge_origin_ok(self) -> bool:
        """Reject cross-site browser POSTs and DNS-rebinding hosts.

        The bridge has no authentication because it is loopback-only; that is
        only safe if a random web page cannot make the browser POST here.  A
        JSON content type forces a CORS preflight for cross-origin requests,
        and this check rejects the preflight/Origin as well.  Requests from
        scripts/curl (no Origin) are still allowed.
        """
        raw_host = (self.headers.get('Host') or '').strip()
        if raw_host:
            try:
                host = urlparse('//' + raw_host).hostname
            except ValueError:
                return False
            if host not in LOOPBACK_HOSTS:
                return False
        origin = (self.headers.get('Origin') or '').strip()
        if origin:
            try:
                parsed = urlparse(origin)
            except ValueError:
                return False
            if parsed.scheme not in ('http', 'https'):
                return False
            if parsed.hostname not in LOOPBACK_HOSTS:
                return False
            try:
                port = parsed.port
                local_port = int(self.server.server_address[1])
            except (TypeError, ValueError):
                return False
            if port is not None and port != local_port:
                return False
        site = (self.headers.get('Sec-Fetch-Site') or '').strip().lower()
        if site and site not in ('same-origin', 'none'):
            return False
        return True

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path in ('/', '/index.html'):
            path = '/index.html'
        if path.startswith('/bridge/'):
            return self._json({'code': 500, 'message': 'use POST', 'data': None})
        if path in ('/login-cover', '/login-cover.css', '/theme-boot.js'):
            if not self._bridge_origin_ok():
                return self._json({'code': 403,
                                   'message': 'cross-origin rejected',
                                   'data': None}, 403)
        if path == '/login-cover':
            return self._serve_login_cover()
        if path == '/login-cover.css':
            return self._serve_login_cover_css()
        if path == '/theme-boot.js':
            store = getattr(API, 'store_dir', None) or STORE_DIR
            return self._send(theme_boot_js(store).encode('utf-8'),
                              'text/javascript; charset=utf-8')
        # realpath + separator check: `str.startswith(FRONTEND_DIR)` also
        # accepts a sibling like `<...>/frontendX/secret`.
        full = static_file(path)
        if full is None:
            return self._json({'code': 404, 'message': 'not found',
                               'data': None}, 404)
        with open(full, 'rb') as f:
            data = f.read()
        ext = os.path.splitext(full)[1].lower()
        self._send(data, MIME.get(ext, 'application/octet-stream'))

    def _serve_login_cover(self):
        """Serve the operator's custom login cover from ``data/``.

        It deliberately lives outside ``frontend/``: that directory is replaced
        wholesale by the updater, while ``data/`` survives an upgrade.
        """
        store = getattr(API, 'store_dir', None) or STORE_DIR
        img = os.path.join(store, 'login-cover.img')
        if not os.path.isfile(img):
            return self._json({'code': 404, 'message': 'no custom cover',
                               'data': None}, 404)
        meta = {}
        try:
            with open(os.path.join(store, 'login-cover.json'),
                      encoding='utf-8') as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                meta = loaded
        except (OSError, ValueError):
            pass
        with open(img, 'rb') as fh:
            data = fh.read()
        self._send(data, str(meta.get('mime') or 'image/jpeg'))

    def _serve_login_cover_css(self):
        store = getattr(API, 'store_dir', None) or STORE_DIR
        self._send(login_cover_css(store).encode('utf-8'),
                   'text/css; charset=utf-8')

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if not path.startswith('/bridge/'):
            self._drain_request_body()
            return self._json({'code': 404, 'message': 'not found',
                               'data': None}, 404)
        name = path[len('/bridge/'):]
        # Only public API methods.  getattr also resolves dunders and private
        # helpers (`__init__`, `__class__`, `_post`), which is not an API.
        if not name or name.startswith('_') or not name.isidentifier():
            self._drain_request_body()
            return self._json({'code': 404,
                               'message': f'unknown api {name}', 'data': None},
                              404)

        if not self._bridge_origin_ok():
            self._drain_request_body()
            return self._json({'code': 403, 'message': 'cross-origin rejected',
                               'data': None}, 403)

        # A browser cross-site "simple" request cannot set this header; any
        # request without it is either our own UI, a local script, or an
        # attempt to smuggle a form body into json.loads.
        ctype = (self.headers.get('Content-Type') or '').split(';', 1)[0]
        if ctype.strip().lower() != 'application/json':
            self._drain_request_body()
            return self._json({'code': 415,
                               'message': 'Content-Type must be application/json',
                               'data': None}, 415)
        if (self.headers.get('Transfer-Encoding') or '').strip():
            self._drain_request_body()
            return self._json({'code': 400, 'message': 'chunked body rejected',
                               'data': None}, 400)
        try:
            n = int(self.headers.get('Content-Length', ''))
        except (TypeError, ValueError):
            return self._json({'code': 400, 'message': 'missing Content-Length',
                               'data': None}, 400)
        if n <= 0:
            return self._json({'code': 400, 'message': 'empty body',
                               'data': None}, 400)
        if n > (MAX_TEMPLATE_BODY if name in ('real_altitude_template',
                                              'real_set_login_cover')
                else MAX_BODY):
            return self._json({'code': 413, 'message': 'body too large',
                               'data': None}, 413)
        raw = self.rfile.read(n)
        if len(raw) != n:
            return self._json({'code': 400, 'message': 'truncated body',
                               'data': None}, 400)
        try:
            payload = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            return self._json({'code': 400, 'message': 'invalid JSON',
                               'data': None}, 400)
        if not isinstance(payload, dict):
            return self._json({'code': 400, 'message': 'JSON body must be an object',
                               'data': None}, 400)

        args = payload.get('args', [])
        kwargs = payload.get('kwargs', {})
        if not isinstance(args, (dict, list)):
            return self._json({'code': 400, 'message': 'args must be an object or array',
                               'data': None}, 400)
        if not isinstance(kwargs, dict):
            return self._json({'code': 400, 'message': 'kwargs must be an object',
                               'data': None}, 400)
        if isinstance(args, dict) and kwargs:
            return self._json({'code': 400,
                               'message': 'use one of args/kwargs, not both',
                               'data': None}, 400)

        fn = getattr(API, name, None)
        if not callable(fn):
            return self._json({'code': 404,
                               'message': f'unknown api {name}', 'data': None},
                              404)

        # Bind the signature BEFORE calling.  A TypeError raised inside a
        # method is an internal error, not a caller mistake; conflating them
        # used to make malformed bridge requests look like a 500.
        try:
            signature = inspect.signature(fn)
            if isinstance(args, dict):
                signature.bind(**args)
            else:
                signature.bind(*args, **kwargs)
        except (TypeError, ValueError) as e:
            return self._json({'code': 400, 'message': f'bad args: {e}',
                               'data': None}, 400)
        try:
            if isinstance(args, dict):
                result = fn(**args)
            else:
                result = fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            # A submit that raised somewhere we did not anticipate may already
            # have reserved a receipt or reached the server.  Mark it unknown
            # so the UI keeps the button locked instead of inviting a retry.
            data = {'failure_kind': 'unknown'} if name == 'real_submit_run' else None
            return self._json({'code': 500, 'message': f'error: {e}',
                               'data': data})
        if not isinstance(result, dict) or 'code' not in result:
            result = {'code': 200, 'message': 'success', 'data': result}
        self._json(result)


def build_api(store_dir: str | None = None) -> ReplicaApi:
    # The bridge store and every auxiliary module (rules, limits, receipts,
    # points cache) must agree on one data directory.  Frozen builds used to
    # write those to the read-only package directory while the session went
    # next to the exe.
    store_dir = os.path.abspath(os.path.expanduser(
        store_dir or _default_store_dir()))
    os.environ['JHP_DATA_DIR'] = store_dir
    os.environ.setdefault('JHP_POINTS_CACHE',
                          os.path.join(store_dir, 'points_cache.json'))
    priv, pub = C.ensure_keypair(os.path.join(store_dir, 'mock_rsa'))
    istate = MockIydsjState(priv, pub)
    isrv = serve_iydsj(istate)
    bstate = MockBackendState()
    bsrv = serve_be(bstate)
    return ReplicaApi(f'http://127.0.0.1:{isrv.server_port}',
                      f'http://127.0.0.1:{bsrv.server_port}', pub, store_dir)


def _write_boot_ok() -> None:
    raw_path = (os.environ.get('JHP_BOOT_OK_PATH') or '').strip()
    if not raw_path:
        return
    tmp_path = ''
    try:
        path = os.path.abspath(os.path.expanduser(raw_path))
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        payload = {
            'version': build_info.VERSION,
            'pid': os.getpid(),
            'at': int(time.time() * 1000),
        }
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        tmp_path = f'{path}.{os.getpid()}.tmp'
        with open(tmp_path, 'wb') as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:  # noqa: BLE001 - boot marker must never break startup
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def serve(port: int = 65520, store_dir: str | None = None,
          quiet: bool = False) -> ThreadingHTTPServer:
    global API
    API = build_api(store_dir)
    # Resume a previously enabled token keeper.  This is probe-only after a
    # restart: the password is never persisted, so auto-relogin stays idle
    # until the user enters it again.
    try:
        API.real_keepalive_resume()
    except Exception:  # noqa: BLE001 - keepalive must not block the UI
        pass
    srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    if not quiet:
        print(f'replica up: http://127.0.0.1:{port}/')
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    _write_boot_ok()
    return srv


# --------------------------------------------------------------------------
# self test
# --------------------------------------------------------------------------

def self_test() -> list[tuple[str, bool, str]]:
    out = []
    out.append(('index resolves', static_file('/index.html') is not None,
                'ok'))
    out.append(('nested asset resolves', static_file('/vendor/leaflet.js')
                is not None, 'ok'))
    out.append(('parent traversal blocked',
                static_file('/../replica/server.py') is None, 'None'))
    out.append(('sibling prefix blocked',
                static_file('/../frontendX/secret') is None, 'None'))
    out.append(('absolute escape blocked',
                static_file('/../../../../etc/passwd') is None, 'None'))
    out.append(('missing file -> None', static_file('/nope.js') is None, 'None'))
    out.append(('directory -> None', static_file('/vendor') is None, 'None'))
    return out


if __name__ == '__main__':
    ok = True
    for name, passed, detail in self_test():
        print('%-34s %s   %s' % (name, 'PASS' if passed else 'FAIL', detail))
        ok = ok and passed
    print('\nRESULT:', 'PASS' if ok else 'FAIL')
    raise SystemExit(0 if ok else 1)

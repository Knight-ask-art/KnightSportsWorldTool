"""Runnable replica server (Phase 2).

Mirrors the new product's BackgroundWebServer idea: one local HTTP server that
1. serves the replica frontend (static files in ../frontend/),
2. exposes ReplicaApi's public methods as POST /bridge/<method>,
3. embeds both mock servers (iydsj school-server + 65530 backend) in-process.

Usage:  python run.py [--port 65520] [--no-browser]
Then open http://127.0.0.1:65520/ (matches the product's Debug URL shape).
Stdlib + requests + pycryptodome only.
"""
import inspect
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

try:                                     # python -m jhp_phase1.replica.server
    from . import crypto as C
    from .mock_65530 import MockBackendState, serve as serve_be
    from .mock_iydsj import MockIydsjState, serve as serve_iydsj
    from .replica_api import ReplicaApi
except ImportError:                      # python -m replica.server (cwd=jhp_phase1)
    from replica import crypto as C
    from replica.mock_65530 import MockBackendState, serve as serve_be
    from replica.mock_iydsj import MockIydsjState, serve as serve_iydsj
    from replica.replica_api import ReplicaApi

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(BASE_DIR, 'frontend')

#: Cap on a bridge request body. The server only listens on 127.0.0.1, but an
#: unbounded read is still a one-line denial of service.
MAX_BODY = 1 << 20


def _default_store_dir() -> str:
    """Honour JHP_DATA_DIR; frozen exe keeps data next to the exe."""
    env = os.environ.get('JHP_DATA_DIR')
    if env:
        return os.path.abspath(os.path.expanduser(env))
    if getattr(sys, 'frozen', False):
        return os.path.join(os.path.dirname(sys.executable), 'data')
    return os.path.join(BASE_DIR, 'data')


STORE_DIR = _default_store_dir()
API: ReplicaApi | None = None

MIME = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript',
        '.css': 'text/css', '.json': 'application/json',
        '.png': 'image/png', '.svg': 'image/svg+xml'}

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
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "frame-ancestors 'none'")
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        self.wfile.write(body)

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
        if n > MAX_BODY:
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
            return self._json({'code': 500, 'message': f'error: {e}',
                               'data': None})
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

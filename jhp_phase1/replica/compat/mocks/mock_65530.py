"""Mock product backend (sportapp.top:65530) + local VM helpers.

Endpoint set observed in the 00201.0.0 binary (marshal `u'/api/...'` consts):
addProxyAgent / adminLogin / admini_changePassword / admini_getAccountBalance /
checkForUpdates / checkRechargeStatus / checkSecretKeyStatus / deleteProxyAgent /
edclass / getProxyAgents / getProxyRechargeAddress / getProxyRechargeInfo /
get_users_ordrs / queryProxyUser / redeemSecretKey / transfer_mileage /
updateProxyAgentStatus / updateProxyRechargeAddress
Local (VM) endpoints from binary: /update_geetoken, /get_ca.

Wire shapes ({code,message,data}, pagination {list,total,page,pageSize,
totalPages}) mirror the React frontend's expectations; arg counts mirror the
bridge wrappers (see archive/reference/NEW_API_LIST.md). This mock defines the replica's own
backend contract — the original server is unreachable.
Stdlib only.
"""
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse


class MockBackendState:
    def __init__(self):
        self.lock = threading.Lock()
        self.mileage: dict[str, float] = {}
        self.orders: list[dict] = []
        self.agents: list[dict] = []
        self.geetokens: list[dict] = []


def _resp(handler: BaseHTTPRequestHandler, obj: dict, status: int = 200):
    body = json.dumps(obj, ensure_ascii=False).encode()
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def make_handler(state: MockBackendState):
    class H(BaseHTTPRequestHandler):
        server_version = 'MockBackend65530/1.0'

        def log_message(self, *a):
            pass

        def _read_json(self) -> dict:
            try:
                n = int(self.headers.get('Content-Length', 0))
            except ValueError:
                n = 0
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode())
            except Exception:
                return {}

        def _ok(self, data=None, message='success'):
            _resp(self, {'code': 200, 'message': message, 'data': data})

        def _fail(self, message='failed', code=500):
            _resp(self, {'code': code, 'message': message, 'data': None})

        def do_GET(self):
            if urlparse(self.path).path == '/get_ca':
                pem = b'-----BEGIN CERTIFICATE-----\nMOCK\n-----END CERTIFICATE-----\n'
                self.send_response(200)
                self.send_header('Content-Type', 'application/x-x509-ca-cert')
                self.send_header('Content-Length', str(len(pem)))
                self.end_headers()
                self.wfile.write(pem)
                return
            self._route(self._read_json())

        def do_POST(self):
            self._route(self._read_json())

        def _route(self, body: dict):
            path = urlparse(self.path).path
            name = path.rsplit('/', 1)[-1]
            with state.lock:
                try:
                    fn = getattr(self, '_api_' + name, None)
                    if fn is None:
                        return self._fail(f'unknown endpoint {path}', code=404)
                    return fn(body)
                except Exception as e:  # noqa: BLE001
                    return self._fail(f'mock error: {e}')

        # -- auth / account ------------------------------------------------
        def _api_adminLogin(self, b: dict):
            if b.get('password'):
                return self._ok({'admin': b.get('account', ''), 'isLoggedIn': True})
            return self._fail('bad credentials')

        def _api_admini_changePassword(self, b: dict):
            return self._ok(True, message='password updated')

        def _api_admini_getAccountBalance(self, b: dict):
            return self._ok({'balance': state.mileage.get(str(b.get('id', '')), 0.0)})

        # -- updates --------------------------------------------------------
        def _api_checkForUpdates(self, b: dict):
            return self._ok({'hasUpdate': False, 'version': '1.0.0', 'url': ''})

        # -- recharge / mileage --------------------------------------------
        def _api_checkRechargeStatus(self, b: dict):
            return self._ok({'status': 'pending'})

        def _api_checkSecretKeyStatus(self, b: dict):
            return self._ok({'valid': True, 'km': 0.0})

        def _api_redeemSecretKey(self, b: dict):
            key = str(b.get('secret') or b.get('key') or '')
            if not key:
                return self._fail('empty secret key')
            acct = str(b.get('id', 'default'))
            state.mileage[acct] = state.mileage.get(acct, 0.0) + 1.0
            return self._ok({'km': 1.0}, message='redeem success')

        def _api_transfer_mileage(self, b: dict):
            km = float(b.get('km') or b.get('meters') or 0)
            return self._ok({'km': round(km, 2)}, message='transfer success')

        def _api_get_users_ordrs(self, b: dict):
            page = int(b.get('page') or b.get('a') or 1)
            return self._ok({'list': state.orders, 'total': len(state.orders),
                             'page': page, 'pageSize': 20, 'totalPages': 1})

        # -- proxy agents ----------------------------------------------------
        def _api_addProxyAgent(self, b: dict):
            agent = {'id': 'ag-' + uuid.uuid4().hex[:8], **b,
                     'ts': int(time.time())}
            state.agents.append(agent)
            return self._ok(agent)

        def _api_deleteProxyAgent(self, b: dict):
            aid = str(b.get('id', ''))
            state.agents = [a for a in state.agents if a.get('id') != aid]
            return self._ok(True)

        def _api_getProxyAgents(self, b: dict):
            return self._ok({'agents': state.agents, 'total': len(state.agents),
                             'page': 1, 'page_size': 10, 'total_pages': 1})

        def _api_queryProxyUser(self, b: dict):
            return self._ok({'isLoggedIn': False, 'user': None})

        def _api_updateProxyAgentStatus(self, b: dict):
            return self._ok({'isLoggedIn': True})

        def _api_getProxyRechargeAddress(self, b: dict):
            return self._ok({'address': '0x' + '0' * 40, 'chain': 'ERC20'})

        def _api_getProxyRechargeInfo(self, b: dict):
            return self._ok({'agents': [], 'total': 0, 'page': 1,
                             'page_size': 10, 'total_pages': 1})

        def _api_updateProxyRechargeAddress(self, b: dict):
            return self._ok({'address': b.get('address', ''), 'chain': 'ERC20'})

        def _api_edclass(self, b: dict):
            return self._ok({})

        # -- local VM helpers --------------------------------------------------
        def _api_update_geetoken(self, b: dict):
            if b.get('geetoken'):
                state.geetokens.append({'geetoken': b['geetoken'],
                                        'ts': time.time()})
                return self._ok(True)
            if state.geetokens:
                return self._ok(state.geetokens[-1])
            return self._fail('no geetoken pooled')

    return H


def serve(state: MockBackendState, host: str = '127.0.0.1', port: int = 0):
    srv = HTTPServer((host, port), make_handler(state))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv

"""Mock 运动世界校园 school-server (run.gxapp.iydsj.com).

Endpoint set: 00201.0.0 binary evidence PLUS 7.3.70 fields extracted from the
phone APK's plaintext string pool (apk_work/dexblob_strings.txt). Old versioned
paths are kept as aliases; 7.3.70 paths are primary.

Notable 7.3.70 deltas vs the 5.2-era evidence:
- records list:            v650  -> v70230/runnings/records
- get_one_record:          v61200 -> v70320/runnings/get_one_record
- geevalidate:             v31   -> v70270/security/geevalidate
- runModeScoreRule(v73):   -> v70103/runModePolicy (kept both)
- distance:                static -> v560/get/{key}/distance/{distance}
- NEW: runnings/upload/originalSign (two-phase sign), v68/runnings/refreshRecord,
  v55/runnings/recordssummary/semester, v70103/university/getUserInformation

Response convention mirrors the product: {"code":200,"message":..,"data":..}.
Stdlib only.
"""
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

from ...domain import crypto as C
from ...domain.rundata import validate_run_data


class MockIydsjState:
    def __init__(self, rsa_priv: bytes, rsa_pub: bytes):
        self.rsa_priv = rsa_priv
        self.rsa_pub = rsa_pub
        self.tokens: dict[str, dict] = {}
        self.records: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.valid_versions = {'7.3.70', '7.2.70', '7.2.61', '1.0.0'}


def _spring_error(handler: BaseHTTPRequestHandler, message: str,
                  status: int = 500, exc: str = ''):
    """Real iydsj server error envelope (probe-verified Spring Boot style)."""
    body = json.dumps({
        'timestamp': int(time.time() * 1000),
        'status': status,
        'error': 'Internal Server Error' if status == 500 else 'Unauthorized',
        'exception': exc,
        'message': message,
        'path': handler.path.split('?')[0],
    }, ensure_ascii=False).encode()
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _resp(handler: BaseHTTPRequestHandler, obj: dict, status: int = 200):
    body = json.dumps(obj, ensure_ascii=False).encode()
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def make_handler(state: MockIydsjState):
    class H(BaseHTTPRequestHandler):
        server_version = 'MockIydsj/1.0'

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
            self._route(self._read_json())

        def do_POST(self):
            self._route(self._read_json())

        def _route(self, body: dict):
            path = urlparse(self.path).path
            # probe-verified: server requires an appVersion header before any
            # business logic; missing/malformed -> ClientParameterException
            ver = self.headers.get('appVersion') or self.headers.get('Appversion')
            if not ver or ver not in state.valid_versions:
                return _spring_error(self, '版本号格式错误!',
                                     exc='com.iydsj.server.campusapi.exception'
                                         '.ClientParameterException')
            with state.lock:
                try:
                    if path == '/api/v70100/login':
                        return self._login(body)
                    if path == '/api/v31/security/login/geepreprocess':
                        return self._ok({'challenge': uuid.uuid4().hex,
                                         'gt': 'mock-gt',
                                         'success': 1})
                    if path in ('/api/v31/security/login/geevalidate',
                                  '/api/v70270/security/geevalidate'):
                        return self._ok({'result': 'success',
                                         'geetoken': 'mock-gee-' + uuid.uuid4().hex[:16]})
                    if path == '/api/v65/security/checkGeeUse':
                        return self._ok({'useGee': True})
                    if path == '/api/runnings/upload/originalSign':
                        return self._ok({'originalSign':
                                         'mock-os-' + uuid.uuid4().hex[:16]})
                    if path == '/api/v68/runnings/refreshRecord':
                        return self._ok({'refreshed': True})
                    if path == '/api/v70260/runnings/save/record':
                        return self._save_record(body)
                    if path in ('/api/v650/runnings/records',
                                  '/api/v70230/runnings/records'):
                        # frontend asserts Array.isArray(data): bare array
                        return self._ok(list(state.records.values()))
                    if path in ('/api/v61200/runnings/get_one_record',
                                '/api/v70320/runnings/get_one_record'):
                        rid = body.get('id') or body.get('runRecordId')
                        rec = state.records.get(rid)
                        return self._ok(rec) if rec else self._fail('record not found')
                    if path in ('/api/v37/runnings/recordssummary/all',
                                '/api/v55/runnings/recordssummary/semester'):
                        dist = sum(r.get('distance_m', 0) for r in state.records.values())
                        return self._ok({'totalDistance': dist,
                                         'runCount': len(state.records)})
                    if path == '/api/v3/university/get/list':
                        return self._ok([{'unid': 'mock-unid-1',
                                          'school_name': 'Mock University',
                                          'glat': 24.87845, 'glon': 118.673878}])
                    if path == '/api/v560/running/campusZoneList':
                        return self._ok([{'unid': body.get('unid', 'mock-unid-1'),
                                          'points': []}])
                    if path in ('/api/v560/get/1/distance/1',) or re.match(
                            r'^/api/v560/get/[^/]+/distance/[^/]+$', path):
                        return self._ok({'distance': 0})
                    if path in ('/api/v73/runModeScoreRule',
                                '/api/v70103/runModePolicy'):
                        return self._ok({'speedLow': 4.0, 'speedHigh': 10.0,
                                         'stepLow': 140, 'stepHigh': 200})
                    if path == '/api/v61101/user/updatezone':
                        return self._ok(True)
                    if path == '/api/v6/user/logout':
                        return self._ok(True)
                    if path == '/api/v70100/run/facecheck/save':
                        return self._ok({'saved': True})
                    if path == '/api/obs/temporary/url':
                        return self._ok({'url': 'http://127.0.0.1:9/mock-obs'})
                    if path == '/api/v1/badge/user/listInfo':
                        return self._ok([])
                    return self._fail(f'unknown endpoint {path}', code=404)
                except Exception as e:  # noqa: BLE001 - mock must never crash
                    return self._fail(f'mock error: {e}')

        def _login(self, body: dict):
            username = body.get('username') or body.get('phone_number')
            if not username:
                return self._fail('missing username')
            uid = 'mock-uid-' + uuid.uuid4().hex[:12]
            token = 'mock-token-' + uuid.uuid4().hex[:16]
            state.tokens[token] = {'uid': uid, 'username': username,
                                   'ts': int(time.time())}
            return self._ok({'uid': uid, 'token': token,
                             'unid': 'mock-unid-1',
                             'DeviceId': body.get('DeviceId', '')})

        def _save_record(self, body: dict):
            # headers check (binary evidence)
            if self.headers.get('Appid') != C.APP_ID:
                return self._fail('bad Appid')
            if not (self.headers.get('Geeid') or '').startswith('G01-'):
                return self._fail('bad Geeid')
            for k in ('encrypted_data', 'encrypted_key', 'aes_iv'):
                if k not in body:
                    return self._fail(f'missing field {k}')
            try:
                inner = C.hybrid_decrypt(body, state.rsa_priv)
            except Exception as e:  # noqa: BLE001
                return self._fail(f'decrypt failed: {e}')
            run_data = inner.get('run_data_') or inner.get('run_data') or {}
            errs = validate_run_data(run_data)
            if errs:
                return self._fail('run_data invalid: ' + ','.join(errs))
            ent = inner.get('entity') or {}
            if not ent.get('originalSign') or not ent.get('signature'):
                return self._fail('missing originalSign/signature')
            # official checkTimeAndUpload: |server - client| <= 5min
            skew = abs(int(time.time() * 1000)
                       - int(inner.get('current_timestamp', 0)))
            if skew > 5 * 60 * 1000:
                return self._fail('time skew too large')
            # official signature verification: recompute over 31-field map
            # Java: String.valueOf((Object) null) == "null" and paraFilter
            # only drops real nulls, so null fields sign as the literal "null".
            m = {}
            for k in C.UPLOAD_SIGN_FIELDS:
                v = ent.get(k)
                if v is None:
                    m[k] = 'null'
                    continue
                m[k] = v if isinstance(v, str) else (
                    'true' if v is True else 'false' if v is False else str(v))
            if C.official_presign(m) != ent['originalSign']:
                return self._fail('bad originalSign')
            if C.official_sign(m) != ent['signature']:
                return self._fail('bad signature')
            # official duplicate: same run uuid twice -> 10304
            if any(r.get('uuid') == ent.get('uuid')
                   for r in state.records.values()):
                return self._fail('duplicate record', code=10304)
            rid = 'rec-' + uuid.uuid4().hex[:12]
            rec = {
                'id': rid,
                'runRecordId': rid,
                'uuid': ent.get('uuid', ''),
                'uid': inner.get('uid', ''),
                'distance_m': run_data.get('juli', 0),
                'distance_km': run_data.get('km', 0),
                'duration': run_data.get('sportTime', 0),
                'beginTime': inner.get('current_timestamp',
                                       int(time.time() * 1000)),
                'geeToken': inner.get('geeToken', '')[:24],
                'originalSign': ent.get('originalSign', ''),
                'signature': ent.get('signature', ''),
                'serverCheckStatus': 'pass',
            }
            state.records[rid] = rec
            return self._ok(rec, message='upload success')

    return H


def serve(state: MockIydsjState, host: str = '127.0.0.1', port: int = 0):
    srv = HTTPServer((host, port), make_handler(state))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv

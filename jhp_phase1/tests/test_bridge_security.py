"""Bridge hardening regression tests.

The local bridge has no auth because it is loopback-only; these checks keep it
from being callable by a random web page and keep malformed bodies from being
silently treated as a valid no-argument call.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from replica.server import serve


def _post(base, method='real_get_session', **kwargs):
    return requests.post(base + '/bridge/' + method, timeout=15, **kwargs)


def test_bridge_rejects_bad_requests_and_sanitises_session():
    old_data = os.environ.get('JHP_DATA_DIR')
    old_cache = os.environ.get('JHP_POINTS_CACHE')
    tmp = tempfile.mkdtemp(prefix='jhp_bridge_security_')
    with open(os.path.join(tmp, 'accounts_emu.json'), 'w',
              encoding='utf-8') as f:
        json.dump({
            'uid': 'test-uid',
            'token': 'tok-secret-123456',
            'DeviceId': 'dev-secret-123456',
            'name': '测试',
            'keepalive': {'enabled': False},
        }, f)
    srv = serve(0, store_dir=tmp, quiet=True)
    base = 'http://127.0.0.1:%d' % srv.server_address[1]
    try:
        # Valid bridge call; the raw token/DeviceId must never reach JS.
        r = _post(base, json={'args': {}})
        assert r.status_code == 200, r.text
        payload = r.json()
        assert payload['code'] == 200, payload
        data = payload['data']
        assert 'token' not in data and 'DeviceId' not in data, data
        assert data['token_present'] is True
        assert data['device_id_present'] is True
        assert 'tok-secret' not in json.dumps(data)
        assert 'dev-secret' not in json.dumps(data)
        assert data['token_masked'].startswith('tok-')

        # Empty body is not the same as an empty args object.
        r = requests.post(base + '/bridge/real_get_session', data=b'',
                          headers={'Content-Type': 'application/json'},
                          timeout=15)
        assert r.status_code == 400, (r.status_code, r.text)

        # A browser form/simple request cannot smuggle JSON past the bridge.
        r = requests.post(base + '/bridge/real_get_session',
                          data='{"args": {}}',
                          headers={'Content-Type': 'text/plain'},
                          timeout=15)
        assert r.status_code == 415, (r.status_code, r.text)

        r = requests.post(base + '/bridge/real_get_session',
                          data='{', headers={'Content-Type': 'application/json'},
                          timeout=15)
        assert r.status_code == 400, (r.status_code, r.text)

        r = _post(base, json={'args': 'not-an-array'})
        assert r.status_code == 400, (r.status_code, r.text)

        # Cross-site Origin and DNS-rebinding Host are refused.
        r = _post(base, json={'args': {}},
                  headers={'Origin': 'http://evil.example'})
        assert r.status_code == 403, (r.status_code, r.text)
        r = _post(base, json={'args': {}},
                  headers={'Host': 'evil.example'})
        assert r.status_code == 403, (r.status_code, r.text)

        # Same-origin is still accepted (browsers send Origin on POST).
        r = _post(base, json={'args': {}}, headers={'Origin': base})
        assert r.status_code == 200, (r.status_code, r.text)

        r = _post(base, method='no_such_api', json={'args': []})
        assert r.status_code == 404, (r.status_code, r.text)
    finally:
        srv.shutdown()
        srv.server_close()
        if old_data is None:
            os.environ.pop('JHP_DATA_DIR', None)
        else:
            os.environ['JHP_DATA_DIR'] = old_data
        if old_cache is None:
            os.environ.pop('JHP_POINTS_CACHE', None)
        else:
            os.environ['JHP_POINTS_CACHE'] = old_cache

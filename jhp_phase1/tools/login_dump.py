"""Dump full login response body + headers."""
import base64
import json
import sys
import time
import uuid
import urllib.request
import urllib.error
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, r'D:\C-Drive-Archive\Downloads\聚合跑最新版（点进去下载）'
                   r'\好想跑\jhp_phase1')

from replica.realclient import Account, RealClient, build_headers, token_sign, LOGIN_URL
import os

acct = Account(name='emu', uid='', token='')
rc = RealClient()

ts = str(int(time.time() * 1000))
nonce = str(uuid.uuid4())
headers = build_headers(acct, ts, nonce)
headers['uid'] = ''
headers['token'] = ''
headers['tokenSign'] = token_sign(ts, '', '')
body_obj = {'loginType': 1, 'device_model': acct.deviceName,
            'os_version': acct.osVersion, 'uuid': nonce}
headers['Authorization'] = 'Basic ' + base64.b64encode(
    (os.environ.get('JHP_USER', '') + ':' + os.environ.get('JHP_PASS', '')).encode()).decode()
plain = json.dumps(body_obj, separators=(',', ':'))
body = rc.encrypt_body(plain)
hdrs = dict(headers)
hdrs['headerSign'] = rc.header_sign(headers)

req = urllib.request.Request(LOGIN_URL, data=body.encode(),
                             headers=hdrs, method='POST')
try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        status, data, rh = resp.status, resp.read(), resp.headers
except urllib.error.HTTPError as e:
    status, data, rh = e.code, e.read(), e.headers

print('status:', status, 'len:', len(data))
print('== response headers ==')
for k, v in rh.items():
    print(f'  {k}: {v}')
print('== body ==')
print(data.decode('utf-8', 'replace'))
# is r base64?
try:
    j = json.loads(data)
    r = j.get('r', '')
    print('== r fields:', list(j.keys()))
    print('r len:', len(r))
    try:
        dec = base64.b64decode(r, validate=False)
        print('r b64-decoded len:', len(dec))
        print('r decoded head hex:', dec[:64].hex())
        print('r decoded head ascii:', ''.join(chr(b) if 32 <= b < 127 else '.' for b in dec[:80]))
    except Exception as e:
        print('r not plain b64:', e)
except Exception as e:
    print('not json:', e)

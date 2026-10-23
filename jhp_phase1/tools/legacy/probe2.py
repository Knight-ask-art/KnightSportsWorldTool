"""Probe zone/point path variants + old/new token comparison."""
import base64
import json
import os
import sys
import time
import uuid
import urllib.request
import urllib.error
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, r'D:\C-Drive-Archive\Downloads\聚合跑最新版（点进去下载）'
                   r'\好想跑\jhp_phase1')

from replica.transport.sidecar import get_sidecar
from replica.transport.realclient import Account, build_headers

HOST = 'run.gxapp.iydsj.com'
sc = get_sidecar()


def post(path, body_obj, token, uid):
    acct = Account(name='t', uid=uid, token=token)
    ts = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())
    headers = build_headers(acct, ts, nonce)
    plain = json.dumps(body_obj, separators=(',', ':'))
    body = sc.w3270(base64.b64encode(plain.encode()).decode())
    hdrs = dict(headers)
    hdrs['headerSign'] = sc.w30f(json.dumps(
        {k: v for k, v in headers.items() if k != 'Host'}, separators=(',', ':')))
    req = urllib.request.Request(f'https://{HOST}{path}',
                                 data=body.encode(), headers=hdrs, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def summarize(status, data):
    if status == 404:
        return '404 NOT-EXIST'
    if status == 401:
        try:
            m = json.loads(data).get('message', '')
        except Exception:
            m = ''
        return f'401 EXISTS (auth): {m[:40]}'
    if status == 200 and len(data) == 0:
        return '200+0 soft-reject'
    return f'{status} len={len(data)} {data[:80]!r}'


NEW = os.environ.get('JHP_TOKEN', '')
OLD = ''

print('== token compare (school-list) ==')
for tag, tok, uid in (('env-token', NEW, os.environ.get('JHP_UID', '')),
                      ('empty', '', '')):
    s, d = post('/api/v3/university/get/list', {}, tok, uid)
    print(f'  [{tag}] {summarize(s, d)}')

print()
print('== zone/point path variants ==')
paths = [
    '/api/v560/running/campusZoneList',
    '/api/v70230/running/campusZoneList',
    '/api/v70260/running/campusZoneList',
    '/api/v77/running/campusZoneList',
    '/api/v560/get/destinationRun/points',
    '/api/v560/1/get/five/points',
    '/api/v1/getGeoFenceForRun',
    '/api/v61101/user/updatezone',
    '/api/v55/university/campusid',
    '/api/v70103/runModePolicy',
    '/api/v73/runModeScoreRule',
]
for p in paths:
    s, d = post(p, {'unid': 47203, 'glat': 22.9, 'glon': 113.0,
                    'school': 'x', 'roomId': 1, 'key': '1', 'distance': 1},
                NEW, os.environ.get('JHP_UID', ''))
    print(f'  {p:<52} {summarize(s, d)}')

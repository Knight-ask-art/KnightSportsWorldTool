"""Matrix probe: token sanity + response format + zone path variants."""
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
from replica.transport.realclient import Account, build_headers, classify

UID = os.environ.get('JHP_UID', '')
TOKEN = os.environ.get('JHP_TOKEN', '')
if not (UID and TOKEN):
    raise SystemExit('set JHP_UID/JHP_TOKEN; the old hardcoded account was removed')
HOST = 'run.gxapp.iydsj.com'

acct = Account(name='emu', uid=UID, token=TOKEN)
sc = get_sidecar()


def post(path, body_obj, encrypt=True):
    ts = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())
    headers = build_headers(acct, ts, nonce)
    plain = json.dumps(body_obj, separators=(',', ':'))
    if encrypt:
        body = sc.w3270(base64.b64encode(plain.encode()).decode())
    else:
        body = plain
    hdrs = dict(headers)
    hdrs['headerSign'] = sc.w30f(json.dumps(
        {k: v for k, v in headers.items() if k != 'Host'}, separators=(',', ':')))
    req = urllib.request.Request(f'https://{HOST}{path}',
                                 data=body.encode(), headers=hdrs, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status, data = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, data = e.code, e.read()
    return status, data


tests = [
    ('school-list', '/api/v3/university/get/list', {}, True),
    ('school-list-plain', '/api/v3/university/get/list', {}, False),
    ('records', '/api/v70230/runnings/records', {'unid': 47203}, True),
    ('records-plain', '/api/v70230/runnings/records', {'unid': 47203}, False),
    ('zone-enc', '/api/v560/running/campusZoneList', {'unid': 47203}, True),
    ('zone-plain', '/api/v560/running/campusZoneList', {'unid': 47203}, False),
    ('zone-v70240', '/api/v70240/running/campusZoneList', {'unid': 47203}, True),
    ('geofence', '/api/v1/getGeoFenceForRun', {'unid': 47203}, True),
    ('updatezone', '/api/v61101/user/updatezone', {'unid': 47203}, True),
]

for name, path, body, enc in tests:
    status, data = post(path, body, enc)
    verdict, detail = classify(status, data)
    print(f'[{name}] {path} enc={enc}')
    print(f'  -> {status} len={len(data)} | {verdict}')
    head = data[:200].decode('utf-8', 'replace').replace('\n', ' ')
    print(f'  head: {head}')
    print()

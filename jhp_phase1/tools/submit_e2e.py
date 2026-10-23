"""Submit-then-decrypt in ONE sidecar process (body-key theory test).

Order: HOOKS -> ENC(request body via w3270) -> real POST submit -> DEC1
(response envelope) in the SAME process. If the response key = the request's
body key (stored in process state at ENC time), the decrypt should now pass
PKCS7 and print the plaintext business result.
"""
import base64
import json
import os
import random
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from replica.domain import rundata  # noqa: E402
from replica.transport.realclient import (Account, build_headers, token_sign,  # noqa: E402
                                HOST, SUBMIT_URL)
from replica.transport.sidecar import JARS  # noqa: E402

UNIDBG = r'C:\Users\20969\AppData\Local\Temp\opencode\apk_work\unidbg'
JAVA = r'D:\Codex\tools\jdk-21\jdk-21.0.11+10\bin\java.exe'


def main():
    cp = 'out'
    for j in JARS:
        cp += ';libs/' + j
    acct = Account.from_dict(json.load(open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     'data', 'accounts.json')))[0])
    rng = random.Random(int(time.time()))
    run = rundata.generate_run_data(distance_km=2.5, pace_min_per_km=6.0,
                                    cadence=172, rng=rng,
                                    center_lat=acct.center_lat,
                                    center_lng=acct.center_lng)
    ctx = {'uid': int(acct.uid), 'unid': acct.unid, 'runMode': acct.runMode,
           'schoolId': acct.schoolId, 'schoolName': acct.schoolName,
           'campusName': acct.campusName, 'token': acct.token}
    entity = rundata.build_upload_entity(run, ctx)
    signed = rundata.sign_upload_entity(entity)
    plain = json.dumps(signed, separators=(',', ':'))

    p = subprocess.Popen([JAVA, '-cp', cp, 'OracleFlow', 'oracle.apk'],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, cwd=UNIDBG,
                         text=True, encoding='utf-8', errors='replace', bufsize=1)
    lines = []
    threading.Thread(target=lambda: [lines.append(l.rstrip()) for l in p.stdout],
                     daemon=True).start()
    cursor = [0]

    def wait_out(hard=180):
        dl = time.time() + hard
        while time.time() < dl:
            if p.poll() is not None:
                raise SystemExit(f'java exited rc={p.returncode}')
            for i in range(cursor[0], len(lines)):
                l = lines[i]
                if l.startswith('OUT:') or l.strip() == 'READY':
                    cursor[0] = i + 1
                    return l
            time.sleep(0.2)
        raise SystemExit('timeout')

    def send(cmd):
        p.stdin.write(cmd + '\n')
        p.stdin.flush()

    wait_out(120)
    send('HOOKS:')
    wait_out()
    print('[ready+hooks]')

    ts = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())
    headers = build_headers(acct, ts, nonce)
    send('ENC:' + base64.b64encode(plain.encode()).decode())
    body_env = wait_out()[4:]
    print(f'[ENC -> envelope {len(body_env)}ch]')

    hdrs = dict(headers)
    hdrs['headerSign'] = None
    # headerSign first (also same process)
    hjson = json.dumps({k: v for k, v in headers.items() if k != 'Host'},
                       separators=(',', ':'))
    send('W30F:' + base64.b64encode(hjson.encode()).decode())
    hs = wait_out()[4:]
    hdrs['headerSign'] = hs

    req = urllib.request.Request(SUBMIT_URL, data=body_env.encode(),
                                 headers=hdrs, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            status, resp = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, resp = e.code, e.read()
    text = resp.decode('utf-8', 'replace').strip()
    print(f'[POST submit -> {status}, {len(resp)}B] {text[:90]}')

    if text.startswith('{"r"'):
        n = len(lines)
        send('DEC1:' + text)
        dl = time.time() + 180
        while time.time() < dl and p.poll() is None:
            if any(l.startswith('OUT:') for l in lines[cursor[0]:]):
                break
            time.sleep(0.2)
        cursor[0] = len(lines)
        for l in lines[n:]:
            if any(k in l for k in ('OUT:', 'IV=', 'BLKLOOP', 'f6084ret')):
                print(l[:300])
    try:
        send('QUIT')
        p.wait(timeout=10)
    except Exception:
        p.kill()


if __name__ == '__main__':
    main()

"""Round-trip with gate-frame dump: ENC -> POST -> DEC1, capture GATEFRAME."""
import base64
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from replica.realclient import Account, build_headers, HOST  # noqa: E402
from replica.sidecar import JARS  # noqa: E402

UNIDBG = r'C:\Users\20969\AppData\Local\Temp\opencode\apk_work\unidbg'
JAVA = r'D:\Codex\tools\jdk-21\jdk-21.0.11+10\bin\java.exe'
LOG = r'C:\Users\20969\AppData\Local\Temp\opencode\apk_work\roundtrip.log'
SUBMIT_URL = f'https://{HOST}/api/v70260/runnings/save/record'

plain = json.dumps({
    "type": 1, "userId": 0, "latitude": 0.0, "longitude": 0.0,
    "speed": 0.0, "stepNum": 0, "distance": 0.0, "cadence": 0,
    "source": 3, "altitude": 0.0, "topSpeed": 0.0, "runTime": 0,
}, separators=(',', ':'))

p = subprocess.Popen([JAVA, '-cp', 'out;' + ';'.join('libs/' + j for j in JARS),
                      'OracleFlow', 'oracle.apk'],
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                     cwd=UNIDBG, text=True, encoding='utf-8', errors='replace', bufsize=1)
lines = []
threading.Thread(target=lambda: [lines.append(l.rstrip()) for l in p.stdout], daemon=True).start()
cursor = [0]

def wait_out(hard=180):
    dl = time.time() + hard
    while time.time() < dl:
        if p.poll() is not None:
            raise SystemExit(f'java exited rc={p.returncode}')
        for i in range(cursor[0], len(lines)):
            l = lines[i]
            if l.startswith('OUT:') or l.strip() == 'READY' or l.startswith('D3LOG') or l.startswith('S2CALL'):
                cursor[0] = i + 1
                return l
        time.sleep(0.2)
    raise SystemExit('timeout')

def send(cmd):
    p.stdin.write(cmd + '\n'); p.stdin.flush()

wait_out(150)
print('[ready]')
acct = Account.from_dict(json.load(open(os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'accounts.json')))[0])

ts = str(int(time.time() * 1000))
nonce = str(uuid.uuid4())
headers = build_headers(acct, ts, nonce)

# 1) ENC the body (fills registry with our keyData)
send('ENC:' + base64.b64encode(plain.encode()).decode())
body_env = wait_out()[4:]
print(f'[ENC envelope {len(body_env)}ch]')

# 2) headerSign (ENC shape)
hjson = json.dumps({k: v for k, v in headers.items() if k != 'Host'}, separators=(',', ':'))
send('W30F:' + base64.b64encode(hjson.encode()).decode())
hs = wait_out()[4:]
hdrs = dict(headers)
hdrs['headerSign'] = hs

# 3) POST
req = urllib.request.Request(SUBMIT_URL, data=body_env.encode(), headers=hdrs, method='POST')
try:
    with urllib.request.urlopen(req, timeout=60) as r:
        status, resp = r.status, r.read()
except urllib.error.HTTPError as e:
    status, resp = e.code, e.read()
text = resp.decode('utf-8', 'replace').strip()
print(f'[POST -> {status}, {len(resp)}B] {text[:100]}')

# 4) DEC1 the response with frame dump
if text.startswith('{"r"'):
    send('HOOKS:')
    wait_out(60)
    print('[hooks on]')
    send('DEC1:' + text)
    dl = time.time() + 180
    while time.time() < dl and p.poll() is None:
        if any(l.startswith('OUT:GATEFRAME') for l in lines[cursor[0]:]) or any(
                l.startswith('OUT:') and 'GATEFRAME' not in l for l in lines[cursor[0]:]):
            time.sleep(1.0)
            break
        time.sleep(0.2)
time.sleep(2.0)
logf = open(LOG, 'w', encoding='utf-8')
for l in lines:
    logf.write(l + '\n')
logf.close()
gf = [l for l in lines if l.startswith('OUT:GATEFRAME')]
outs = [l for l in lines if l.startswith('OUT:')]
print(f'=== {len(lines)} lines, OUT={len(outs)}, GATEFRAME={len(gf)} ===')
for l in outs[-6:]:
    print(' ', l[:180])
try:
    send('QUIT'); p.wait(timeout=10)
except Exception:
    p.kill()

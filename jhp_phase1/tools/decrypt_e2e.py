"""End-to-end response decryption test + memory key hunt in ONE sidecar process.

Flow: boot OracleFlow -> HOOKS -> W30F headerSign -> real GET gmttime ->
DEC1(response) in the same process -> dump singleton/heap -> save everything
for the Python-side key hunt.
"""
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
from replica.transport.realclient import Account, build_headers, HOST  # noqa: E402
from replica.transport.sidecar import JARS  # noqa: E402

UNIDBG = r'C:\Users\20969\AppData\Local\Temp\opencode\apk_work\unidbg'
JAVA = r'D:\Codex\tools\jdk-21\jdk-21.0.11+10\bin\java.exe'
OUT = r'C:\Users\20969\AppData\Local\Temp\opencode\apk_work\e2e_capture'
GMT_URL = f'https://{HOST}/api/v3/getserver/gmttime'
os.makedirs(OUT, exist_ok=True)


def main():
    cp = 'out'
    for j in JARS:
        cp += ';libs/' + j
    acct = Account.from_dict(json.load(open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     'data', 'accounts.json')))[0])

    p = subprocess.Popen([JAVA, '-cp', cp, 'OracleFlow', 'oracle.apk'],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, cwd=UNIDBG,
                         text=True, encoding='utf-8', errors='replace', bufsize=1)
    lines = []
    threading.Thread(target=lambda: [lines.append(l.rstrip()) for l in p.stdout],
                     daemon=True).start()

    cursor = [0]

    def wait_out(hard=150):
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
        raise SystemExit('timeout waiting OUT:')

    def send(cmd):
        p.stdin.write(cmd + '\n')
        p.stdin.flush()

    t0 = time.time()
    wait_out(120)
    print(f'[ready {time.time()-t0:.1f}s]')

    send('HOOKS:')
    print(wait_out())

    ts = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())
    headers = build_headers(acct, ts, nonce)
    hjson = json.dumps({k: v for k, v in headers.items() if k != 'Host'},
                       separators=(',', ':'))
    send('W30F:' + base64.b64encode(hjson.encode()).decode())
    header_sign = wait_out()[4:]
    print(f'[headerSign len={len(header_sign)}]')

    req = urllib.request.Request(GMT_URL, headers=dict(headers, headerSign=header_sign),
                                 method='GET')
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            status, body = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read()
    text = body.decode('utf-8', 'replace').strip()
    print(f'[GET -> {status}, {len(body)}B] {text[:100]}')

    capture = {'headerSign': header_sign, 'resp_status': status, 'resp': text,
               'hjson': hjson}
    if text.startswith('{"r"'):
        n = len(lines)
        send('DEC1:' + text)
        dl = time.time() + 150
        while time.time() < dl:
            if p.poll() is not None:
                break
            if any(l.startswith('OUT:') for l in lines[cursor[0]:]):
                break
            time.sleep(0.2)
        cursor[0] = len(lines)
        d3 = [l for l in lines[n:] if 'D3LOG' in l or 'S2CALL' in l or l.startswith('OUT:')]
        for l in d3:
            print(l[:400])
        capture['d3log'] = d3

        # memory hunt: singleton address from D3LOG @e4764
        sgl = None
        for l in d3:
            if 'singleton=0x' in l:
                sgl = int(l.split('singleton=0x')[1].split()[0], 16)
                break
        dumps = {}
        # heap/bss/stack ranges around observed allocations
        for name, start, ln in [('heapA', 0x12700000, 0x40000),
                                ('heapB', 0x12740000, 0x40000),
                                ('heapC', 0x12780000, 0x40000),
                                ('malloc1', 0x12600000, 0x40000),
                                ('malloc2', 0x12640000, 0x40000),
                                ('malloc3', 0x12680000, 0x40000),
                                ('malloc4', 0x126c0000, 0x40000),
                                ('stack1', 0xe4fc0000, 0x40000),
                                ('bssT', 0x123c0000, 0x10000)]:
            if ln == 0:
                continue
            got = []
            off = 0
            while off < ln:
                chunk = min(0x8000, ln - off)
                send(f'DUMP:{start + off:x}:{chunk}')
                ol = wait_out(60)
                got.append(ol[4:])
                off += chunk
            dumps[name] = {'start': start, 'hex': ''.join(got)}
        capture['dumps'] = {k: {'start': v['start'], 'len': len(v['hex']) // 2}
                            for k, v in dumps.items()}
        json.dump({'meta': capture['dumps']}, open(os.path.join(OUT, 'meta.json'), 'w'))
        for k, v in dumps.items():
            open(os.path.join(OUT, k + '.hex'), 'w').write(v['hex'])
        json.dump(capture, open(os.path.join(OUT, 'capture.json'), 'w'))
        print('[saved dumps to', OUT, ']', {k: v['len'] for k, v in capture['dumps'].items()})
    else:
        print('response is NOT an envelope')
    try:
        send('QUIT')
        p.wait(timeout=10)
    except Exception:
        p.kill()


if __name__ == '__main__':
    main()

"""Token-state ordering experiment.

Theory: registry tokens must exist BEFORE the request is built, so the server
and the client derive the same response key. Two variants in separate
processes:
  normal : W30F -> GET -> DEC1   (tokens generated at decrypt time only)
  prewarm: DEC1(stale env) -> W30F -> GET -> DEC1   (state warmed first)
Each step dumps the registry so token stability is visible.
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
from replica.realclient import Account, build_headers, HOST  # noqa: E402
from replica.sidecar import JARS  # noqa: E402

UNIDBG = r'C:\Users\20969\AppData\Local\Temp\opencode\apk_work\unidbg'
JAVA = r'D:\Codex\tools\jdk-21\jdk-21.0.11+10\bin\java.exe'
GMT_URL = f'https://{HOST}/api/v3/getserver/gmttime'
STALE = open(r'C:\Users\20969\AppData\Local\Temp\opencode\apk_work\env_gmttime.txt').read().strip()
REG = '127133a8:192'


def run_variant(variant):
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
        raise SystemExit('timeout')

    def send(cmd):
        p.stdin.write(cmd + '\n')
        p.stdin.flush()

    def dump_reg(tag):
        n = len(lines)
        send('DUMP:' + REG)
        wait_out(60)
        out = None
        for l in lines[n:]:
            if l.startswith('OUT:') and len(l) > 100:
                out = l[4:]
                break
        toks = []
        if out:
            b = bytes.fromhex(out[:len(out) // 2 * 2])
            for s in range(4):
                seg = b[s * 0x30:(s + 1) * 0x30]
                if len(seg) >= 0x20:
                    ln = int.from_bytes(seg[0x18:0x20], 'little')
                    toks.append((seg[:ln] if 0 < ln < 24 else b'?').decode('latin1'))
        print(f'  [{variant}] registry {tag}: {toks}')
        return toks

    print(f'=== variant: {variant} ===')
    wait_out(120)
    send('HOOKS:')
    wait_out()

    if variant == 'prewarm':
        send('DEC1:' + STALE)
        wait_out(150)
        dump_reg('after stale DEC1')

    ts = str(int(time.time() * 1000))
    headers = build_headers(acct, ts, str(uuid.uuid4()))
    hjson = json.dumps({k: v for k, v in headers.items() if k != 'Host'},
                       separators=(',', ':'))
    send('W30F:' + base64.b64encode(hjson.encode()).decode())
    hs = wait_out()[4:]
    dump_reg('after W30F')

    req = urllib.request.Request(GMT_URL, headers=dict(headers, headerSign=hs), method='GET')
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            status, body = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read()
    text = body.decode('utf-8', 'replace').strip()
    print(f'  [GET -> {status}, {len(body)}B]')

    n = len(lines)
    send('DEC1:' + text)
    dl = time.time() + 150
    while time.time() < dl and p.poll() is None:
        if any(l.startswith('OUT:') for l in lines[cursor[0]:]):
            break
        time.sleep(0.2)
    cursor[0] = len(lines)
    ivs = [l.split('IV=')[1].split()[0] for l in lines[n:] if 'IV=' in l]
    outs = [l for l in lines[n:] if l.startswith('OUT:')]
    print(f'  [DEC1] IV(s)={ivs} OUT={outs}')
    dump_reg('after fresh DEC1')
    try:
        send('QUIT')
        p.wait(timeout=10)
    except Exception:
        p.kill()


if __name__ == '__main__':
    run_variant('normal')
    run_variant('prewarm')

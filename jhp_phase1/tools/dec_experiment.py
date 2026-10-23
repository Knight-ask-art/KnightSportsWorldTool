"""Response-decryption experiment (Theory A: response is encrypted with the
request's K; our sidecar process retains it after a real w3270 request).

Steps:
 1. ONE sidecar process.
 2. Real submission (w30f + w3270 -> POST -> real response envelope).
 3. SAME process: DEC:<response>  -> the 5-method battery tries
    w1228/wee0c/w92bb/w183/w30f with inputs: full json, r, s, r+s, s+r, r|s.
 4. Same for the OLD captured response (control: its K is lost -> expect garbage).
"""
import json
import random
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, r'C:\Users\20969\AppData\Local\Temp\opencode\apk_work')

from replica import rundata
from replica.transport.realclient import Account, RealClient, build_headers, SUBMIT_URL
from replica.transport.sidecar import UnidbgSidecar

OLD_RESP = Path(r'C:\Users\20969\AppData\Local\Temp\opencode\apk_work\unidbg\gmttime_env_compact.json')


def real_submit(cli: RealClient, acct: Account) -> str:
    rng = random.Random(int(time.time()))
    run = rundata.generate_run_data(distance_km=2.5, pace_min_per_km=6.0, cadence=172,
                                    rng=rng, center_lat=acct.center_lat,
                                    center_lng=acct.center_lng)
    ctx = {'uid': int(acct.uid), 'unid': acct.unid, 'runMode': acct.runMode,
           'schoolId': acct.schoolId, 'schoolName': acct.schoolName,
           'campusName': acct.campusName, 'token': acct.token}
    entity = rundata.build_upload_entity(run, ctx)
    signed = rundata.sign_upload_entity(entity)
    ts = str(int(time.time() * 1000))
    nonce = 'decexp-' + str(time.time_ns())[-12:]
    headers = build_headers(acct, ts, nonce)
    body = cli.encrypt_body(json.dumps(signed, separators=(',', ':')))
    hdrs = dict(headers)
    hdrs['headerSign'] = cli.header_sign(headers)
    req = urllib.request.Request(SUBMIT_URL, data=body.encode('utf-8'),
                                 headers=hdrs, method='POST')
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    print(f'[submit] HTTP {resp.status} len={len(data)}')
    return data.decode('utf-8', 'replace')


def score_output(out: str):
    """Judge whether an OUT: payload looks like decrypted plaintext."""
    if not out or out in ('<null>',) or out.startswith('<'):
        return None
    import base64 as b64
    try:
        raw = b64.b64decode(out.strip())
    except Exception:
        raw = None
    printable = lambda b: sum(1 for c in b if 32 <= c < 127 or c in (9, 10, 13)) / max(1, len(b))
    infos = []
    if raw:
        infos.append(('b64dec', printable(raw), raw[:96]))
    infos.append(('asis', printable(out.encode()), out.encode()[:96]))
    best = max(infos, key=lambda x: x[1])
    if best[1] > 0.9:
        return best
    return None


def run_dec(sc: UnidbgSidecar, resp: str, tag: str):
    print(f'\n===== DEC on {tag} (len={len(resp)}) =====', flush=True)
    # the DEC battery runs 5 methods x 6 inputs = 30 OUT lines, then loops back to readline
    proc = sc._proc
    proc.stdin.write('DEC:' + resp + '\n')
    proc.stdin.flush()
    deadline = time.time() + 600
    battery = 0
    last = time.time()
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            print('[sidecar died]')
            return
        line = line.rstrip('\n')
        if line.startswith('OUT:') and 'warm' not in line and 'keyget' not in line \
                and 'slot-reg' not in line:
            battery += 1
            last = time.time()
            payload = line[4:]
            m = re.match(r'(w[0-9a-f]+)\( inlen=(\d+) rawlen=(\d+) ', payload)
            if m:
                meth, inlen, rawlen = m.group(1)[:12], m.group(2), m.group(3)
                rest = payload[m.end():]
                mp = re.search(r'plain=\[(.*?)\]$', rest, re.S)
                plain = mp.group(1) if mp else ''
                looks = 'PRINTABLE!' if plain and sum(
                    1 for c in plain if 32 <= ord(c) < 127) / max(1, len(plain)) > 0.8 else ''
                print(f'  {meth} inlen={inlen} rawlen={rawlen} plain[:48]={plain[:48]!r} {looks}',
                      flush=True)
            else:
                print('  OUT:', payload[:100], flush=True)
            if battery >= 30:
                break
        elif line.strip():
            last = time.time()
        # silence guard: battery finished, oracle is back at readline
        if battery > 0 and time.time() - last > 10:
            break
    print(f'  (battery lines seen: {battery})', flush=True)


def main():
    sc = UnidbgSidecar()
    sc.start()
    cli = RealClient(sc)
    acct = Account.from_dict(json.loads(
        (ROOT / 'data' / 'accounts.json').read_text(encoding='utf-8'))[0])

    # control FIRST: old captured response (its K is lost) in the fresh process
    old = OLD_RESP.read_text(encoding='utf-8').strip()
    run_dec(sc, old, 'OLD captured response (control, K lost)')

    # real submission in the SAME process, then decrypt ITS response
    resp = real_submit(cli, acct)
    run_dec(sc, resp, 'OUR response (same session!)')
    sc.stop()
    print('\nEXPERIMENT DONE')


if __name__ == '__main__':
    main()

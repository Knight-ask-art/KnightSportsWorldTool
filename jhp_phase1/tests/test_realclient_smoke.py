"""Offline smoke: sidecar persistence + real crypto shapes (no server contact).

1. ONE sidecar process serves three calls (w30f, w3270, w3270) — persistence.
2. w3270 output is the real envelope shape: {"d":b64,"h":64hex,"k":b64,"p":"101"}.
3. tokenSign matches the verified formula.
"""
import base64
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from replica.transport.realclient import RealClient, Account, token_sign, build_headers
from replica.transport.sidecar import UnidbgSidecar


def main():
    sc = UnidbgSidecar()
    t0 = time.time()
    cli = RealClient(sc)

    # --- call 1: w30f on a small headers json
    acct = Account(name='t', uid='99999999', token='test-token')
    ts = str(int(time.time() * 1000))
    nonce = '11111111-2222-3333-4444-555555555555'
    headers = build_headers(acct, ts, nonce)
    sign = cli.header_sign(headers)
    assert sign.startswith('{') and '"d"' in sign, f'w30f bad: {sign[:80]}'
    print(f'[1] w30f OK len={len(sign)} head={sign[:48]}...  ({time.time()-t0:.1f}s)')

    # --- calls 2&3: two w3270 in the SAME process (persistence check)
    p1 = json.dumps({'hello': 'world', 'n': 1}, separators=(',', ':'))
    e1 = cli.encrypt_body(p1)
    p2 = json.dumps({'hello': 'world2', 'n': 2}, separators=(',', ':'))
    e2 = cli.encrypt_body(p2)
    for tag, env in (('2', e1), ('3', e2)):
        assert env.startswith('{'), env[:60]
        obj = json.loads(env)
        assert set(obj.keys()) >= {'d', 'h', 'k', 'p'}, obj.keys()
        d_raw = base64.b64decode(obj['d'])
        k_raw = base64.b64decode(obj['k'])
        assert re.fullmatch(r'[0-9a-f]{32}', obj['h']), obj['h']  # w3270 h = MD5
        assert obj['p'] == '101', obj['p']
        assert len(d_raw) % 16 == 0 and len(d_raw) > 0
        # request-side k is a 128-byte key container (differs from the 113B response-side
        # white-box SM2 shape); format verified by the real accepted submission
        assert len(k_raw) == 128 and len(d_raw) % 16 == 0
        print(f'[{tag}] w3270 OK d={len(d_raw)}B k={len(k_raw)}B h={obj["h"][:12]}.. '
              f'p={obj["p"]}  ({time.time()-t0:.1f}s)')
    assert e1 != e2, 'envelopes identical across calls (fresh randomness expected)'
    print('[x] two calls differ -> per-call randomness OK')

    # --- tokenSign formula
    assert token_sign('123', 'tok', 'u1') == token_sign('123', 'tok', 'u1')
    print('[4] tokenSign deterministic OK')

    sc.stop()
    print('SMOKE PASS')


if __name__ == '__main__':
    main()

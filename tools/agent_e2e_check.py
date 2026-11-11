"""Offline end-to-end check: learn -> suggest -> preview -> audit -> confirm.

Uses the real bridge, a temporary store and a fake read-only account. It never
touches the network, never submits and never writes to the repo's data dir.

    python tools/agent_e2e_check.py
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import time
import types

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(APP_DIR, 'app')
if APP not in sys.path:
    sys.path.insert(0, APP)

from replica.adapters.bridge_api import ReplicaApi  # noqa: E402
from replica.application import readonly as RO  # noqa: E402
from replica.domain import safety as S  # noqa: E402

CENTER = (22.4108, 112.9408)


def _fixtures():
    with open(os.path.join(APP, 'fixtures', 'campus_fence_47203.json'),
              encoding='utf-8') as f:
        fence = json.load(f)
    ring = [(float(p['glat']), float(p['glon'])) for p in fence['polygon']]
    return ring


def _oval(laps: int = 2, a_m: float = 70.0, b_m: float = 45.0,
          n: int = 80) -> list[tuple[float, float]]:
    m_lat = 110540.0
    m_lng = 111320.0 * math.cos(math.radians(CENTER[0]))
    pts: list[tuple[float, float]] = []
    for _ in range(laps):
        for i in range(n):
            t = 2.0 * math.pi * i / n
            pts.append((CENTER[0] + (b_m * math.sin(t)) / m_lat,
                        CENTER[1] + (a_m * math.cos(t)) / m_lng))
    pts.append(pts[0])
    return pts


def _checkpoints(line: list[tuple[float, float]], count: int = 5):
    out = []
    for k in range(count):
        idx = int(len(line) * (k + 1) / (count + 1))
        out.append({'lat': float(line[idx][0]), 'lon': float(line[idx][1]),
                    'isMustPoint': False, 'name': f'P{k + 1}'})
    return out


def _fake_detail(rrid, line):
    return {
        'ok': True,
        'detail': {
            'rrid': str(rrid),
            'uuid': f'uuid-{rrid}',
            'distance_m': 1000.0,
            'duration_s': 360,
            'start_ms': 1700000000000 + int(rrid) * 1000,
            'is_cheat': 0,
            'complete': True,
        },
        'track': [{'lat': la, 'lng': ln} for la, ln in line],
    }


def run() -> dict:
    ring = _fixtures()
    line = _oval()
    points = _checkpoints(line)
    store = tempfile.mkdtemp(prefix='hxp-agent-e2e-')
    api = ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'', store)
    acct = types.SimpleNamespace(
        uid='1001', unid='47203', token='tok-e2e-secret',
        center_lat=CENTER[0], center_lng=CENTER[1],
        campusName='广州软件学院 江门校区')
    api._real_account = lambda: acct
    api._read_session = lambda: {'uid': '1001', 'unid': '47203',
                                 'lat': CENTER[0], 'lon': CENTER[1]}

    rows = [{'rrid': i, 'uuid': f'uuid-{i}', 'distance_m': 1000.0,
             'duration_s': 360, 'start_ms': 1700000000000 + i * 1000,
             'complete': True} for i in range(1, 6)]
    RO.recent_records = lambda rc, a, limit=50: {'ok': True, 'records': rows}
    RO.record_detail = lambda rc, a, rrid, uuid=None: _fake_detail(rrid, line)
    S.resolve_start_ms = lambda *a, **k: 1700000000000

    submitted = {'called': False}

    def _no_submit(*args, **kwargs):
        submitted['called'] = True
        raise AssertionError('submit must never be called from the Agent flow')

    api.real_submit_run = _no_submit

    out = api.real_agent_start(text='帮我跑 2.5 公里', points=points, fence=[ring])
    if out.get('code') != 200:
        raise AssertionError(f'agent start failed: {out}')

    st = None
    deadline = time.time() + 30.0
    while time.time() < deadline:
        st = api.real_agent_status()['data']
        if st['state'] in ('wait_confirm', 'error', 'cancelled'):
            break
        time.sleep(0.05)
    if st is None or st['state'] != 'wait_confirm':
        raise AssertionError(f'agent did not reach wait_confirm: {st}')

    audit = (st.get('audit') or {}).get('summary') or {}
    if audit.get('ok') is not True:
        raise AssertionError(f'audit failed: {audit}')
    if not st.get('candidates'):
        raise AssertionError('no candidates')

    conf = api.real_agent_confirm()
    if conf.get('code') != 200:
        raise AssertionError(f'confirm failed: {conf}')
    args = (conf.get('data') or {}).get('preview_args') or {}
    if abs(float(args.get('km') or 0) - 2.5) > 1e-6:
        raise AssertionError(f'confirm km mismatch: {args.get("km")}')
    if not args.get('waypoints') or args.get('closed') is not True:
        raise AssertionError('confirm preview_args incomplete')
    if submitted['called']:
        raise AssertionError('submit was called')
    receipts = os.path.join(store, 'submission_receipts')
    if os.path.isdir(receipts) and os.listdir(receipts):
        raise AssertionError('a submission receipt was written')

    return {
        'state': st['state'],
        'candidates': len(st['candidates']),
        'selected': st.get('selected'),
        'audit': audit,
        'km': args['km'],
        'waypoints': len(args['waypoints']),
        'closed': args['closed'],
        'checkpoints': len(args.get('checkpoints') or []),
        'submitted': submitted['called'],
        'receipts': [],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()
    try:
        result = run()
    except Exception as exc:  # noqa: BLE001 - CLI should print a readable failure
        print('FAIL: %s' % exc)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print('PASS', result)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

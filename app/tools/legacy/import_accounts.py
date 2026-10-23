"""Import real accounts (uid/token/device) from a captured flow JSONL.

The flows file is produced by the mitmproxy capture pipeline
(reverse-engineering/dump_flows.py -> emu_flows.jsonl) while a real account is logged
in on the emulator. This extracts the per-account session facts needed by
realclient.Account.

Usage:
    python tools/import_accounts.py <flows.jsonl> [out_accounts.json]
"""
import json
import sys
from pathlib import Path

HOST = 'run.gxapp.iydsj.com'

# headers that belong to an Account (device fingerprint + session)
FIELDS = {
    'uid': 'uid', 'token': 'token', 'DeviceId': 'DeviceId',
    'deviceName': 'deviceName', 'osVersion': 'osVersion', 'cpuModel': 'cpuModel',
    'physicPixel': 'physicPixel', 'logicPixel': 'logicPixel',
    'appInstallTime': 'appInstallTime', 'appUpdateTime': 'appUpdateTime',
}


def import_accounts(flows_path: str) -> list[dict]:
    accounts = {}
    with open(flows_path, encoding='utf-8') as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if HOST not in rec.get('url', ''):
                continue
            h = rec.get('req_headers') or {}
            uid = h.get('uid') or ''
            token = h.get('token') or ''
            if not uid or not token:
                continue
            a = accounts.setdefault(uid, {'name': f'student-{uid}', 'uid': uid,
                                          'token': token})
            for src, dst in FIELDS.items():
                v = h.get(src)
                if v and (dst not in a or not a[dst]):
                    a[dst] = v
            # body may carry unid/school info on some endpoints
            body = rec.get('req_body') or ''
            if isinstance(body, str) and body.startswith('{'):
                try:
                    bj = json.loads(body)
                except json.JSONDecodeError:
                    bj = {}
                for k in ('unid', 'schoolId', 'schoolName', 'campusName', 'runMode'):
                    if k in bj and bj[k] is not None and k not in a:
                        a[k] = bj[k]
    return list(accounts.values())


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    flows = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else str(
        Path(__file__).resolve().parent.parent.parent / 'data' / 'accounts.json')
    accts = import_accounts(flows)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(accts, f, ensure_ascii=False, indent=2)
    print(f'imported {len(accts)} account(s) -> {out}')
    for a in accts:
        print(f"  uid={a['uid']} token={a['token'][:8]}.. device={a.get('deviceName')}")


if __name__ == '__main__':
    main()

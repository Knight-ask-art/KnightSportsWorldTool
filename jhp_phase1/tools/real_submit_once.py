"""One REAL end-to-end submission through the new RealClient stack.

Real server, real account (from data/accounts.json), real NetSecKit envelope
via the persistent unidbg sidecar. This is the acceptance test for M1.
"""
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from replica import rundata
from replica.realclient import Account, RealClient


def main():
    accounts_path = ROOT / 'data' / 'accounts.json'
    accts = [Account.from_dict(a) for a in json.loads(accounts_path.read_text(encoding='utf-8'))]
    acct = accts[0]
    print(f'account: uid={acct.uid} token={acct.token[:8]}.. device={acct.deviceName}')

    rng = random.Random(int(time.time()))
    run = rundata.generate_run_data(distance_km=2.5, pace_min_per_km=6.0,
                                    cadence=172, rng=rng,
                                    center_lat=acct.center_lat, center_lng=acct.center_lng)
    ctx = {
        'uid': int(acct.uid),
        'unid': acct.unid,
        'runMode': acct.runMode,
        'schoolId': acct.schoolId,
        'schoolName': acct.schoolName,
        'campusName': acct.campusName,
        'token': acct.token,
    }
    entity = rundata.build_upload_entity(run, ctx)
    signed = rundata.sign_upload_entity(entity)

    cli = RealClient()
    t0 = time.time()
    result = cli.submit_run(acct, run, entity, signed)
    dt = time.time() - t0
    print(f'submit took {dt:.1f}s (incl. sidecar first-call)')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result['verdict'] == 'accepted':
        print('REAL SUBMISSION ACCEPTED ✓')
    else:
        print('verdict:', result['verdict'])
    return 0 if result['verdict'] == 'accepted' else 1


if __name__ == '__main__':
    sys.exit(main())

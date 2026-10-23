"""Offline simulation validator -- generates runs and checks every cross-field
invariant the app/server would see. NEVER submits anything.

Usage:
    python tools/validate_sim.py            # default matrix
    python tools/validate_sim.py 1.0 2.0 5.0
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from replica.domain import rundata

FIELDS31 = None


def fmt_pace(pace_min):
    """Mirror Utils.formatSpeed(double)."""
    i = int(pace_min)
    r = round((pace_min - i) * 60)
    return "%d'%02d\"" % (i, r)


def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + (
        "" if cond else "  <-- " + str(extra)))
    return bool(cond)


def validate(km, pace, cadence=172, seed=0):
    import random
    print("\n=== %.2f km @ %s /km  (cadence %d) ===" % (
        km, fmt_pace(pace), cadence))
    rng = random.Random(1000 + seed)
    run = rundata.generate_run_data(distance_km=km, pace_min_per_km=pace,
                                    cadence=cadence, rng=rng)
    ctx = {"uid": 99999999, "selectedUnid": 47203, "sportType": 1,
           "policy": 0, "uuid": run['sportunid']}
    ent = rundata.build_upload_entity(run, ctx)
    ok = []

    # ---- basic numbers ----
    dis, tsec = ent['totalDis'], ent['totalTime']
    ok.append(check("totalDis == %d" % int(km * 1000), dis == int(km * 1000), dis))
    ok.append(check("totalTime == %d s" % int(km * pace * 60),
                    tsec == int(km * pace * 60), tsec))
    ok.append(check("validDis == totalDis", ent['validDis'] == dis))
    ok.append(check("validTime == totalTime", ent['validTime'] == tsec))

    # ---- pace (speed field = min/km * 1000) ----
    want_pace = tsec / 60.0 / km
    got_pace = ent['speed'] / 1000.0
    ok.append(check("speed = pace*1000 (%.3f vs %.3f)" % (got_pace, want_pace),
                    abs(got_pace - want_pace) < 0.02,
                    "renders %s" % fmt_pace(got_pace)))
    ok.append(check("pace renders as %s" % fmt_pace(pace),
                    fmt_pace(got_pace) == fmt_pace(pace)))

    # ---- steps / stride ----
    from_steps = ent['avgStepFreq'] * tsec / 60.0
    ok.append(check("totalSteps ~= cadence*t/60 (%d vs %d)" % (
        ent['totalSteps'], round(from_steps)),
        abs(ent['totalSteps'] - from_steps) <= 1, ent['totalSteps']))
    stride = dis / max(1, ent['totalSteps'])
    ok.append(check("stride %.2f-1.15 m" % stride, 0.70 <= stride <= 1.30, stride))

    # ---- allLocJson 3-layer ----
    outer = json.loads(ent['allLocJson'])
    ok.append(check("allLocJson has useZip", 'useZip' in outer))
    ok.append(check("allLocJson.useZip == False", outer.get('useZip') is False))
    pts = json.loads(outer['allLocJson'])
    ok.append(check("track points >= 20", len(pts) >= 20, len(pts)))
    p0 = pts[0]
    ok.append(check("point has gLat/gLng/lat/lng",
                    all(k in p0 for k in ('gLat', 'gLng', 'lat', 'lng'))))
    # The app carries BD-09 in lat/lng and GCJ-02 in gLat/gLng for the same
    # physical point; here the offset is ~0.006 deg (~950 m).  Identical pairs
    # mean one of them is wrong.
    ok.append(check("point gLat!=lat (BD/GCJ pairs differ)",
                    abs(p0['gLat'] - p0['lat']) > 0.003
                    and abs(p0['gLng'] - p0['lng']) > 0.003,
                    "d=%.5f,%.5f" % (p0['lat'] - p0['gLat'],
                                     p0['lng'] - p0['gLng'])))
    ok.append(check("point monotonic totalTime",
                    all(a['totalTime'] <= b['totalTime'] for a, b in zip(pts, pts[1:]))))
    ok.append(check("point totalDis cumulative to %d" % dis,
                    abs(pts[-1]['totalDis'] - dis) < 1.0, pts[-1]['totalDis']))

    # ---- ten-second buckets ----
    n_exp = max(1, tsec // 10)
    sp, st = ent['speedPerTenSec'], ent['stepsPerTenSec']
    ok.append(check("speedPerTenSec count == %d" % n_exp, len(sp) == n_exp, len(sp)))
    ok.append(check("stepsPerTenSec count == %d" % n_exp, len(st) == n_exp, len(st)))
    dsum = sum(x['distance'] for x in sp)
    ok.append(check("sum(bucket distance) == totalDis (%.1f)" % dsum,
                    abs(dsum - dis) < 2.0, dsum))
    ssum = sum(x['stepsNum'] for x in st)
    ok.append(check("sum(bucket steps) == totalSteps (%d)" % ssum,
                    abs(ssum - ent['totalSteps']) <= 2, ssum))
    ok.append(check("buckets contiguous 10s",
                    all(sp[i]['endTime'] == sp[i + 1]['beginTime']
                        for i in range(len(sp) - 1))))
    ok.append(check("bucket[0].beginTime == startTime",
                    sp[0]['beginTime'] == ent['startTime']))
    ok.append(check("last bucket endTime == stopTime",
                    sp[-1]['endTime'] == ent['stopTime'],
                    "%s vs %s" % (sp[-1]['endTime'], ent['stopTime'])))

    # ---- entity shape (matches src/api/submit.rs) ----
    ok.append(check("no invented fields",
                    not ({'segmentJson', 'prizeName', 'recordUrl', 'roomId'}
                         & set(ent)),
                    sorted({'segmentJson', 'prizeName', 'recordUrl', 'roomId'}
                           & set(ent))))
    ok.append(check("more == false", ent['more'] is False, ent['more']))
    ok.append(check("avgStepFreq == steps/time*60",
                    ent['avgStepFreq'] == max(1, round(ent['totalSteps'] / tsec * 60)),
                    ent['avgStepFreq']))
    ok.append(check("speedPerTenSec id starts at 60000",
                    ent['speedPerTenSec'][0]['id'] == 60000,
                    ent['speedPerTenSec'][0]['id']))

    # ---- five points ----
    fj = json.loads(ent['fivePointJson'])
    ok.append(check("fivePointJson is PointJsonEntity",
                    set(fj.keys()) == {'fivePointJson', 'freedomShowFence',
                                       'geoFencesJson', 'runAreaId', 'useZip'}))
    fpts = json.loads(fj['fivePointJson'])
    ok.append(check("fivePoint non-empty", len(fpts) >= 1, len(fpts)))
    ok.append(check("fivePoint uses lon/glon (not lng)",
                    all('lon' in f and 'glon' in f for f in fpts)))

    # ---- signature ----
    signed = rundata.sign_upload_entity(ent, salt="x" * 32)
    from replica.domain.crypto import UPLOAD_SIGN_FIELDS
    ok.append(check("31 sign fields present",
                    all(k in ent for k in UPLOAD_SIGN_FIELDS),
                    [k for k in UPLOAD_SIGN_FIELDS if k not in ent]))
    ok.append(check("originalSign = sorted link string (getPreSign)",
                    signed['originalSign'].startswith('address=')
                    and '&signature=' not in signed['originalSign']))
    ok.append(check("signature = 32 hex (getSign MD5)",
                    len(signed['signature']) == 32
                    and all(c in '0123456789abcdef' for c in signed['signature'])))
    ok.append(check("originalSign keys == 31 sign fields",
                    len(signed['originalSign'].split('&')) == len(UPLOAD_SIGN_FIELDS),
                    len(signed['originalSign'].split('&'))))
    ok.append(check("uid is int", isinstance(ent['uid'], int)))
    ok.append(check("goalId is None (-> 'null' in sign)",
                    ent['goalId'] is None))
    return all(ok)


def validator_negative():
    """The pre-submit validator must actually fire.

    `validate_run_data` is the last line of defence before a record is signed,
    so a check that silently passes everything is worse than no check.  Each
    case below injects one known-bad shape and asserts it is reported.
    """
    import copy
    from replica.domain.rundata import generate_run_data, validate_run_data

    base = generate_run_data(distance_km=2.5, pace_min_per_km=6.0)
    ok = []

    def probe(name, needle, mutate):
        d = copy.deepcopy(base)
        mutate(d)
        errs = validate_run_data(d)
        hit = any(needle in e for e in errs)
        ok.append(check("validator catches %s" % name, hit, errs or "NOT CAUGHT"))

    def shrink(d):
        locs = d['allLocJson']
        c = (locs[0]['lat'], locs[0]['lng'])
        for p in locs:
            p['lat'] = c[0] + (p['lat'] - c[0]) * 0.5
            p['lng'] = c[1] + (p['lng'] - c[1]) * 0.5

    def same_frame(d):
        wrap = d['locJsonEntity']
        arr = json.loads(wrap['allLocJson']) if isinstance(wrap['allLocJson'], str) \
            else wrap['allLocJson']
        for p in arr:
            p['gLat'] = p['lat']
            p['gLng'] = p['lng']
        wrap['allLocJson'] = json.dumps(arr)

    probe("short geometry", "geometry", shrink)
    probe("BD/GCJ identical", "identical", same_frame)
    probe("isPass false", "not passed",
          lambda d: d['fivePoint'][0].__setitem__('isPass', False))
    probe("bucket sum drift", "bucket distance sum",
          lambda d: d['speed_json'][0].__setitem__(
              'distance', d['speed_json'][0]['distance'] * 3))

    # and the clean record must stay clean
    ok.append(check("clean record has no findings",
                    validate_run_data(base) == [], validate_run_data(base)))
    return ok


def main():
    args = [float(a) for a in sys.argv[1:]] or [1.0, 2.0, 3.0, 5.0]
    results = []
    for seed, km in enumerate(args):
        pace = 6.0 if km <= 2 else 6.5
        results.append((km, validate(km, pace, seed=seed)))

    print("\n=== validator self-check ===")
    neg = validator_negative()
    results.append(("validator", all(neg)))

    print("\n" + "=" * 46)
    for km, ok in results:
        label = "%.2f km" % km if isinstance(km, float) else str(km)
        print("  %-10s: %s" % (label, "ALL PASS" if ok else "*** FAIL ***"))
    print("=" * 46)
    print("RESULT:", "PASS" if all(r[1] for r in results) else "FAIL")
    return 0 if all(r[1] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
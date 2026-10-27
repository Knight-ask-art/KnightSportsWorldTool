"""Point ``type`` must stay inside the domain the official client emits.

The history page hands ``type`` straight to the map SDK
(``RunHistoryDetailPImpl`` -> ``LineBean``), and a value the client never
produces falls through to the SDK's fallback colour -- that is the grey
segment reported upstream as issue #103.

The values this client actually writes are:

    0    normal point          passPointAndBackType() default
    1/2  checkpoint passed     isFixed == 1 ? 2 : 1
    5    start sentinel
    6    end sentinel
    -1   invalid               dropped before drawing

``8`` ("resume running") used to be sprinkled into the last third on a coin
flip even though none of the four implementations returns it.
"""
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from replica.domain.rundata import generate_run_data

CENTER = (22.4108, 112.9408)
LEGAL = {0, 1, 2, 5, 6}


def _run(**kw):
    return generate_run_data(2.5, 6.0, 172, center_lat=CENTER[0],
                             center_lng=CENTER[1], rng=random.Random(7), **kw)


def test_plain_track_keeps_sentinels_and_stays_in_domain():
    run = _run()
    types = [p['type'] for p in run['locPoints']]
    # With no live checkpoints the generator still invents five points along
    # its own route, and those are genuinely on the track, so their 1-marks
    # are consistent with the fivePoint payload that ships next to them.
    assert set(types) <= {0, 1, 5, 6}
    assert types[1] == 5                     # setStartPoint marker
    assert types[-1] == 6                    # end sentinel


def test_checkpoint_marks_only_land_where_the_track_goes():
    seed = _run()
    locs = seed['allLocJson']
    picks = [locs[int(len(locs) * f)] for f in (0.2, 0.5, 0.8)]
    cps = [{'lat': p['lat'], 'lon': p['lng'], 'isMustPoint': i == 0,
            'isFixed': 1 if i == 0 else 0, 'pid': i + 1}
           for i, p in enumerate(picks)]

    run = _run(checkpoints=cps,
               waypoints=[(p['lat'], p['lng']) for p in picks])
    types = [p['type'] for p in run['locPoints']]
    marked = [i for i, t in enumerate(types) if t in (1, 2)]

    assert len(marked) == len(cps), marked
    # the mandatory (isFixed) checkpoint is 2, the others are 1
    assert sorted(types[i] for i in marked) == [1, 1, 2]
    # sentinels keep their own type
    assert 1 not in marked and len(types) - 1 not in marked


def test_no_point_type_outside_the_client_domain():
    run = _run()
    assert {p['type'] for p in run['locPoints']} <= LEGAL

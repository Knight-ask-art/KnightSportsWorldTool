"""Audit detects shape drift without touching the network."""

import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from replica.application.audit import audit_detail, audit_generated  # noqa: E402
from replica.domain.rundata import generate_run_data  # noqa: E402

WAYPOINTS = [(22.4108, 112.9408), (22.4118, 112.9418), (22.4100, 112.9410)]


def _run():
    return generate_run_data(2.0, 6.0, 150, waypoints=WAYPOINTS, closed=True,
                             rng=random.Random(7))


def test_generated_run_passes_the_shape_audit():
    out = audit_generated(_run())
    assert out['summary']['ok'] is True, [c for c in out['checks'] if not c['ok']]
    assert out['summary']['failed'] == 0
    assert out['record']['points'] > 10


def test_empty_points_does_not_crash_the_audit():
    out = audit_generated({'locPoints': [], 'sportTime': 0, 'juli': 0})
    assert out['summary']['ok'] is False
    assert any(not c['ok'] for c in out['checks'])


def test_legacy_key_mode_expects_two_keys():
    run = _run()
    out = audit_generated(run, key_mode='legacy')
    assert out['summary']['ok'] is True
    check = next(c for c in out['checks'] if c['id'] == 'obs.key-count')
    assert check['ok'] is True and 'legacy' in check['label']


def test_steps_field_is_reported():
    run = _run()
    run['locPoints'][1]['steps'] = 999
    out = audit_generated(run)
    ids = {c['id'] for c in out['checks'] if not c['ok']}
    assert 'points.no-steps' in ids
    assert out['summary']['ok'] is False


def test_wrong_pace_is_reported():
    run = _run()
    run['locPoints'][1]['speed'] = 1.0
    out = audit_generated(run)
    failed = {c['id'] for c in out['checks'] if not c['ok']}
    assert 'points.pace' in failed


def test_detail_audit_flags_missing_track():
    out = audit_detail({'ok': True, 'detail': {}, 'track': [],
                        'stats': {}, 'analysis': {}})
    assert out['summary']['ok'] is False
    assert 'detail.track' in {c['id'] for c in out['checks'] if not c['ok']}

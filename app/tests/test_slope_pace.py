"""Terrain coupling uses a supplied elevation template, not invented hills."""

import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from replica.domain.rundata import generate_run_data  # noqa: E402


def test_flat_template_matches_uncoupled_total():
    kw = dict(center_lat=22.4108, center_lng=112.9408,
              altitude_profile=[25.0] * 20)
    plain = generate_run_data(3.0, 6.0, 150, rng=random.Random(13), **kw)
    coupled = generate_run_data(3.0, 6.0, 150, rng=random.Random(13),
                                terrain_coupling=True, **kw)
    assert coupled['sportTime'] == plain['sportTime']
    assert [s['seconds'] for s in coupled['km_splits']] == \
        [s['seconds'] for s in plain['km_splits']]


def test_terrain_still_applies_with_manual_splits():
    hill = [20.0] * 10 + [60.0] * 10
    kw = dict(center_lat=22.4108, center_lng=112.9408,
              altitude_profile=hill, km_times=[360, 360, 360])
    plain = generate_run_data(3.0, 6.0, 150, rng=random.Random(15), **kw)
    coupled = generate_run_data(3.0, 6.0, 150, rng=random.Random(15),
                                terrain_coupling=True, **kw)
    assert coupled['sportTime'] == plain['sportTime'] == 1080
    assert [s['seconds'] for s in coupled['km_splits']] != \
        [s['seconds'] for s in plain['km_splits']]


def test_hill_changes_split_shape_but_not_total():
    hill = [20.0] * 10 + [60.0] * 10
    kw = dict(center_lat=22.4108, center_lng=112.9408,
              altitude_profile=hill)
    plain = generate_run_data(3.0, 6.0, 150, rng=random.Random(14), **kw)
    coupled = generate_run_data(3.0, 6.0, 150, rng=random.Random(14),
                                terrain_coupling=True, **kw)
    assert coupled['sportTime'] == plain['sportTime']
    assert sum(s['distance'] for s in coupled['segment_json']) > 0
    assert coupled['km_splits'][1]['seconds'] != plain['km_splits'][1]['seconds']

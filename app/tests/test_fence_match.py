"""Multiple fences are matched by an actual anchor, never by list order."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from replica.application import points as P  # noqa: E402


def _square(lat, lng, d=0.001):
    return {
        'id': 1 if lat < 22.41 else 2,
        'name': 'south' if lat < 22.41 else 'north',
        'polygon': [(lat - d, lng - d), (lat - d, lng + d),
                    (lat + d, lng + d), (lat + d, lng - d)],
        'center': (lat, lng),
    }


def test_single_fence_is_unique():
    f = _square(22.40, 112.94)
    out = P.match_fence([f])
    assert out['id'] == 1 and out['source'] == 'unique'


def test_anchor_picks_the_matching_fence():
    south, north = _square(22.40, 112.94), _square(22.42, 112.94)
    out = P.match_fence([south, north], anchor=(22.42, 112.94))
    assert out['id'] == 2 and out['source'] == 'anchor'


def test_route_picks_the_matching_fence():
    south, north = _square(22.40, 112.94), _square(22.42, 112.94)
    out = P.match_fence([south, north], route=[(22.4005, 112.9405)])
    assert out['id'] == 1 and out['source'] == 'route'


def test_no_match_is_ambiguous():
    south, north = _square(22.40, 112.94), _square(22.42, 112.94)
    with pytest.raises(P.FenceAmbiguous):
        P.match_fence([south, north], anchor=(22.50, 113.00))

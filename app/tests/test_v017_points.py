"""v0.1.7 checkpoint provenance and 10603 behaviour.

A live probe on 2026-10-30 showed the endpoint returns a random five-of-N draw
per request (1000/1060/2500 m each returned five points, with different
membership and a different ``isFixed`` point).  These tests pin the rules that
follow from that:

* an exact-distance cache is preferred;
* a 10603 may reuse the newest set for the same account/area at another
  distance, but the result must be flagged ``stale`` and carry its distance;
* a recorded cooldown serves cache without touching the network, and raises
  ``PointsRateLimited`` with a countdown when there is no cache;
* submit keeps the distance of the set the operator saw.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from replica.application import points as P  # noqa: E402
from replica.transport.realclient import Account  # noqa: E402


def _key(dist, lat=22.41, lon=112.94):
    return P.cache_key('1', '2', None, lat, lon, dist)


def _row(name='点位1', is_fixed=0):
    return {'lat': 22.41, 'lon': 112.94, 'name': name,
            'isFixed': is_fixed, 'isMustPoint': False, 'seq': None}


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    rows = {}
    monkeypatch.setattr(P, '_load_cache', lambda: rows)
    monkeypatch.setattr(P, 'store_points', lambda *a, **k: None)
    monkeypatch.setattr(P.netsec, 'build_envelope',
                        lambda *a, **k: {'json': 'runec'})
    monkeypatch.setattr(P, '_host', lambda: 'example.test')
    monkeypatch.setenv('JHP_POINTS_RATELIMIT',
                       str(tmp_path / 'points_ratelimit.json'))
    P.clear_rate_limit()
    yield rows
    P.clear_rate_limit()


class _LimitedClient:
    _ns = object()

    def __init__(self):
        self.calls = 0

    def request(self, *a, **k):
        self.calls += 1
        return {'json': {'error': P.RATE_LIMIT_ERR, 'message': 'limited'}}


class _NoNetworkClient:
    _ns = object()

    def __init__(self):
        self.calls = 0

    def request(self, *a, **k):
        self.calls += 1
        raise AssertionError('a cooling-down fetch must not touch the network')


def _acct():
    return Account(name='a', uid='1', token='x', unid=2)


def test_10603_reuses_another_distance_and_marks_it_stale(isolated):
    isolated[_key(2500)] = {'ts': time.time(),
                            'points': [_row('点位7', 1)]}
    client = _LimitedClient()
    meta = P.fetch_points_meta(client, _acct(), 22.41, 112.94,
                               distance_m=1060, force=True)
    assert client.calls == 1
    assert [p['name'] for p in meta['points']] == ['点位7']
    assert meta['distance_m'] == 2500          # where the set came from
    assert meta['stale'] is True
    assert meta['source'] == 'cache-other-distance'
    assert meta['rate_limited'] is True
    assert meta['retry_after_s'] > 0


def test_exact_distance_cache_wins_over_a_newer_other_distance(isolated):
    isolated[_key(1060)] = {'ts': time.time() - 60,
                            'points': [_row('点位3')]}
    isolated[_key(2500)] = {'ts': time.time(),
                            'points': [_row('点位1')]}
    meta = P.fetch_points_meta(_LimitedClient(), _acct(), 22.41, 112.94,
                               distance_m=1060, force=True)
    assert meta['distance_m'] == 1060
    assert meta['stale'] is False
    assert meta['source'] == 'cache'
    assert [p['name'] for p in meta['points']] == ['点位3']


def test_moved_anchor_still_uses_the_same_distance(isolated):
    isolated[_key(2500, lat=22.40, lon=112.90)] = {
        'ts': time.time(), 'points': [_row('点位2')]}
    meta = P.fetch_points_meta(_LimitedClient(), _acct(), 22.41, 112.94,
                               distance_m=2500, force=True)
    assert meta['distance_m'] == 2500
    assert meta['stale'] is False
    assert meta['source'] == 'cache'


def test_cooldown_serves_cache_without_a_request(isolated):
    isolated[_key(2500)] = {'ts': time.time(), 'points': [_row()]}
    P.note_rate_limited()
    client = _NoNetworkClient()
    meta = P.fetch_points_meta(client, _acct(), 22.41, 112.94,
                               distance_m=1060)
    assert client.calls == 0
    assert meta['stale'] is True
    assert meta['retry_after_s'] > 0


def test_cooldown_without_cache_raises_with_a_countdown(isolated):
    P.note_rate_limited()
    client = _NoNetworkClient()
    with pytest.raises(P.PointsRateLimited) as info:
        P.fetch_points_meta(client, _acct(), 22.41, 112.94, distance_m=1060)
    assert client.calls == 0
    assert info.value.retry_after > 0


def test_cooldown_is_scoped_per_account(isolated):
    P.note_rate_limited('1', '2')
    assert P.rate_limit_remaining('1', '2') > 0
    assert P.rate_limit_remaining('9', '8') == 0
    assert P.rate_limit_remaining() > 0      # diagnostic view
    P.clear_rate_limit('1', '2')
    assert P.rate_limit_remaining('1', '2') == 0


def test_force_does_not_bypass_the_cooldown(isolated):
    P.note_rate_limited('1', '2')
    client = _NoNetworkClient()
    with pytest.raises(P.PointsRateLimited) as info:
        P.fetch_points_meta(client, _acct(), 22.41, 112.94,
                            distance_m=1060, force=True)
    assert client.calls == 0
    assert info.value.retry_after > 0


def test_cooldown_is_persisted_outside_the_cache(isolated):
    P.note_rate_limited()
    assert P.rate_limit_remaining() > 0
    P.clear_rate_limit()
    assert P.rate_limit_remaining() == 0


def test_any_distance_lookup_is_opt_in(isolated):
    isolated[_key(2500)] = {'ts': time.time(), 'points': [_row()]}
    assert P.newest_cached_points('1', '2', None, 1060) is None
    hit = P.newest_cached_points('1', '2', None, 1060, any_distance=True)
    assert hit and hit[0]['name'] == '点位1'


def test_submit_keeps_the_displayed_points_distance():
    assert P.points_distance_for_submit(1060, 1.06) == 1060
    assert P.points_distance_for_submit(None, 1.06) == 1060
    assert P.points_distance_for_submit(0, None) == 2000


def test_ui_points_are_normalised_for_submit():
    from replica.adapters.bridge_api import _normalise_ui_points

    raw = [
        {'n': '点位3', 'lat': 22.41, 'lng': 112.94, 'must': True,
         'isFixed': 1, 'position': 3, 'seq': 3},
        {'lat': 'bad', 'lng': 112.94},
        {'lat': 95.0, 'lon': 112.94},
    ]
    out = _normalise_ui_points(raw)
    assert out == [{'lat': 22.41, 'lon': 112.94, 'name': '点位3',
                    'isMustPoint': True, 'isFixed': 1, 'position': 3,
                    'seq': 3}]

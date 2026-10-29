"""Generated runs must have the statistics of the account's real traces.

Reference set: 25 OBS objects from the account (read-only).  The 2026-09/10
runs -- the ones recorded by a phone, not by an older tool build -- sit at

    2.2-3.4 s between samples
    10 s window distance CV 0.19-0.52 (tracks pace: 6'05" -> 0.19, 8'57" -> 0.52)
    step window CV 0.18-0.36
    19-23 m barometric span
    either no stop, or ~6% of the windows under 1 m

The generator used to be a 5.0 s metronome with a window CV of 0.03 and 1 m of
altitude.  These tests pin the new floor; they are fidelity checks, not a
detection model.
"""
import random
import statistics

import pytest

from replica.domain.rundata import generate_run_data, validate_run_data
from replica.transport import obs as OBS

WAYPOINTS = [(22.4108, 112.9408), (22.4118, 112.9418), (22.4100, 112.9410)]


def _run(seed=7, **kw):
    kw.setdefault('waypoints', WAYPOINTS)
    pace = kw.pop('pace_min_per_km', 6.0)
    return generate_run_data(4.0, pace, 150, closed=True,
                             rng=random.Random(seed), **kw)


def _windows(run):
    return [x['distance'] for x in run['speed_json']]


def _gaps(run):
    pts = run['allLocJson']
    return [(b['ts'] - a['ts']) / 1000.0 for a, b in zip(pts, pts[1:])]


def test_sample_gap_matches_a_phone():
    for seed in (1, 4, 9):
        gaps = _gaps(_run(seed))
        assert 2.0 <= min(gaps) < 2.6, (seed, min(gaps))
        assert max(gaps) <= 4.5, (seed, max(gaps))
        assert statistics.median(gaps) == pytest.approx(3.0, abs=0.4)


def test_window_distance_varies_like_gps():
    for seed in (1, 4, 7, 9):
        w = _windows(_run(seed))
        assert statistics.pstdev(w) / statistics.mean(w) >= 0.15, seed
        assert max(w) / min(w) > 2.0, seed


def test_gps_noise_grows_with_pace():
    # Same fixed position error is a larger fraction of a shorter window.
    slow = _windows(_run(5, pace_min_per_km=8.5))
    fast = _windows(_run(5, pace_min_per_km=5.0))
    assert (statistics.pstdev(slow) / statistics.mean(slow)
            > statistics.pstdev(fast) / statistics.mean(fast))


def test_step_column_is_not_a_rescaled_distance_column():
    run = _run(4)
    steps = [x['stepsNum'] for x in run['step_freq_json']]
    assert statistics.pstdev(steps) / statistics.mean(steps) >= 0.15


def test_barometric_span_is_realistic():
    for seed in (1, 4, 9):
        alts = [p['alt'] for p in _run(seed)['allLocJson']]
        assert 12.0 <= max(alts) - min(alts) <= 30.0, seed


def test_every_kilometre_has_a_different_split():
    for seed in (1, 2, 5, 8):
        run = _run(seed)
        laps = OBS.build_laps(run, run['allLocJson'][0]['ts'])
        # OBS laps carry minutes/km; the official adapter multiplies by 60.
        paces = [round(lap['avgPace'] * 60) for lap in laps]
        assert len(paces) >= 3, paces
        assert len(set(paces)) == len(paces), (seed, paces)
        assert max(paces) - min(paces) >= 10, (seed, paces)


def test_laps_differ_because_the_distance_profile_differs():
    run = _run(6)
    laps = OBS.build_laps(run, run['allLocJson'][0]['ts'])
    # Distance per elapsed second is what the lap table reads; if the
    # cumulative distance were still linear in time every lap would match.
    rates = [lap['distance'] / lap['duration'] for lap in laps]
    assert max(rates) - min(rates) > 0.15, rates


def test_stops_exist_but_stay_below_ten_percent():
    shares = []
    for seed in range(1, 13):
        w = _windows(_run(seed))
        shares.append(sum(1 for x in w if x < 1.0) / len(w))
    assert any(x > 0 for x in shares), shares
    assert max(shares) <= 0.10, shares


def test_totals_are_still_exact():
    run = _run(3)
    assert sum(_windows(run)) == pytest.approx(run['juli'], abs=0.5)
    steps = [x['stepsNum'] for x in run['step_freq_json']]
    assert sum(steps) == run['total_steps']
    pts = run['allLocJson']
    assert pts[-1]['ts'] - pts[0]['ts'] == run['sportTime'] * 1000
    assert pts[-1].get('alt') == pts[-1]['alt']
    assert validate_run_data(run) == []


def test_obs_laps_cover_the_run_exactly():
    run = _run(6)
    laps = OBS.build_laps(run, run['allLocJson'][0]['ts'])
    assert sum(lap['distance'] for lap in laps) == pytest.approx(run['juli'], abs=0.01)
    assert sum(lap['duration'] for lap in laps) == pytest.approx(
        run['sportTime'], abs=3)
    assert all(lap['avgPace'] for lap in laps)


def test_obs_lap_pace_is_minutes_per_km():
    # The official adapter renders avgPace as minutes: (int)(avgPace * 60).
    # Storing seconds made the phone show 360'00" for a 6'00" lap.
    run = _run(1)
    laps = OBS.build_laps(run, run['allLocJson'][0]['ts'])
    assert 4.0 < laps[0]['avgPace'] < 12.0, laps[0]['avgPace']
    expected = (run['sportTime'] / 60.0) / (run['juli'] / 1000.0)
    assert abs(laps[0]['avgPace'] - expected) < 1.0


def test_distance_follows_the_profile_not_the_clock():
    run = _run(1)
    pts = run['locPoints']
    total = run['juli']
    assert pts[-1]['totalDis'] == total
    assert all(b['totalDis'] >= a['totalDis']
               for a, b in zip(pts, pts[1:]))
    # A stop has to leave a plateau in the cumulative distance column.
    window = [p['totalDis'] for p in pts]
    flat = sum(1 for a, b in zip(window, window[1:]) if b - a < 0.2)
    assert flat >= 1, flat


def test_edited_km_times_are_honoured():
    wanted = [360, 400, 340, 350]
    run = _run(7, km_times=wanted)
    assert [s['seconds'] for s in run['km_splits']] == wanted
    assert run['sportTime'] == sum(wanted)
    laps = OBS.build_laps(run, run['allLocJson'][0]['ts'])
    assert [lap['duration'] for lap in laps] == pytest.approx(wanted, abs=2)


def test_km_times_must_match_the_kilometre_count():
    with pytest.raises(ValueError):
        _run(1, km_times=[360, 360])
    with pytest.raises(ValueError):
        _run(1, km_times=[360, 0])


def test_run_area_id_is_carried_into_the_point_entity():
    run = _run(1, run_area_id=22)
    assert run['fivePointJson']['runAreaId'] == 22
    assert run['fixed_point_json']['runAreaId'] == 22
    assert _run(1)['fivePointJson']['runAreaId'] == -1


def test_pass_radius_matches_the_official_25m():
    from replica.domain.rundata import _passes_near

    pts = [{'lat': 22.41, 'lng': 112.94}]
    assert _passes_near(pts, 22.41 + 24.0 / 110540, 112.94, 25.0)
    assert not _passes_near(pts, 22.41 + 30.0 / 110540, 112.94, 25.0)


def test_sequential_five_point_keeps_school_order_and_raw_isfixed():
    cps = [
        {'lat': 22.4108, 'lon': 112.9408, 'isMustPoint': False,
         'isFixed': 0, 'name': '点位1', 'seq': 1},
        {'lat': 22.4118, 'lon': 112.9418, 'isMustPoint': True,
         'isFixed': 1, 'name': '点位2', 'seq': 2},
        {'lat': 22.4100, 'lon': 112.9410, 'isMustPoint': False,
         'isFixed': 0, 'name': '点位3', 'seq': 3},
    ]
    run = generate_run_data(
        1.0, 6.0, 150,
        waypoints=[(c['lat'], c['lon']) for c in cps],
        checkpoints=cps, sequential=True, rng=random.Random(3))
    assert [fp['pointName'] for fp in run['fivePoint']] == [
        '点位1', '点位2', '点位3']
    assert [fp['isFixed'] for fp in run['fivePoint']] == [0, 1, 0]
    assert all(fp['isPass'] for fp in run['fivePoint'])


def test_preview_returns_the_split_table(tmp_path):
    from replica.adapters.bridge_api import ReplicaApi
    api = ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                     str(tmp_path))
    r = api.preview_route(4.0, 6.0, 150, waypoints=WAYPOINTS,
                          km_times=[360, 400, 340, 350])
    assert r['code'] == 200, r
    assert [s['seconds'] for s in r['data']['splits']] == [360, 400, 340, 350]
    assert r['data']['sportTime'] == 1450

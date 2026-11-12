"""Bridge API tests for route memory; no network or submit."""

import json
import os
import sys
import threading
import time
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from replica.adapters import bridge_api  # noqa: E402
from replica.adapters.bridge_api import ReplicaApi, _clean_route  # noqa: E402
from replica.application import readonly as RO  # noqa: E402
from replica.application import route_memory as RM  # noqa: E402


def _api(tmp_path):
    return ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                      str(tmp_path))


def _acct(token='tok-secret-value-12345'):
    return SimpleNamespace(
        uid='1001',
        unid='2002',
        token=token,
        center_lat=22.4100,
        center_lng=112.9400,
    )


def _square_track(base_lat=22.4100, base_lng=112.9400, d=0.001, per_edge=8):
    corners = [
        (base_lat - d, base_lng - d),
        (base_lat - d, base_lng + d),
        (base_lat + d, base_lng + d),
        (base_lat + d, base_lng - d),
    ]
    pts = []
    for idx in range(4):
        a = corners[idx]
        b = corners[(idx + 1) % 4]
        for step in range(per_edge):
            t = step / float(per_edge)
            pts.append({
                'lat': a[0] + (b[0] - a[0]) * t,
                'lng': a[1] + (b[1] - a[1]) * t,
            })
    pts.append({'lat': corners[0][0], 'lng': corners[0][1]})
    return pts


def _seed_ok_memory(api, acct):
    pts = [[p['lat'], p['lng']] for p in _square_track()]
    tracks = [
        {
            'rrid': str(10 + i),
            'line': pts,
            'distance_m': 1200.0,
            'duration_s': 420,
            'start_ms': 1700000000000 + i * 86400000,
            'is_cheat': 0,
            'complete': True,
            'source': 'server',
        }
        for i in range(5)
    ]
    doc = RM.learn_from_tracks(
        tracks,
        f'{acct.uid}:{acct.unid}',
        campus={'center': [22.4100, 112.9400], 'fence_hash': ''},
    )
    RM.save(api.store_dir, acct.uid, acct.unid, doc)
    return doc


def test_clean_route_accepts_learned():
    cleaned = _clean_route({
        'name': '操场记忆',
        'type': 'learned',
        'line': [[22.4100, 112.9400], [22.4105, 112.9405]],
        'closed': True,
        'saved_at': '2026-11-12',
    })
    assert cleaned is not None
    assert cleaned['type'] == 'learned'
    assert cleaned['closed'] is True


def test_methods_require_token(tmp_path):
    api = _api(tmp_path)
    api._real_account = lambda: None
    for res in (
        api.real_route_memory_status(),
        api.real_route_memory_learn(),
        api.real_route_memory_suggest(),
        api.real_route_memory_apply('tpl1'),
        api.real_route_memory_clear(),
        api.real_route_memory_save('tpl1'),
    ):
        assert res['code'] != 200
        assert 'token' in res['message'].lower()

    api._real_account = lambda: _acct(token='')
    res = api.real_route_memory_status()
    assert res['code'] != 200


def test_learn_happy_path_filters_receipts_and_failures(tmp_path, monkeypatch):
    api = _api(tmp_path)
    acct = _acct()
    api._real_account = lambda: acct

    receipts_dir = os.path.join(api.store_dir, 'submission_receipts')
    os.makedirs(receipts_dir, exist_ok=True)
    with open(os.path.join(receipts_dir, '1001__2002.json'), 'w', encoding='utf-8') as f:
        json.dump({'tool-uuid-1': {'uuid': 'tool-uuid-1', 'rrid': '99'}}, f)

    rows = [
        {'rrid': i, 'uuid': f'srv-{i}', 'distance_m': 1200.0, 'duration_s': 420,
         'start_ms': 1700000000000 + i * 1000, 'complete': True}
        for i in range(1, 6)
    ]
    rows.append({'rrid': 99, 'uuid': 'tool-uuid-1', 'distance_m': 1200.0,
                 'duration_s': 420, 'start_ms': 1700000099000, 'complete': True})
    rows.append({'rrid': 88, 'uuid': 'bad-uuid', 'distance_m': 1200.0,
                 'duration_s': 420, 'start_ms': 1700000088000, 'complete': True})

    track_pts = _square_track()
    sleeps = []
    monkeypatch.setattr(bridge_api.time, 'sleep', lambda s: sleeps.append(s))
    monkeypatch.setattr(RO, 'recent_records', lambda rc, a, limit=50: {
        'ok': True,
        'records': rows,
    })

    def fake_detail(rc, a, rrid, uuid=None):
        if rrid == 88:
            return {'ok': False, 'error': 'detail failed'}
        return {
            'ok': True,
            'detail': {
                'rrid': rrid,
                'uuid': f'tool-uuid-1' if rrid == 99 else f'srv-{rrid}',
                'distance_m': 1200.0,
                'duration_s': 420,
                'start_ms': 1700000000000 + int(rrid) * 1000,
                'is_cheat': 0,
                'complete': True,
            },
            'track': track_pts,
        }

    monkeypatch.setattr(RO, 'record_detail', fake_detail)

    out = api.real_route_memory_learn(limit=50, force=True, wait=True)
    assert out['code'] == 200
    data = out['data']
    assert data['cached'] is False
    assert data['summary']['seen'] == 7
    assert data['summary']['usable'] == 5
    assert data['summary']['skipped'] == 2
    assert data['summary']['status'] == 'ok'
    assert len(data['skipped']) == 1
    assert data['skipped'][0]['rrid'] == '88'
    assert len(data['templates']) >= 1
    assert 'line' not in data['templates'][0]
    assert sleeps == [0.15] * (len(rows) - 1)
    assert data['progress']['phase'] == 'done'
    assert data['progress']['running'] is False
    assert acct.token not in json.dumps(out, ensure_ascii=False)

    prog_path = os.path.join(api.store_dir, 'route_memory', 'progress.json')
    assert os.path.exists(prog_path)


def test_learn_24h_cache_and_force(tmp_path, monkeypatch):
    api = _api(tmp_path)
    acct = _acct()
    api._real_account = lambda: acct
    _seed_ok_memory(api, acct)

    called = {'recent': 0}

    def fail_if_called(*a, **k):
        called['recent'] += 1
        return {'ok': True, 'records': []}

    monkeypatch.setattr(RO, 'recent_records', fail_if_called)

    cached_out = api.real_route_memory_learn(limit=50, force=False, wait=True)
    assert cached_out['code'] == 200
    assert cached_out['data']['cached'] is True
    assert called['recent'] == 0

    monkeypatch.setattr(bridge_api.time, 'sleep', lambda s: None)
    forced_out = api.real_route_memory_learn(limit=50, force=True, wait=True)
    assert forced_out['code'] == 200
    assert forced_out['data']['cached'] is False
    assert called['recent'] == 1


def test_learn_async_barrier_and_single_flight(tmp_path, monkeypatch):
    api = _api(tmp_path)
    acct = _acct()
    api._real_account = lambda: acct

    entered = threading.Event()
    release = threading.Event()
    calls = {'recent': 0}

    def slow_recent(rc, a, limit=50):
        calls['recent'] += 1
        entered.set()
        release.wait(timeout=5.0)
        return {'ok': True, 'records': [{'rrid': 1, 'distance_m': 1200.0,
                                         'duration_s': 420, 'start_ms': 1700000001000,
                                         'complete': True}]}

    monkeypatch.setattr(RO, 'recent_records', slow_recent)
    monkeypatch.setattr(RO, 'record_detail', lambda rc, a, rrid, uuid=None: {
        'ok': True,
        'detail': {'rrid': rrid, 'distance_m': 1200.0, 'duration_s': 420,
                   'start_ms': 1700000001000, 'is_cheat': 0, 'complete': True},
        'track': _square_track(),
    })

    first = api.real_route_memory_learn(limit=10, force=True, wait=False)
    assert first['code'] == 200
    assert first['data']['started'] is True
    assert first['data']['progress']['running'] is True

    assert entered.wait(timeout=3.0)
    second = api.real_route_memory_learn(limit=10, force=True, wait=False)
    assert second['code'] == 200
    assert second['data']['started'] is False
    assert calls['recent'] == 1

    release.set()
    deadline = time.time() + 5.0
    status = None
    while time.time() < deadline:
        status = api.real_route_memory_status()
        if not status['data']['progress']['running']:
            break
        time.sleep(0.02)

    assert status is not None
    assert status['data']['progress']['running'] is False
    assert status['data']['progress']['phase'] == 'done'
    assert status['data']['summary']['usable'] == 1


def test_learn_async_error_sets_progress_error(tmp_path, monkeypatch):
    api = _api(tmp_path)
    acct = _acct()
    api._real_account = lambda: acct

    def boom(rc, a, limit=50):
        raise RuntimeError('upstream failure token=supersecret')

    monkeypatch.setattr(RO, 'recent_records', boom)

    out = api.real_route_memory_learn(limit=10, force=True, wait=False)
    assert out['code'] == 200
    assert out['data']['started'] is True

    deadline = time.time() + 5.0
    status = None
    while time.time() < deadline:
        status = api.real_route_memory_status()
        if not status['data']['progress']['running']:
            break
        time.sleep(0.02)

    assert status is not None
    prog = status['data']['progress']
    assert prog['phase'] == 'error'
    assert prog['error']
    assert 'supersecret' not in prog['error']


def test_suggest_empty_weak_and_fence_shapes(tmp_path, monkeypatch):
    api = _api(tmp_path)
    acct = _acct()
    api._real_account = lambda: acct
    monkeypatch.setattr(api, 'real_fetch_fence',
                        lambda: bridge_api._fail('no fence'))

    empty_out = api.real_route_memory_suggest(km=2.5)
    assert empty_out['code'] == 200
    assert empty_out['data']['status'] == 'empty'
    assert empty_out['data']['candidates'] == []
    assert empty_out['data']['message']

    # Seed weak memory (only 2 usable tracks)
    pts = [[p['lat'], p['lng']] for p in _square_track()]
    weak_doc = RM.learn_from_tracks(
        [{'rrid': str(i), 'line': pts, 'distance_m': 1200.0, 'duration_s': 420,
          'start_ms': 1700000000000 + i * 1000, 'is_cheat': 0, 'complete': True}
         for i in range(2)],
        f'{acct.uid}:{acct.unid}',
    )
    RM.save(api.store_dir, acct.uid, acct.unid, weak_doc)
    weak_out = api.real_route_memory_suggest(km=2.5)
    assert weak_out['code'] == 200
    assert weak_out['data']['status'] == 'weak'
    assert weak_out['data']['candidates'] == []
    assert weak_out['data']['message']

    # Seed ok memory and test multiple fence shapes + checkpoint aliases
    _seed_ok_memory(api, acct)
    outer_ring_dicts = [
        {'lat': 22.405, 'lng': 112.935},
        {'lat': 22.405, 'lng': 112.945},
        {'lat': 22.415, 'lng': 112.945},
        {'lat': 22.415, 'lng': 112.935},
    ]
    outer_ring_pairs = [[p['lat'], p['lng']] for p in outer_ring_dicts]
    points = [{'glat': 22.411, 'glon': 112.941, 'must': True, 'n': '东门'}]

    for fence_arg in (
        outer_ring_dicts,
        [outer_ring_pairs],
        {'polygons': [outer_ring_pairs]},
    ):
        res = api.real_route_memory_suggest(km=2.5, points=points, fence=fence_arg)
        assert res['code'] == 200
        assert res['data']['status'] == 'ok'
        assert len(res['data']['candidates']) == 1
        assert res['data']['candidates'][0]['fence_ok'] is True

    # Ensure the fetched fence is used when fence=None
    monkeypatch.setattr(api, 'real_fetch_fence', lambda: bridge_api._ok({
        'polygons': [[{'lat': p[0], 'lon': p[1]} for p in outer_ring_pairs]],
        'center': {'lat': 22.4100, 'lon': 112.9400},
    }))
    res_auto = api.real_route_memory_suggest(km=2.5, points=points, fence=None)
    assert res_auto['code'] == 200
    assert len(res_auto['data']['candidates']) == 1

    # Faraway fence excludes template cleanly
    far_fence = [[
        [30.0, 120.0],
        [30.0, 120.01],
        [30.01, 120.01],
        [30.01, 120.0],
    ]]
    res_far = api.real_route_memory_suggest(km=2.5, fence=far_fence)
    assert res_far['code'] == 200
    assert res_far['data']['candidates'] == []
    assert res_far['data']['message']


def test_apply_save_dedupe_and_clear(tmp_path):
    api = _api(tmp_path)
    acct = _acct()
    api._real_account = lambda: acct

    bad_apply = api.real_route_memory_apply('missing-id', km=2.5)
    assert bad_apply['code'] != 200
    assert '22.41' not in bad_apply['message']

    doc = _seed_ok_memory(api, acct)
    tpl_id = doc['templates'][0]['id']

    applied = api.real_route_memory_apply(
        tpl_id,
        km=2.5,
        points=[{'lat': 22.411, 'lon': 112.941, 'isMustPoint': True, 'name': 'A'}],
    )
    assert applied['code'] == 200
    assert applied['data']['ok'] is True
    assert len(applied['data']['waypoints']) >= 4
    assert applied['data']['laps'] >= 1

    bad_save = api.real_route_memory_save('missing-id')
    assert bad_save['code'] != 200

    saved1 = api.real_route_memory_save(tpl_id)
    assert saved1['code'] == 200
    assert saved1['data']['count'] == 1
    assert saved1['data']['routes'][0]['type'] == 'learned'

    saved2 = api.real_route_memory_save(tpl_id)
    assert saved2['code'] == 200
    assert saved2['data']['count'] == 1

    got_routes = api.real_get_routes()
    assert got_routes['code'] == 200
    assert got_routes['data']['routes'][0]['type'] == 'learned'

    cleared = api.real_route_memory_clear()
    assert cleared['code'] == 200
    assert cleared['data']['cleared'] is True

    after_status = api.real_route_memory_status()
    assert after_status['code'] == 200
    assert after_status['data']['summary']['status'] == 'empty'
    assert after_status['data']['progress']['phase'] == 'idle'
    assert after_status['data']['progress']['running'] is False


def test_learn_error_is_chinese_and_redacts_bare_token(tmp_path, monkeypatch):
    api = _api(tmp_path)
    acct = _acct(token='tok-secret-value-12345')
    api._real_account = lambda: acct

    def boom(*args, **kwargs):
        raise RuntimeError('upstream reset tok-secret-value-12345')

    monkeypatch.setattr(RO, 'recent_records', boom)
    out = api.real_route_memory_learn(force=True, wait=True)
    assert out['code'] != 200
    assert out['message'].startswith('路线学习失败')
    assert 'tok-secret-value-12345' not in out['message']
    assert '***' in out['message']

    prog = api.real_route_memory_status()['data']['progress']
    assert prog['phase'] == 'error'
    assert prog['error'].startswith('路线学习失败')
    assert 'tok-secret-value-12345' not in prog['error']


def test_progress_is_hidden_for_another_account(tmp_path):
    api = _api(tmp_path)
    acct_a = _acct()
    acct_b = SimpleNamespace(uid='2002', unid='3003', token='tok-b',
                             center_lat=22.4100, center_lng=112.9400)
    api._real_account = lambda: acct_a
    api._rm_set_progress(running=False, phase='error', error='A failed',
                         owner='1001:2002')
    assert api.real_route_memory_status()['data']['progress']['phase'] == 'error'

    api._real_account = lambda: acct_b
    prog = api.real_route_memory_status()['data']['progress']
    assert prog['phase'] == 'idle'
    assert prog['error'] == ''


def test_two_wait_true_learns_do_not_overlap(tmp_path, monkeypatch):
    api = _api(tmp_path)
    acct = _acct()
    api._real_account = lambda: acct
    guard = threading.Lock()
    state = {'now': 0, 'max': 0}

    def slow(rc, a, limit=50):
        with guard:
            state['now'] += 1
            state['max'] = max(state['max'], state['now'])
        time.sleep(0.05)
        with guard:
            state['now'] -= 1
        return {'ok': True, 'records': []}

    monkeypatch.setattr(RO, 'recent_records', slow)
    outs = []

    def call():
        outs.append(api.real_route_memory_learn(force=True, wait=True))

    threads = [threading.Thread(target=call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5.0)
    assert state['max'] == 1
    assert len(outs) == 2
    assert all(o['code'] == 200 for o in outs)

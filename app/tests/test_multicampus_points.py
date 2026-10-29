"""Multi-campus / no-fence / empty-point behaviour.

A school is allowed to have no geofence: the official client treats an empty
fence list as "always in area".  Empty checkpoints are different: the normal
``/distance/`` path has no free-run branch, so submit blocks unless the
operator explicitly skips checkpoints.  These tests pin the position-0 run
area default, all-fence OR semantics, and the conservative empty-point gate.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from replica.adapters.bridge_api import ReplicaApi  # noqa: E402
from replica.adapters import bridge_api as BAPI  # noqa: E402
from replica.application import points as P  # noqa: E402
from replica.application import policy as POL  # noqa: E402
from replica.transport.realclient import Account  # noqa: E402


def _poly(lat, lon):
    return [(lat, lon), (lat + 0.001, lon),
            (lat + 0.001, lon + 0.001), (lat, lon + 0.001)]


class _FakeClient:
    def __init__(self, data, error=10000):
        self.data = data
        self.error = error

    def request(self, acct, path, payload=None, retries=0, method='POST'):
        return {'json': {'error': self.error, 'data': self.data,
                         'message': ''}}


def test_empty_fence_list_is_not_an_error():
    f = P.fetch_fence(None, Account(name='a', uid='1', token='x'),
                      policy_data={'geoFence': {'geoFences': []}})
    assert f['polygon'] == []
    assert f['polygons'] == []
    assert f['center'] is None


def test_policy_fence_returns_every_polygon_not_a_campus_guess():
    policy = {'geoFence': {'geoFences': [
        {'id': '1', 'name': '东校区', 'points': [
            {'glat': a, 'glon': b} for a, b in _poly(30.0, 120.0)]},
        {'id': '2', 'name': '西校区', 'points': [
            {'glat': a, 'glon': b} for a, b in _poly(31.0, 121.0)]},
    ]}}
    acct = Account(name='a', uid='1', token='x', campusName='西校区')
    f = P.fetch_fence(None, acct, policy_data=policy)
    assert len(f['polygons']) == 2
    assert f['ambiguous'] is True
    assert f['center'] is None


def test_run_area_uses_position_zero_and_ignores_select():
    policy = {'runAreaModels': [
        {'id': 11, 'name': '东跑区', 'valid': True, 'select': False,
         'fenceIds': [1]},
        {'id': 22, 'name': '西跑区', 'valid': True, 'select': True,
         'fenceIds': [20, 21]},
    ]}
    assert POL.select_run_area(policy)['id'] == 11
    assert POL.select_run_area(policy, 0)['id'] == 11
    assert POL.select_run_area(policy, 1)['id'] == 22
    assert POL.select_run_area(policy, -1) is None
    assert POL.select_run_area(policy, 99) is None


def test_run_area_auto_matches_each_accounts_campus():
    """Account A (广软) and account B (广州) must not inherit each other."""
    policy = {'runAreaModels': [
        {'id': 1, 'name': '广软学校 江门校区', 'valid': True,
         'fenceIds': [1]},
        {'id': 2, 'name': '广州学校 天河校区', 'valid': True,
         'fenceIds': [2]},
    ]}
    assert POL.select_run_area(
        policy, campus_name='广软学校 江门校区')['id'] == 1
    assert POL.select_run_area(
        policy, campus_name='广州学校 天河校区')['id'] == 2
    # schoolName is tried when campusName does not match.
    assert POL.select_run_area(
        policy, campus_name='大学城', school_name='广州学校')['id'] == 2
    # No matching name -> official default position 0.
    assert POL.select_run_area(policy, campus_name='未知学校')['id'] == 1


def test_run_area_keeps_all_fence_ids():
    policy = {
        'runAreaModels': [
            {'id': 11, 'name': '东跑区', 'valid': True, 'select': False,
             'fenceIds': [1]},
            {'id': 22, 'name': '西跑区', 'valid': True, 'select': True,
             'fenceIds': [20, 21]},
        ],
        'geoFence': {'geoFences': [
            {'id': 1, 'name': '东校区', 'points': [
                {'glat': a, 'glon': b} for a, b in _poly(30.0, 120.0)]},
            {'id': 20, 'name': '西校区A', 'points': [
                {'glat': a, 'glon': b} for a, b in _poly(31.0, 121.0)]},
            {'id': 21, 'name': '西校区B', 'points': [
                {'glat': a, 'glon': b} for a, b in _poly(32.0, 122.0)]},
        ]},
    }
    area = POL.select_run_area(policy, 1)
    assert area['id'] == 22
    assert area['fence_ids'] == [20, 21]

    class _NoNetwork:
        def request(self, *a, **k):
            raise AssertionError('policy already carried the fence')

    acct = Account(name='a', uid='1', token='x', campusName='西校区')
    fence = P.fetch_fence(_NoNetwork(), acct, policy_data=policy,
                          run_area=area)
    assert [f['id'] for f in fence['fences']] == [20, 21]
    assert len(fence['polygons']) == 2


def test_invalid_default_area_is_returned_for_the_caller_to_block():
    # Official selectedPosition defaults to 0 even when that area is invalid;
    # startSport then refuses with "跑区有效时间已更新，请重新选择".
    policy = {'runAreaModels': [
        {'id': 1, 'name': '东跑区', 'valid': False, 'select': True},
        {'id': 2, 'name': '西跑区', 'valid': True, 'select': False},
    ]}
    assert POL.select_run_area(policy)['id'] == 1
    assert POL.find_run_area(policy, 1) is None


def test_missing_valid_flag_is_unknown_not_invalid():
    policy = {'runAreaModels': [
        {'id': 1, 'name': '东跑区', 'select': True},
        {'id': 2, 'name': '西跑区', 'valid': True},
    ]}
    assert POL.run_area_models(policy)[0]['valid'] is None
    assert POL.select_run_area(policy)['id'] == 1


def test_missing_run_area_models_selects_nothing():
    assert POL.select_run_area({'runAreaModels': []}) is None
    assert POL.select_run_area({}) is None


def test_fetch_points_honours_the_selected_run_area(tmp_path, monkeypatch):
    import replica.transport.realclient as RC

    api = _api(tmp_path)
    api.real_set_session(uid='1', token='x', lat=22.41, lon=112.94,
                         campusName='某校区', schoolName='某大学', unid=9)
    policy = {'runAreaModels': [
        {'id': 1, 'name': '东跑区', 'valid': True, 'select': True,
         'fenceIds': [1]},
        {'id': 2, 'name': '西跑区', 'valid': True, 'select': False,
         'fenceIds': [2]},
    ]}
    monkeypatch.setattr(api, '_fetch_run_policy', lambda *a, **k: policy)
    monkeypatch.setattr(RC, 'RealClient', lambda *a, **k: object())
    seen = {}

    def fake_fence(acct, client=None, policy_data=None, run_area=None,
                   anchor=None):
        seen['area'] = run_area
        return {'polygon': [], 'polygons': [], 'center': None, 'name': ''}

    monkeypatch.setattr(api, '_ensure_fence', fake_fence)
    monkeypatch.setattr(P, 'fetch_points', lambda *a, **k: [])
    j = api.real_fetch_points(glat=22.41, glon=112.94, distance_m=2000,
                              run_area_id=2)
    assert j['code'] == 200, j
    assert seen['area']['id'] == 2
    assert j['data']['run_area']['id'] == 2

    # An id the server does not offer (or marks invalid) falls back to the
    # account's own area instead of raising.
    j = api.real_fetch_points(glat=22.41, glon=112.94, distance_m=2000,
                              run_area_id=99)
    assert j['data']['run_area']['id'] == 1
    assert any('所选跑区不可用' in n for n in j['data']['notes'])


def test_fence_ids_drop_every_other_campus():
    fences = P._parse_fences([
        {'id': 1, 'name': '东校区', 'points': [
            {'glat': a, 'glon': b} for a, b in _poly(30.0, 120.0)]},
        {'id': 20, 'name': '西校区', 'points': [
            {'glat': a, 'glon': b} for a, b in _poly(31.0, 121.0)]},
    ])
    kept = P.filter_fences_by_area(fences, {'id': 22, 'fence_ids': [20]})
    assert [f['id'] for f in kept] == [20]
    # String ids from the server still match ints from the policy.
    kept = P.filter_fences_by_area(fences, {'id': 22, 'fence_ids': ['20']})
    assert [f['id'] for f in kept] == [20]


def test_run_area_owns_no_fence_means_no_fence():
    """Official filter returns [] and `isInArea` then treats it as inside.

    Using another run area's polygon here would submit at the wrong campus.
    """
    policy = {
        'runAreaModels': [
            {'id': 11, 'name': '东跑区', 'valid': True, 'select': True,
             'fenceIds': [1]},
        ],
        'geoFence': {'geoFences': [
            {'id': 1, 'name': '东校区', 'points': [
                {'glat': a, 'glon': b} for a, b in _poly(30.0, 120.0)]},
        ]},
    }
    acct = Account(name='a', uid='1', token='x', campusName='西校区')
    area = {'id': 22, 'name': '西跑区', 'fence_ids': []}
    fence = P.fetch_fence(_FakeClient(policy['geoFence']), acct,
                          policy_data=policy, run_area=area)
    assert fence['polygon'] == []
    assert fence['center'] is None

    # Unknown fence id: a server inconsistency, still not another campus.
    area = {'id': 22, 'name': '西跑区', 'fence_ids': [99]}
    fence = P.fetch_fence(_FakeClient(policy['geoFence']), acct,
                          policy_data=policy, run_area=area)
    assert fence['polygon'] == []


def test_fetch_run_policy_never_mixes_modes(tmp_path, monkeypatch):
    """Areas and fence must come from one reply, never spliced across modes."""
    api = _api(tmp_path)
    acct = Account(name='a', uid='1', token='x', unid=9)
    calls = []

    def fake_fetch_policy(client, account, run_mode=1, **kw):
        calls.append(run_mode)
        if run_mode == 1:
            return {'runAreaModels': [{'id': 1, 'name': '东', 'fenceIds': [1]}]}
        return {'geoFence': {'geoFences': [{'id': 9, 'name': '西'}]}}

    monkeypatch.setattr(POL, 'fetch_policy', fake_fetch_policy)
    data = api._fetch_run_policy(object(), acct)
    assert calls == [1, 2]
    # Mode 2 is preferred because only it carries the fence, and the mode 1
    # run areas must NOT be attached to it.
    assert 'geoFence' in data
    assert 'runAreaModels' not in data

    calls.clear()

    def complete(client, account, run_mode=1, **kw):
        calls.append(run_mode)
        return {'runAreaModels': [{'id': 1, 'name': '东'}],
                'geoFence': {'geoFences': []},
                'freezeRunTime': 0,
                'runFaceCheckConfigDTO': {'enable': False}}

    monkeypatch.setattr(POL, 'fetch_policy', complete)
    assert api._fetch_run_policy(object(), acct)['runAreaModels']
    # A complete reply ends the search: no second mode request.
    assert calls == [1]

    calls.clear()

    def reject_then_complete(client, account, run_mode=1, **kw):
        calls.append(run_mode)
        if run_mode == 1:
            raise RuntimeError('policy rejected')
        return {'runAreaModels': [{'id': 2, 'name': '西'}],
                'geoFence': {'geoFences': [{'id': 2}]}}

    monkeypatch.setattr(POL, 'fetch_policy', reject_then_complete)
    data = api._fetch_run_policy(object(), acct)
    assert calls == [1, 2]
    assert data['runAreaModels'][0]['id'] == 2
    assert data['geoFence']['geoFences'][0]['id'] == 2


def test_policy_without_fence_falls_back_only_when_section_is_missing():
    class _NoNetwork:
        def request(self, *a, **k):
            raise AssertionError('explicit empty fence must not hit the network')

    acct = Account(name='a', uid='1', token='x')
    # `geoFences: []` is an explicit "no fence": authoritative.
    fence = P.fetch_fence(_NoNetwork(), acct,
                          policy_data={'geoFence': {'geoFences': []}})
    assert fence['polygon'] == []
    assert fence['source'] == 'policy-empty'

    # A JSON null (live frozen account) is not a section.  The school's ring
    # still exists on getGeoFenceForRun, so use it instead of claiming "none".
    class _Legacy:
        def request(self, acct_, path, payload=None, retries=0):
            assert path.endswith('getGeoFenceForRun'), path
            return {'json': {'error': 10000, 'message': '', 'data': {
                'geoFences': [{'id': 1939, 'name': '学校', 'points': [
                    {'glat': 22.4098, 'glon': 112.9399},
                    {'glat': 22.4098, 'glon': 112.9416},
                    {'glat': 22.4116, 'glon': 112.9416},
                ]}]}}}

    fence = P.fetch_fence(_Legacy(), acct, policy_data={'geoFence': None})
    assert fence['source'] == 'legacy-fallback'
    assert len(fence['polygons']) == 1
    assert fence['center'][0] == pytest.approx(22.4104, abs=1e-3)
    assert fence['center'][1] == pytest.approx(112.9410, abs=1e-3)


def test_json_null_fence_is_not_a_section():
    assert POL.has_fence_section({'geoFence': None}) is False
    assert POL.has_fence_section({'geoFence': 'null'}) is False
    assert POL.has_fence_section({'runAreaModels': []}) is False
    assert POL.has_fence_section({'geoFence': {'geoFences': []}}) is True


def test_rate_limited_fetch_reuses_cache_from_a_different_anchor(monkeypatch):
    import time

    cached = {'ts': time.time(), 'points': [
        {'lat': 22.41, 'lon': 112.94, 'name': '点位2', 'isFixed': 1,
         'isMustPoint': False, 'seq': None},
    ]}
    monkeypatch.setattr(
        P, '_load_cache',
        lambda: {'1|2||23.4732|116.7967|d2500': cached})
    pts = P.newest_cached_points('1', '2', None, 2500)
    assert pts and pts[0]['name'] == '点位2'
    assert pts[0]['isMustPoint'] is True      # normalized on read
    # A different distance must not reuse the 2.5 km set.
    assert P.newest_cached_points('1', '2', None, 3000) is None


def test_fetch_points_uses_anchor_agnostic_cache_on_10603(monkeypatch):
    import time

    class _Client:
        _ns = object()

        def request(self, *a, **k):
            return {'json': {'error': P.RATE_LIMIT_ERR, 'message': 'limited'}}

    cached = {'ts': time.time(), 'points': [
        {'lat': 22.41, 'lon': 112.94, 'name': '点位1', 'isFixed': 0,
         'isMustPoint': False, 'seq': 1},
    ]}
    monkeypatch.setattr(
        P, '_load_cache',
        lambda: {'1|2||23.4732|116.7967|d2500': cached})
    monkeypatch.setattr(P.netsec, 'build_envelope',
                        lambda *a, **k: {'json': 'x'})
    monkeypatch.setattr(P, '_host', lambda: 'example.test')
    acct = Account(name='a', uid='1', token='x', unid=2)
    pts = P.fetch_points(_Client(), acct, 22.41, 112.94, distance_m=2500)
    assert [p['name'] for p in pts] == ['点位1']


def test_geo_fences_accepts_list_and_json_string_shapes():
    fences = [{'id': 1, 'name': '东校区'}]
    assert POL.geo_fences({'geoFences': fences}) == fences
    assert POL.geo_fences({'geoFence': fences}) == fences
    assert POL.geo_fences(
        {'geoFence': {'geoFences': json.dumps(fences)}}) == fences


def test_checkpoint_body_always_carries_run_area_id(monkeypatch):
    monkeypatch.setattr(P.netsec, 'build_envelope',
                        lambda *a, **k: {'json': 'runec'})
    monkeypatch.setattr(P, '_host', lambda: 'example.test')
    acct = Account(name='a', uid='1', token='x', unid=9)
    body = P.build_body(acct, 22.41, 112.94, object(), run_area_id=22)
    assert body['runAreaId'] == 22
    assert body['selectedUnid'] == 9
    # Official default is -1, not an omitted key.
    body2 = P.build_body(acct, 22.41, 112.94, object())
    assert body2['runAreaId'] == -1


def test_empty_checkpoint_list_can_be_a_valid_answer():
    assert P.parse_points({'pointsResModels': []}, allow_empty=True) == []
    with pytest.raises(ValueError):
        P.parse_points({'pointsResModels': []})


def test_real_fetch_points_draws_all_fences_with_a_manual_anchor(
        tmp_path, monkeypatch):
    import replica.transport.realclient as RC

    api = _api(tmp_path)
    api.real_set_session(uid='1', token='x', lat=31.0005, lon=121.0005,
                         campusName='某校区', schoolName='某大学', unid=9)
    policy = {'runAreaModels': [], 'geoFence': {'geoFences': [
        {'id': 1, 'name': '东', 'points': [
            {'glat': a, 'glon': b} for a, b in _poly(30.0, 120.0)]},
        {'id': 2, 'name': '西', 'points': [
            {'glat': a, 'glon': b} for a, b in _poly(31.0, 121.0)]},
    ]}}
    monkeypatch.setattr(api, '_fetch_run_policy', lambda *a, **k: policy)
    monkeypatch.setattr(RC, 'RealClient', lambda *a, **k: object())
    monkeypatch.setattr(P, 'fetch_points', lambda *a, **k: [])
    j = api.real_fetch_points(glat=31.0005, glon=121.0005, distance_m=2000)
    assert j['code'] == 200, j
    assert j['data']['fence_count'] == 2
    assert len(j['data']['fences']) == 2
    assert j['data']['center']['lat'] == pytest.approx(31.0005)


def test_outside_fences_accepts_a_point_in_any_ring():
    from replica.domain import routes as RT

    east = _poly(30.0, 120.0)
    west = _poly(31.0, 121.0)
    inside_west = [(31.0005, 121.0005)]
    between = [(30.5, 120.5)]
    assert RT.outside_fences(inside_west, [east, west])['count'] == 0
    assert RT.outside_fences(between, [east, west])['count'] == 1
    # A segment that leaves one campus and enters the other is outside in the
    # gap, even though both endpoints are inside.
    seg = RT.outside_fence_segments_multi(
        [(30.0005, 120.0005), (31.0005, 121.0005)], [east, west])
    assert seg['count'] == 1


def test_ensure_fence_multi_uses_a_manual_or_route_anchor(
        tmp_path, monkeypatch):
    api = _api(tmp_path)
    policy = {'geoFence': {'geoFences': [
        {'id': 1, 'name': '东', 'points': [
            {'glat': a, 'glon': b} for a, b in _poly(30.0, 120.0)]},
        {'id': 2, 'name': '西', 'points': [
            {'glat': a, 'glon': b} for a, b in _poly(31.0, 121.0)]},
    ]}}

    api.real_set_session(uid='1', token='x', lat=31.0005, lon=121.0005,
                         campusName='某校区', schoolName='某大学', unid=9)
    acct = api._real_account()
    saved = []
    monkeypatch.setattr(api, '_save_center',
                        lambda *a, **k: saved.append((a, k)))
    f = api._ensure_fence(acct, policy_data=policy)
    assert f['center_source'] == 'manual'
    assert acct.center_lat == pytest.approx(31.0005)
    assert not saved

    # No saved centre: a route point inside the west fence is a transient
    # anchor and must not be persisted.
    api.real_set_session(uid='1', token='x', campusName='某校区',
                         schoolName='某大学', unid=9)
    acct = api._real_account()
    f = api._ensure_fence(acct, policy_data=policy,
                          anchor=(31.0005, 121.0005))
    assert f['center_source'] == 'route'
    assert acct.center_lat == pytest.approx(31.0005)
    assert not saved

    # No manual/route anchor -> stop instead of choosing the first polygon.
    api.real_set_session(uid='1', token='x', campusName='某校区',
                         schoolName='某大学', unid=9)
    acct = api._real_account()
    with pytest.raises(P.FenceAmbiguous):
        api._ensure_fence(acct, policy_data=policy)


def _api(tmp_path):
    return ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                      str(tmp_path))


def test_ensure_fence_keeps_a_known_centre_when_school_has_no_fence(
        tmp_path, monkeypatch):
    api = _api(tmp_path)
    api.real_set_session(uid='1', token='x', lat=22.41, lon=112.94,
                         campusName='某校区', schoolName='某大学', unid=9)
    monkeypatch.setattr(P, 'fetch_fence', lambda *a, **k: {
        'id': '', 'name': '', 'polygon': [], 'polygons': [], 'center': None})
    acct = api._real_account()
    f = api._ensure_fence(
        acct, policy_data={'geoFence': {'geoFences': []}})
    assert f['polygon'] == []
    assert acct.center_lat == pytest.approx(22.41)
    assert acct.center_lng == pytest.approx(112.94)


def test_ensure_fence_without_fence_or_centre_is_actionable(
        tmp_path, monkeypatch):
    api = _api(tmp_path)
    api.real_set_session(uid='1', token='x', campusName='某校区',
                         schoolName='某大学', unid=9)
    monkeypatch.setattr(P, 'fetch_fence', lambda *a, **k: {
        'id': '', 'name': '', 'polygon': [], 'polygons': [], 'center': None})
    acct = api._real_account()
    with pytest.raises(P.FenceNotConfigured):
        api._ensure_fence(
            acct, policy_data={'geoFence': {'geoFences': []}})


def test_manual_campus_centre_is_persisted(tmp_path):
    api = _api(tmp_path)
    api.real_set_session(uid='1', token='x')
    assert api.real_set_campus_center(22.41, 112.94)['code'] == 200
    sess = api.real_get_session()['data']
    assert sess['lat'] == pytest.approx(22.41)
    assert sess['lon'] == pytest.approx(112.94)
    assert api.real_clear_campus_center()['code'] == 200
    sess = api.real_get_session()['data']
    assert 'lat' not in sess or not sess.get('lat')


def test_relogin_keeps_manual_centre_for_the_same_campus(
        tmp_path, monkeypatch):
    api = _api(tmp_path)
    api.real_set_session(uid='1', token='old', unid=9,
                         campusName='某校区', schoolName='某大学',
                         lat=22.41, lon=112.94)
    raw = api._read_session()
    monkeypatch.setattr(api, '_ensure_fence',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    api._persist_login({'uid': '1', 'token': 'new', 'unid': 9,
                        'profile': {'campusName': '某校区'}},
                       {'source': 'generated'}, raw)
    sess = api.real_get_session()['data']
    assert sess['lat'] == pytest.approx(22.41)
    assert sess['lon'] == pytest.approx(112.94)


def test_isfixed_marks_mandatory_when_ismustpoint_is_absent():
    biz = {'pointsResModels': [
        {'glat': 22.41, 'glon': 112.94, 'isFixed': 1, 'pointName': '点位2'},
        {'glat': 22.411, 'glon': 112.941, 'isFixed': 0, 'pointName': '点位1'},
    ]}
    pts = P.parse_points(biz)
    assert pts[0]['isMustPoint'] and pts[0]['isFixed'] == 1
    assert pts[0]['seq'] == 2 and pts[1]['seq'] == 1
    assert [p['seq'] for p in P.ordered(pts, True)] == [1, 2]
    ordered = P.route_order(pts, 22.41, 112.94, sequential=True)
    assert ordered[0] == (pts[1]['lat'], pts[1]['lon'])


def test_freeze_and_face_helpers():
    assert POL.freeze_seconds({'freezeRunTime': 125}) == 125
    assert POL.freeze_seconds({'freezeRunTime': '60'}) == 60
    assert POL.freeze_seconds({'runRuleModel': '{"freezeRunTime": 30}'}) == 30
    assert POL.freeze_seconds({}) == 0
    assert POL.face_required({'runRuleModel': {'faceVerify': True}}) == (
        True, 'runRuleModel.faceVerify')
    assert POL.face_required(
        {'runRuleModel': '{"faceVerify": true}'})[0] is True
    assert POL.face_required(
        {'runFaceCheckConfigDTO': {'enable': True}})[1] == \
        'runFaceCheckConfigDTO.enable'
    assert POL.face_required({'runRuleModel': {'faceVerify': False}}) == (
        False, '')


def _submit_api(tmp_path, monkeypatch, policy):
    import replica.application.limits as LIM
    import replica.transport.realclient as RC

    api = _api(tmp_path)
    api.real_set_session(uid='1', token='x', lat=22.41, lon=112.94,
                         campusName='某校区', schoolName='某大学', unid=9)
    monkeypatch.setattr(api, '_fetch_run_policy', lambda rc, acct: policy)
    monkeypatch.setattr(api, '_ensure_fence', lambda *a, **k: {
        'polygon': [], 'center': (22.41, 112.94)})
    monkeypatch.setattr(P, 'fetch_points', lambda *a, **k: [])
    monkeypatch.setattr(RC, 'RealClient', lambda *a, **k: object())
    monkeypatch.setattr(LIM, 'check', lambda *a, **k: None)
    reached = []

    def boom(*a, **k):
        reached.append(True)
        raise RuntimeError('REACHED_GENERATE')

    monkeypatch.setattr(BAPI, 'generate_run_data', boom)
    return api, reached


def test_empty_points_block_unless_operator_skips_them(tmp_path, monkeypatch):
    api, reached = _submit_api(
        tmp_path, monkeypatch,
        {'policy': 0, 'runRuleModel': {}})
    res = api.real_submit_run(km=1.0, pace=6.0, days_ago=1)
    assert not reached
    assert res['code'] != 200
    assert '打卡点' in res['message']
    assert '跳过打卡点' in res['message']

    # The explicit checkbox is the only way through.  Pin a past date: the
    # 09:00-22:00 window cannot fit "today" before 09:06 or after ~21:54, and
    # this test must not depend on the wall clock.
    res = api.real_submit_run(km=1.0, pace=6.0, use_points=False, days_ago=1)
    assert reached, res


def test_sequential_school_cannot_skip_points(tmp_path, monkeypatch):
    api, reached = _submit_api(
        tmp_path, monkeypatch,
        {'policy': 1, 'runRuleModel': {}})
    res = api.real_submit_run(km=1.0, pace=6.0, use_points=False)
    assert not reached
    assert res['code'] != 200
    assert '顺序' in res['message']


def test_submit_stops_when_policy_is_unavailable(tmp_path, monkeypatch):
    import replica.transport.realclient as RC

    api = _api(tmp_path)
    api.real_set_session(uid='1', token='x', campusName='某校区',
                         schoolName='某大学', unid=9)
    monkeypatch.setattr(api, '_fetch_run_policy', lambda *a, **k: None)
    monkeypatch.setattr(RC, 'RealClient', lambda *a, **k: object())
    res = api.real_submit_run(km=1.0, pace=6.0)
    assert res['code'] != 200
    assert '跑步策略' in res['message']
    receipts = tmp_path / 'submission_receipts'
    assert not receipts.exists() or not list(receipts.iterdir())


def test_freeze_warns_but_does_not_block(tmp_path, monkeypatch):
    api, reached = _submit_api(
        tmp_path, monkeypatch,
        {'policy': 0, 'freezeRunTime': 120,
         'runRuleModel': {'minDistance': 1000}})
    res = api.real_submit_run(km=1.0, pace=6.0, use_points=False, days_ago=1)
    assert reached, res
    assert 'REACHED_GENERATE' in res['message']


def test_face_verify_blocks_before_generation(tmp_path, monkeypatch):
    api, reached = _submit_api(
        tmp_path, monkeypatch,
        {'policy': 1, 'runRuleModel': {'faceVerify': True}})
    res = api.real_submit_run(km=1.0, pace=6.0)
    assert not reached
    assert '人脸' in res['message']


def test_generic_fence_failure_stops_without_a_saved_centre(tmp_path, monkeypatch):
    import replica.transport.realclient as RC

    api = _api(tmp_path)
    api.real_set_session(uid='1', token='x', campusName='某校区',
                         schoolName='某大学', unid=9)   # no lat/lon saved
    monkeypatch.setattr(api, '_fetch_run_policy',
                        lambda *a, **k: {'policy': 1, 'runRuleModel': {}})
    monkeypatch.setattr(RC, 'RealClient', lambda *a, **k: object())

    def timeout(*a, **k):
        raise RuntimeError('network timeout')

    monkeypatch.setattr(api, '_ensure_fence', timeout)
    saved, reached = [], []
    monkeypatch.setattr(api, '_save_center',
                        lambda *a, **k: saved.append((a, k)))
    monkeypatch.setattr(BAPI, 'generate_run_data',
                        lambda *a, **k: reached.append(1))
    res = api.real_submit_run(
        km=1.0, pace=6.0,
        waypoints=[(22.41, 112.94), (22.42, 112.95)])
    assert res['code'] != 200
    assert '围栏' in res['message'] and '停止提交' in res['message']
    # The route's first point must never become the account's campus.
    assert not reached
    assert not saved


def test_generic_fence_failure_may_reuse_an_already_saved_centre(
        tmp_path, monkeypatch):
    import replica.transport.realclient as RC

    api = _api(tmp_path)
    api.real_set_session(uid='1', token='x', campusName='某校区',
                         schoolName='某大学', unid=9, lat=22.41, lon=112.94)
    monkeypatch.setattr(api, '_fetch_run_policy',
                        lambda *a, **k: {'policy': 0, 'runRuleModel': {}})
    monkeypatch.setattr(RC, 'RealClient', lambda *a, **k: object())
    monkeypatch.setattr(api, '_ensure_fence',
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError('network timeout')))
    saved, reached = [], []
    monkeypatch.setattr(api, '_save_center',
                        lambda *a, **k: saved.append((a, k)))
    monkeypatch.setattr(P, 'fetch_points', lambda *a, **k: [])

    def boom(*a, **k):
        reached.append(1)
        raise RuntimeError('REACHED_GENERATE')

    monkeypatch.setattr(BAPI, 'generate_run_data', boom)
    res = api.real_submit_run(
        km=1.0, pace=6.0,
        waypoints=[(22.41, 112.94), (22.42, 112.95)],
        use_points=False, days_ago=1)
    assert reached, res
    # The stored coordinate is reused, not overwritten and not re-saved.
    assert not saved


def test_old_cache_is_normalized_on_read(monkeypatch):
    import time

    old = {'ts': time.time(), 'points': [
        {'lat': 22.41, 'lon': 112.94, 'name': '点位2',
         'isMustPoint': False, 'isFixed': 1, 'seq': None},
    ]}
    monkeypatch.setattr(P, '_load_cache', lambda: {'k': old})
    monkeypatch.setattr(P, 'cache_key', lambda *a, **k: 'k')
    pts = P.cached_points('1', '2', None, 22.41, 112.94,
                          ttl=999999999, distance_m=2500)
    assert pts[0]['isMustPoint'] is True
    assert pts[0]['seq'] == 2


def test_submit_sequential_orders_points_by_number(tmp_path, monkeypatch):
    import replica.application.limits as LIM
    import replica.transport.realclient as RC
    import replica.domain.routes as RT

    api = _api(tmp_path)
    api.real_set_session(uid='1', token='x', lat=22.41, lon=112.94,
                         campusName='某校区', schoolName='某大学', unid=9)
    policy = {'policy': 1, 'freezeRunTime': 0, 'runAreaModels': [],
              'runRuleModel': {}}
    monkeypatch.setattr(api, '_fetch_run_policy', lambda *a: policy)
    monkeypatch.setattr(api, '_ensure_fence', lambda *a, **k: {
        'polygon': [], 'center': (22.41, 112.94)})
    pts = [
        {'lat': 22.4108, 'lon': 112.9408, 'name': '点位2',
         'isMustPoint': True, 'isFixed': 1, 'seq': 2},
        {'lat': 22.4118, 'lon': 112.9418, 'name': '点位1',
         'isMustPoint': False, 'isFixed': 0, 'seq': 1},
        {'lat': 22.4100, 'lon': 112.9410, 'name': '点位3',
         'isMustPoint': False, 'isFixed': 0, 'seq': 3},
    ]
    monkeypatch.setattr(P, 'fetch_points', lambda *a, **k: pts)
    monkeypatch.setattr(RC, 'RealClient', lambda *a, **k: object())
    monkeypatch.setattr(LIM, 'check', lambda *a, **k: None)
    monkeypatch.setattr(RT, 'smooth', lambda w, **k: list(w))
    captured = {}

    def boom(*a, **k):
        captured.update(k)
        raise RuntimeError('REACHED_GENERATE')

    monkeypatch.setattr(BAPI, 'generate_run_data', boom)
    api.real_submit_run(km=1.0, pace=6.0, days_ago=1)
    assert captured.get('sequential') is True
    assert captured.get('waypoints')[0] == (pts[1]['lat'], pts[1]['lon'])
    assert captured.get('waypoints')[1] == (pts[0]['lat'], pts[0]['lon'])

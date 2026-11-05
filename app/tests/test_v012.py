"""v0.1.2: version drift and the soft plausibility band."""
import hashlib
import random
import re
import types
from pathlib import Path

import pytest

from replica.adapters.bridge_api import (APP_VERSION, PLAUSIBLE_CADENCE_MAX,
                                        PLAUSIBLE_CADENCE_MIN,
                                        PLAUSIBLE_PACE_MAX, PLAUSIBLE_PACE_MIN,
                                        plausibility_warnings)
from replica.domain.rundata import generate_run_data
from replica.transport import obs

ROOT = Path(__file__).resolve().parents[1]


def test_frontend_version_matches_app_version():
    for rel in ('frontend/index.html', 'packaging/USAGE.txt'):
        text = (ROOT / rel).read_text(encoding='utf-8')
        assert f'v{APP_VERSION}' in text, rel
        assert 'v0.1.1' not in text, rel
        found = set(re.findall(r'v(\d+\.\d+\.\d+)', text))
        assert found == {APP_VERSION}, (rel, found)


def test_band_edges_do_not_warn():
    assert plausibility_warnings(PLAUSIBLE_PACE_MIN, PLAUSIBLE_CADENCE_MIN) == []
    assert plausibility_warnings(PLAUSIBLE_PACE_MAX, PLAUSIBLE_CADENCE_MAX) == []
    assert plausibility_warnings(6.0, 172) == []


def test_the_verified_ios_run_would_warn_but_not_fail():
    # The accepted 2026-10-23 run: 9.40 min/km, 117 spm.  It must stay
    # submittable; the band is a reminder, not a gate.
    warnings = plausibility_warnings(9.40, 117)
    assert len(warnings) == 2
    assert '配速' in warnings[0] and '步频' in warnings[1]


def test_invalid_inputs_are_silent():
    assert plausibility_warnings(None, None) == []
    assert plausibility_warnings(0, 0) == []


def test_closed_route_span_larger_than_target_is_rejected():
    wps = [(22.4109, 112.9407), (22.4052, 112.9338),
           (22.4048, 112.9346), (22.4038, 112.9341)]
    with pytest.raises(ValueError, match='超过目标距离'):
        generate_run_data(1.0, 6.0, 172, waypoints=wps, closed=True)


def test_open_route_longer_than_target_is_rejected():
    wps = [(22.4046, 112.9342), (22.4060, 112.9342), (22.4070, 112.9342)]
    with pytest.raises(ValueError, match='超过目标距离'):
        generate_run_data(0.1, 6.0, 172, waypoints=wps, closed=False)


def _fake_obs(monkeypatch, corrupt=False, platform=None, key_mode='single'):
    run = generate_run_data(2.5, 6.0, 172, center_lat=22.4108,
                            center_lng=112.9408, rng=random.Random(1))
    attrs = {'uid': '42'}
    if platform:
        attrs.update({'platform': platform, 'osVersion': '18.1',
                      'physicPixel': '1179x2556', 'logicPixel': '393x852'})
    acct = types.SimpleNamespace(**attrs)
    seen = {}

    monkeypatch.setattr(obs, 'sign_url',
                        lambda client, acct, key, method='Put': 'http://signed/' + key)

    def put(url, payload, timeout=60, user_agent=''):
        seen['payload'] = payload
        seen['put_ua'] = user_agent
        return 200

    def get(url, timeout=60, user_agent=''):
        seen['get_ua'] = user_agent
        payload = seen['payload']
        if corrupt:
            return 200, b'x' * len(payload)   # same length, wrong bytes
        return 200, payload

    monkeypatch.setattr(obs, 'put_object', put)
    monkeypatch.setattr(obs, 'get_object_bytes', get)
    monkeypatch.setattr(obs.time, 'sleep', lambda *_: None)
    return obs.upload_obs(object(), acct, run, 123, 'UUID', verify=True,
                          key_mode=key_mode), seen


def test_obs_upload_verifies_sha256(monkeypatch):
    res, seen = _fake_obs(monkeypatch)
    assert res['uploaded'] == 1 and res['verified'] == 1
    assert res['sha256'] == hashlib.sha256(seen['payload']).hexdigest()


def test_obs_uses_the_same_platform_ua_as_the_api(monkeypatch):
    # OBS is a separate host (signed URL), so forgetting the UA here would
    # leave one run with an okhttp/SWCampus API UA and a Python-urllib PUT.
    _, seen = _fake_obs(monkeypatch)
    assert seen['put_ua'] == 'okhttp/4.12.0'
    assert seen['get_ua'] == 'okhttp/4.12.0'


def test_obs_follows_an_ios_profile(monkeypatch):
    _, seen = _fake_obs(monkeypatch, platform='ios')
    assert seen['put_ua'].startswith('SWCampus/7.4.0 (iPhone; iOS 18.1;')
    assert seen['get_ua'] == seen['put_ua']


def test_obs_legacy_key_mode_writes_two_keys(monkeypatch):
    res, _ = _fake_obs(monkeypatch, key_mode='legacy')
    assert res['uploaded'] == 2 and res['verified'] == 2


def test_obs_same_length_corruption_is_rejected(monkeypatch):
    res, _ = _fake_obs(monkeypatch, corrupt=True)
    assert res['uploaded'] == 1
    assert res['verified'] == 0, 'length-only verification would have passed'
    assert len(res['errors']) == 1
    assert all('sha256' in e or '回读不一致' in e for e in res['errors'])


def test_record_detail_exposes_the_obs_source():
    html = (ROOT / 'frontend' / 'index.html').read_text(encoding='utf-8')
    js = (ROOT / 'frontend' / 'js' / 'app.js').read_text(encoding='utf-8')
    assert 'id="rdSource"' in html
    assert 'json_from_obs' in js and 'OBS 读取失败' in js
    assert 'track_source' in js
    assert '轨迹未保留（OBS 404）' in js
    assert '服务器轨迹已不存在' in js


def test_frontend_warning_constants_match_backend():
    js = (ROOT / 'frontend' / 'js' / 'app.js').read_text(encoding='utf-8')
    assert f'PLAUSIBLE_PACE_MIN = {PLAUSIBLE_PACE_MIN}' in js
    assert f'PLAUSIBLE_PACE_MAX = {PLAUSIBLE_PACE_MAX}' in js
    assert f'PLAUSIBLE_CADENCE_MIN = {PLAUSIBLE_CADENCE_MIN}' in js
    assert f'PLAUSIBLE_CADENCE_MAX = {PLAUSIBLE_CADENCE_MAX}' in js
    assert '参数提醒' in js

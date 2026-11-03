"""Bridge wiring for P2 modules; no network or submit."""

import base64
import io
import json
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from replica.adapters.bridge_api import ReplicaApi  # noqa: E402


def _api(tmp_path):
    return ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                      str(tmp_path))


def test_presets_roundtrip(tmp_path):
    api = _api(tmp_path)
    assert api.real_presets('save', '早跑', {'km': 2.5})['code'] == 200
    out = api.real_presets('list')
    assert out['data']['presets'][0]['name'] == '早跑'


def test_stats_without_account_is_local(tmp_path):
    out = _api(tmp_path).real_stats(4)
    assert out['code'] == 200
    assert out['data']['source'] in ('local', 'server+local')


def test_diagnostics_zip_is_real_zip(tmp_path):
    out = _api(tmp_path).real_diagnostics_zip()
    assert out['code'] == 200
    blob = base64.b64decode(out['data']['data_b64'])
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = set(zf.namelist())
    assert {'diagnostics.txt', 'audit.json', 'clock.json'} <= names


def test_export_fit_bridge(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from replica.application import readonly

    api = _api(tmp_path)
    api._real_account = lambda: SimpleNamespace(uid='1', unid='2')
    monkeypatch.setattr(readonly, 'record_detail', lambda *a, **k: {
        'ok': True,
        'detail': {'rrid': 9, 'start_ms': 1792584000000, 'calorie': 100,
                   'total_ascent': 10},
        'track': [
            {'lat': 22.4100, 'lng': 112.9400, 'altitude': 20,
             'distance_m': 0, 'duration_s': 0},
            {'lat': 22.4110, 'lng': 112.9410, 'altitude': 25,
             'distance_m': 1000, 'duration_s': 360},
        ],
    })
    out = api.real_export_fit(9)
    assert out['code'] == 200
    assert out['data']['filename'].endswith('.fit')
    assert out['data']['data_b64']


def test_inspection_without_login_is_explicit(tmp_path):
    out = _api(tmp_path).real_inspection()
    assert out['code'] == 200
    assert out['data']['items']
    assert any(i['id'] == 'token' for i in out['data']['items'])

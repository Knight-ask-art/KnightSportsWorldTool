"""Read-only verification must classify a record without resubmitting."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from replica.application import readonly, verification  # noqa: E402


def _detail(**kw):
    d = {
        'ok': True,
        'detail': {
            'rrid': 42, 'uuid': 'u', 'complete': True, 'is_cheat': 0,
            'state': 'ok', 'status': '', 'distance_m': 2000,
            'duration_s': 720, 'reasons': [],
        },
        'stats': {'track_source': 'obs'},
        'analysis': {'series': [{'t': 10}]},
        'track': [{'duration_s': 0}, {'duration_s': 720}],
    }
    d['detail'].update(kw.pop('detail', {}))
    d.update(kw)
    return d


def test_ok_verdict(monkeypatch):
    monkeypatch.setattr(readonly, 'record_detail', lambda *a, **k: _detail())
    out = verification.verify_record(object(), object(), 42)
    assert out['ok'] is True and out['verdict'] == 'ok'
    assert any(x['step'] == 'track' and x['ok'] for x in out['timeline'])


def test_cheat_is_issue(monkeypatch):
    monkeypatch.setattr(readonly, 'record_detail',
                        lambda *a, **k: _detail(detail={'is_cheat': 1,
                                                        'state': 'issue'}))
    out = verification.verify_record(object(), object(), 42)
    assert out['verdict'] == 'issue'
    assert '不要重提' in out['next']


def test_missing_track_is_warn(monkeypatch):
    monkeypatch.setattr(readonly, 'record_detail',
                        lambda *a, **k: _detail(track=[], stats={'track_source': 'none'}))
    out = verification.verify_record(object(), object(), 42)
    assert out['verdict'] == 'warn'


def test_missing_id_is_unknown():
    out = verification.verify_record(object(), object())
    assert out['verdict'] == 'unknown'

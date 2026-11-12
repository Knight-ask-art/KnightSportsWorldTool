"""Diagnostics bundle: redaction, whitelisting and a strict leak check."""

import base64
import io
import json
import zipfile

import pytest

from replica.application import diagnostics_bundle as B

SECRETS = ('tok-9f8e7d6c5b4a', 'hunter2password', 'sig-xyz-123456789')


def _files(res):
    raw = base64.b64decode(res['data_b64'])
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        return {n: zf.read(n).decode('utf-8') for n in zf.namelist()}


def test_archive_contains_the_contract_files():
    res = B.build(app_version='0.1.8')
    files = _files(res)
    assert sorted(files) == ['audit.json', 'clock.json', 'diagnostics.txt',
                             'integrity.json', 'recent_receipts.json',
                             'settings.redacted.json']
    assert res['filename'].endswith('.zip')
    assert res['mime'] == 'application/zip'
    assert '0.1.8' in files['diagnostics.txt']
    assert '已脱敏' in files['diagnostics.txt']


def test_scrub_text_removes_credentials_urls_and_coordinates():
    text = ('token=%s Authorization: Bearer %s password=%s '
            'https://bucket.oss.example.com/a/b?Signature=%s&Expires=1 '
            '位置 22.54321,113.93456 lat: 22.54321 lng=113.93456'
            % (SECRETS[0], SECRETS[0], SECRETS[1], SECRETS[2]))
    out = B.scrub_text(text)
    for secret in SECRETS:
        assert secret not in out
    assert '22.54321' not in out
    assert '113.93456' not in out
    assert 'bucket.oss.example.com/a/b' in out      # host/path kept


def test_scrub_is_idempotent():
    once = B.scrub_text('token=abc12345 lat=22.54321')
    assert B.scrub_text(once) == once


def test_scrub_dict_masks_coordinate_values():
    out = B.scrub({'lat': 22.54321, 'lng': 113.93456, 'km': 2.5,
                   'note': 'token=abc12345', 'nested': [{'glat': 22.1}]})
    assert out['lat'] == B.COORD and out['lng'] == B.COORD
    assert out['km'] == 2.5
    assert 'abc12345' not in out['note']
    assert out['nested'][0]['glat'] == B.COORD


def test_settings_redaction_keeps_only_safe_values():
    out = B.redact_settings({'obs_key_mode': 'single', 'gps_profile': 'urban',
                             'api_token': 'tok-9f8e7d6c5b4a',
                             'saved_password': 'hunter2password',
                             'custom': 'value'})
    assert out['obs_key_mode'] == 'single'
    assert out['gps_profile'] == 'urban'
    assert out['api_token'] == B.REDACTED
    assert out['saved_password'] == B.REDACTED
    assert out['custom'] == B.REDACTED
    assert 'tok-9f8e7d6c5b4a' not in json.dumps(out)
    assert B.redact_settings(None) == {}


def test_receipts_redaction_drops_identifiers():
    rows = B.redact_receipts([
        {'uuid': 'uuid-secret', 'rrid': 778899, 'start': 1_700_000_000_000,
         'stop': 1_700_001_500_000, 'distance_m': 2500, 'state': 'accepted',
         'note': 'x ' + 'tok-9f8e7d6c5b4a'},
        'junk',
    ])
    assert len(rows) == 1
    blob = json.dumps(rows)
    assert 'uuid-secret' not in blob and '778899' not in blob
    assert 'tok-9f8e7d6c5b4a' not in blob
    assert rows[0]['duration_s'] == 1500
    assert rows[0]['state'] == 'accepted'


def test_audit_redaction_keeps_check_ids_only():
    out = B.redact_audit({'summary': {'ok': True, 'passed': 3},
                          'checks': [{'id': 'points.keys', 'level': 'error',
                                      'ok': False,
                                      'detail': 'gLat=22.54321 missing',
                                      'secret': 'tok-9f8e7d6c5b4a'}],
                          'at': '2026-11-04 12:00:00'})
    assert out['checks'][0]['id'] == 'points.keys'
    assert 'secret' not in out['checks'][0]
    assert '22.54321' not in out['checks'][0]['detail']


def test_full_bundle_has_no_leaks():
    res = B.build(
        diagnostics_text='token=%s lat=22.54321 lng=113.93456 '
                         'https://x.oss.com/a?Signature=%s'
                         % (SECRETS[0], SECRETS[2]),
        audit={'checks': [{'id': 'a', 'detail': '22.54321,113.93456'}]},
        settings={'api_token': SECRETS[0]},
        receipts=[{'start': 1_700_000_000_000, 'state': 'accepted',
                   'note': 'password=%s' % SECRETS[1]}],
        clock_state={'offset_ms': 120, 'source': 'gmttime', 'applied': True})
    info = B.inspect(res['data_b64'])
    assert info['leaks'] == []
    text = '\n'.join(_files(res).values())
    for secret in SECRETS:
        assert secret not in text


def test_inspect_flags_a_leak():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('diagnostics.txt', 'token=tok-9f8e7d6c5b4a')
        zf.writestr('x.json', '{"lat": 22.54321}')
    info = B.inspect(base64.b64encode(buf.getvalue()).decode('ascii'))
    assert info['leaks']


def test_missing_sections_become_notes():
    files = _files(B.build(diagnostics_text='ok'))
    assert '未运行提交前审计' in files['audit.json']
    assert files['recent_receipts.json'].strip() == '[]'
    assert 'offset_ms' in files['clock.json']


def test_collector_failure_does_not_break_the_bundle(monkeypatch):
    from replica.diagnostics import collector as D

    def boom(**kw):
        raise RuntimeError('collector exploded')
    monkeypatch.setattr(D, 'collect', boom)
    res = B.build()
    assert '诊断信息收集失败' in _files(res)['diagnostics.txt']


def test_self_test_passes():
    assert all(ok for _, ok, _ in B.self_test())


def test_build_rejects_oversized(monkeypatch):
    monkeypatch.setattr(B, 'MAX_BYTES', 10)
    with pytest.raises(ValueError):
        B.build(diagnostics_text='x' * 1000)

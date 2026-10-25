"""GeeTest v4: image / payload / RSA split, retry policy and failure dumps.

These tests are offline.  They cover the parts that used to be untested and
that cost us the most: the payload must stay byte-identical, a dropped field
must fail loudly, and the retry policy must not hammer a server that already
said no.
"""

import json
import os
import tempfile

import cv2
import numpy as np
import pytest

from replica.transport import gt4, gt4_image, gt4_payload, gt4_rsa


# --------------------------------------------------------------------------
# image
# --------------------------------------------------------------------------

def _pair(cut_x=100, width=200, height=60):
    bg = np.full((height, width), 40, np.uint8)
    cv2.rectangle(bg, (120, 20), (160, 55), 220, -1)
    sl = bg[20:56, cut_x:cut_x + 41].copy()
    return (cv2.imencode('.png', bg)[1].tobytes(),
            cv2.imencode('.png', sl)[1].tobytes())


def test_image_locates_the_gap_and_reports_the_width():
    bg, sl = _pair()
    found = gt4_image.locate_gap(bg, sl)
    assert abs(found['x'] - 100) <= 3
    assert found['bg_width'] == 200
    assert found['bg_height'] == 60


def test_image_rejects_a_blank_slice():
    bg, _ = _pair()
    flat = cv2.imencode('.png', np.zeros((30, 30), np.uint8))[1].tobytes()
    with pytest.raises(ValueError):
        gt4_image.locate_gap(bg, flat)


# --------------------------------------------------------------------------
# payload
# --------------------------------------------------------------------------

def test_payload_is_byte_identical_to_the_pre_split_build():
    # The sha256 was captured from gt4.py before it was split.  A single
    # changed byte here means the ticket shape moved.
    payload = gt4_payload.build_payload(
        181, lot_number=gt4_payload.GOLDEN_LOT,
        pow_msg=gt4_payload.GOLDEN_POW, pow_sign='sign', passtime=1887)
    assert gt4_payload.hashlib.sha256(payload.encode()).hexdigest() == (
        gt4_payload.GOLDEN_SHA256)


def test_payload_keeps_every_smuggled_field():
    payload = json.loads(gt4_payload.build_payload(
        181, lot_number=gt4_payload.GOLDEN_LOT,
        pow_msg=gt4_payload.GOLDEN_POW, pow_sign='sign', passtime=1887))
    # YciC and the per-challenge key are the two that are easy to lose and
    # impossible to notice from GeeTest's own answer.
    assert payload['YciC'] == 'P3Vn'
    assert payload['abcd789a'] == '6789abcd'
    assert payload['gee_guard']['roe']['cdc'] == '3'
    assert tuple(payload) == gt4_payload.expected_keys(gt4_payload.GOLDEN_LOT)


def test_dropping_a_field_is_refused():
    data = gt4_payload.payload_dict(
        1, lot_number=gt4_payload.GOLDEN_LOT, pow_msg='p', pow_sign='s',
        passtime=1000)
    data.pop('YciC')
    with pytest.raises(gt4_payload.PayloadError):
        gt4_payload.validate_payload(data, gt4_payload.GOLDEN_LOT)


def test_reordering_a_field_is_refused():
    data = gt4_payload.payload_dict(
        1, lot_number=gt4_payload.GOLDEN_LOT, pow_msg='p', pow_sign='s',
        passtime=1000)
    moved = data.pop('setLeft')
    data['setLeft'] = moved          # same keys, different order
    with pytest.raises(gt4_payload.PayloadError):
        gt4_payload.validate_payload(data, gt4_payload.GOLDEN_LOT)


def test_normal_render_keeps_the_tested_ratio():
    legacy = 181 / gt4_payload.USERRESPONSE_RATIO + 2.0
    assert gt4_payload.userresponse(181, 342) == pytest.approx(legacy, abs=1e-9)
    assert gt4_payload.userresponse(181, None) == pytest.approx(legacy, abs=1e-9)


def test_a_scaled_render_is_rescaled():
    # A 2x image reports the gap in image pixels; the track has not moved.
    normal = gt4_payload.userresponse(181, 342)
    doubled = gt4_payload.userresponse(362, 684)
    assert doubled == pytest.approx(normal, abs=0.5)


def test_passtime_varies_and_stays_plausible():
    values = {gt4_payload.passtime_for(181) for _ in range(20)}
    assert len(values) > 1
    assert all(900 <= v <= 3400 for v in values)


# --------------------------------------------------------------------------
# RSA
# --------------------------------------------------------------------------

def test_rsa_parser_recovers_the_builtin_modulus():
    js = 'var noise=[1,2,3];var key=[%s];' % ','.join(
        str(x) for x in gt4_rsa.BUILTIN_LIMBS)
    assert gt4_rsa.parse_limbs(js) == gt4_rsa.BUILTIN_LIMBS


def test_rsa_parser_ignores_short_arrays():
    assert gt4_rsa.parse_limbs('var x=[1,2,3,4,5];') is None


def test_rsa_rejects_a_bogus_candidate():
    assert gt4_rsa.set_modulus([2] * 37) is False
    assert gt4_rsa.active_source() == 'builtin'


def test_rsa_refresh_is_never_fatal():
    def boom(_):
        raise OSError('offline')

    report = gt4_rsa.refresh('/v4/static/vX', tempfile.gettempdir(),
                             fetch=boom)
    assert report['source'] == 'builtin'
    assert report['error']


def test_rsa_refresh_caches_by_static_path(tmp_path):
    js = 'var key=[%s];' % ','.join(str(x) for x in gt4_rsa.BUILTIN_LIMBS)
    calls = []

    def fetch(path):
        calls.append(path)
        return js

    first = gt4_rsa.refresh('/v4/static/vTEST', str(tmp_path), fetch=fetch)
    second = gt4_rsa.refresh('/v4/static/vTEST', str(tmp_path), fetch=fetch)

    assert first['source'] == 'js'
    assert second['source'] == 'js'
    assert len(calls) == 1          # second call came from the cache
    assert list(tmp_path.glob('rsa-*.json'))


# --------------------------------------------------------------------------
# retry policy
# --------------------------------------------------------------------------

def _creds():
    return {'lotNumber': 'x' * 32, 'captchaOutput': 'a',
            'passToken': 'b', 'genTime': '1'}


def test_rejection_moves_to_the_next_id_without_retrying(monkeypatch, tmp_path):
    monkeypatch.setenv('JHP_GT4_DUMP', 'off')
    seen = []

    def fake(cid, log, trace, dump):
        seen.append(cid)
        raise gt4.GeeRejected('nope', status='fail')

    monkeypatch.setattr(gt4, '_solve_once', fake)
    with pytest.raises(gt4.GeeRejected):
        gt4.solve_gt4(attempts=3)
    # One call per captcha id, no matter how many attempts were allowed.
    assert seen == list(gt4.CAPTCHA_ID_CANDIDATES)


def test_transport_error_is_retried(monkeypatch):
    monkeypatch.setenv('JHP_GT4_DUMP', 'off')
    monkeypatch.setattr(gt4.time, 'sleep', lambda *_: None)
    seen = []

    def fake(cid, log, trace, dump):
        seen.append(cid)
        if len(seen) < 2:
            raise gt4.GeeUnavailable('boom')
        return _creds()

    monkeypatch.setattr(gt4, '_solve_once', fake)
    assert gt4.solve_gt4('cid', attempts=3)['passToken'] == 'b'
    assert seen == ['cid', 'cid']


def test_probe_returns_a_report_instead_of_raising(monkeypatch):
    monkeypatch.setenv('JHP_GT4_DUMP', 'off')

    def fake(cid, log, trace, dump):
        raise gt4.GeeSchemaChanged('/load missing bg')

    monkeypatch.setattr(gt4, '_solve_once', fake)
    report = gt4.probe('cid')
    assert report['ok'] is False
    assert report['stage'] == 'GeeSchemaChanged'
    assert 'bg' in report['error']


# --------------------------------------------------------------------------
# dumps
# --------------------------------------------------------------------------

def test_failure_dump_lands_on_disk_and_redacts_credentials(monkeypatch,
                                                            tmp_path):
    monkeypatch.setenv('JHP_GT4_DUMP_DIR', str(tmp_path))
    monkeypatch.setenv('JHP_GT4_DUMP', 'fail')
    path = gt4.write_dump({'ok': False, 'error': 'x',
                           'pass_token': 'super-secret'})
    body = open(os.path.join(path, 'attempt.json'), encoding='utf-8').read()
    assert 'super-secret' not in body
    assert 'error' in body


def test_success_does_not_dump_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv('JHP_GT4_DUMP_DIR', str(tmp_path))
    monkeypatch.setenv('JHP_GT4_DUMP', 'fail')
    assert gt4.write_dump({'ok': True}) == ''
    assert list(tmp_path.iterdir()) == []


def test_dump_off_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv('JHP_GT4_DUMP_DIR', str(tmp_path))
    monkeypatch.setenv('JHP_GT4_DUMP', 'off')
    assert gt4.write_dump({'ok': False, 'error': 'x'}) == ''

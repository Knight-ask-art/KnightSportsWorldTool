"""End-to-end GeeTest flow against a fake /load + /verify.

Every other test exercises one module.  This one drives `_solve_once` for
real: builds a synthetic challenge, runs the image pipeline, builds `w`,
URL-encodes it, and checks that what lands in the `/verify` query is exactly
what we dumped.  That is the seam nothing else covered.

No network: `_get` and `_text` are replaced.
"""

import json
import urllib.parse

import cv2
import numpy as np
import pytest

from replica.transport import gt4, gt4_payload, gt4_rsa

LOT = gt4_payload.GOLDEN_LOT
GAP_X = 150


def _images():
    """Background with one high-contrast block; the slice is a crop at GAP_X."""
    bg = np.full((212, 340), 40, np.uint8)
    cv2.rectangle(bg, (GAP_X + 50, 40), (GAP_X + 110, 200), 230, -1)
    sl = bg[40:200, GAP_X:GAP_X + 60].copy()
    return (cv2.imencode('.png', bg)[1].tobytes(),
            cv2.imencode('.png', sl)[1].tobytes())


def _load_data():
    return {
        'bg': 'bg.png',
        'slice': 'slice.png',
        'lot_number': LOT,
        'payload': 'challenge-payload',
        'process_token': 'process-token',
        'pow_detail': {'version': 'v1', 'bits': 5, 'hashfunc': 'sha256',
                       'datetime': '2026-09-10T00:00:00'},
        'static_path': '/v4/static/vTEST',
        'captcha_mode': 'adaptive',
        'check_device': True,
    }


class FakeGee:
    def __init__(self, *, verify=None, load=None):
        self.verify = verify if verify is not None else {
            'status': 'success',
            'data': {'seccode': {
                'lot_number': LOT, 'captcha_output': 'co',
                'pass_token': 'pt', 'gen_time': '1700000000'}}}
        self.load = load
        self.urls = []
        self.bg, self.slice = _images()

    def get(self, url, timeout=30.0):
        assert '/load?' not in url and '/verify?' not in url, url
        return self.bg if url.endswith('bg.png') else self.slice

    def text(self, url, timeout=30.0):
        self.urls.append(url)
        if '/load?' in url:
            data = self.load if self.load is not None else _load_data()
            return 'cb(' + json.dumps({'status': 'success', 'data': data}) + ')'
        return 'cb(' + json.dumps(self.verify) + ')'


def _install(monkeypatch, fake):
    monkeypatch.setattr(gt4, '_text', fake.text)
    monkeypatch.setattr(gt4, '_get', fake.get)
    monkeypatch.setattr(gt4, 'refresh_rsa',
                        lambda *a, **k: {'source': 'builtin', 'error': 'test'})
    monkeypatch.setenv('JHP_GT4_DUMP', 'off')
    return fake


def _verify_url(fake):
    urls = [u for u in fake.urls if '/verify?' in u]
    assert len(urls) == 1, urls
    return urls[0]


def test_full_solve_returns_the_ticket_and_a_consistent_w(monkeypatch):
    fake = _install(monkeypatch, FakeGee())
    trace = {}

    creds = gt4.solve_gt4('cid', dump=False, trace_out=trace)

    assert creds == {'lotNumber': LOT, 'captchaOutput': 'co',
                     'passToken': 'pt', 'genTime': '1700000000'}

    # The version signals survive into the trace.
    assert trace['static_path'] == '/v4/static/vTEST'
    assert trace['check_device'] is True
    assert trace['captcha_mode'] == 'adaptive'
    assert trace['bg_width'] == 340
    assert abs(trace['gap'] - GAP_X) <= 3

    # Payload is the recorded shape, including the dynamic key and YciC.
    parsed = json.loads(trace['payload'])
    assert tuple(parsed) == gt4_payload.expected_keys(LOT)
    assert parsed['YciC'] == 'P3Vn'
    assert parsed[gt4_payload.dynamic_key(LOT)] == '6789abcd'

    # The `w` that actually went out must be AES(that payload) || RSA(str16).
    query = urllib.parse.parse_qs(urllib.parse.urlparse(_verify_url(fake)).query)
    sent = query['w'][0]
    padded = (len(trace['payload'].encode()) // 16 + 1) * 16
    assert len(sent) == padded * 2 + 128 * 2
    assert query['lot_number'] == [LOT]
    assert query['payload'] == ['challenge-payload']
    assert query['process_token'] == ['process-token']


def test_rejection_is_typed_and_leaves_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv('JHP_GT4_DUMP_DIR', str(tmp_path))
    fake = FakeGee(verify={'status': 'fail', 'code': '-1'})
    _install(monkeypatch, fake)
    monkeypatch.setenv('JHP_GT4_DUMP', 'fail')

    with pytest.raises(gt4.GeeRejected) as exc:
        gt4.solve_gt4('cid')

    assert exc.value.status == 'fail'
    assert exc.value.code == '-1'

    dirs = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert len(dirs) == 1, dirs
    body = (dirs[0] / 'attempt.json').read_text(encoding='utf-8')
    assert '"payload"' in body
    assert '"static_path"' in body
    # Screenshots are what let you re-run the matcher offline.
    assert (dirs[0] / 'bg.png').stat().st_size > 0
    assert (dirs[0] / 'slice.png').stat().st_size > 0


def test_missing_load_field_is_a_schema_error(monkeypatch):
    data = _load_data()
    data.pop('bg')
    fake = _install(monkeypatch, FakeGee(load=data))
    with pytest.raises(gt4.GeeSchemaChanged) as exc:
        gt4.solve_gt4('cid', dump=False)
    assert 'bg' in str(exc.value)
    # It failed before downloading anything.
    assert not [u for u in fake.urls if '/verify?' in u]


def test_non_object_pow_detail_is_a_schema_error(monkeypatch):
    data = _load_data()
    data['pow_detail'] = 'v1|5'
    _install(monkeypatch, FakeGee(load=data))
    with pytest.raises(gt4.GeeSchemaChanged):
        gt4.solve_gt4('cid', dump=False)


def test_success_without_seccode_is_a_schema_error(monkeypatch):
    fake = FakeGee(verify={'status': 'success', 'data': {}})
    _install(monkeypatch, fake)
    with pytest.raises(gt4.GeeSchemaChanged):
        gt4.solve_gt4('cid', dump=False)


def test_probe_reports_the_same_solve_without_geevalidate(monkeypatch):
    fake = _install(monkeypatch, FakeGee())
    report = gt4.probe('cid')
    assert report['ok'] is True
    assert report['static_path'] == '/v4/static/vTEST'
    assert report['bg_width'] == 340
    # A canary must never reach the business endpoints.
    assert len([u for u in fake.urls if '/verify?' in u]) == 1


def test_refresh_switches_to_a_2048_bit_key(monkeypatch, tmp_path):
    monkeypatch.setenv('JHP_GT4_RSA', 'on')
    n = (1 << 2047) + 1
    limbs = gt4_rsa.int_to_limbs(n)
    js = 'var noise=[1,2,3];var key=[%s];' % ','.join(str(x) for x in limbs)
    try:
        report = gt4_rsa.refresh('/v4/static/vBIG', str(tmp_path),
                                 fetch=lambda path: js)
        assert report['source'] == 'js'
        assert report['bits'] == 2048
        assert report['changed'] is True
        # A 2048-bit modulus means a 256-byte block, i.e. 512 hex chars.
        assert len(gt4_rsa.rsa_encrypt_hex(b'0123456789abcdef')) == 512
    finally:
        gt4_rsa.set_modulus(None)
    assert gt4_rsa.active_source() == 'builtin'
    assert gt4_rsa.modulus_bits() == 1024


def test_bridge_ticket_signature_accepts_the_ui_payload():
    import inspect
    from replica.adapters.bridge_api import ReplicaApi
    sig = inspect.signature(ReplicaApi.real_login_ticket)
    sig.bind(None, '13800000000', 'pw', lotNumber='l', captchaOutput='c',
             passToken='t', genTime='1')


def test_bridge_gt4_config_is_offline_and_shaped_for_the_ui():
    from replica.adapters.bridge_api import ReplicaApi
    r = ReplicaApi.real_gt4_config(None)
    assert r['code'] == 200
    data = r['data']
    assert data['captcha_id'] in gt4.CAPTCHA_ID_CANDIDATES
    assert data['script'].startswith('https://static.geetest.com/')
    assert data['rsa_bits'] == 1024

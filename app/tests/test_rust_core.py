"""Guards for the optional Rust core sidecar.

The binary is built by `packaging/build.ps1`; a source checkout without Rust
falls back to the Python implementation, so the runtime tests skip when the
binary is absent.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from replica.domain import crypto  # noqa: E402
from replica.transport import netsec, rustcore  # noqa: E402

CORE = rustcore.find_core()
needs_core = pytest.mark.skipif(CORE is None, reason='hxp-core not built')
UPLOAD_FIXTURE = (ROOT.parent / 'rust' / 'hxp-core' / 'tests' / 'fixtures'
                  / 'upload_entity.json')


def test_packaging_builds_and_bundles_the_native_core():
    build = (ROOT / 'packaging' / 'build.ps1').read_text(encoding='utf-8')
    assert 'rust\\hxp-core' in build
    assert 'cargo build --release --locked' in build
    assert "runtime\\rust" in build


def test_netsec_can_route_through_the_native_core():
    src = (ROOT / 'replica' / 'transport' / 'netsec.py').read_text(encoding='utf-8')
    assert 'rustcore' in src
    assert "'build_envelope'" in src
    assert "'decrypt_response'" in src


def test_gt4_payload_can_route_through_the_native_core():
    src = (ROOT / 'replica' / 'transport' / 'gt4_payload.py').read_text(encoding='utf-8')
    assert 'rustcore' in src
    assert "'gt4_payload'" in src
    assert "'gt4_w'" in src


@needs_core
def test_native_gt4_payload_matches_the_golden_digest():
    import hashlib
    from replica.transport import gt4_payload
    core = rustcore.get()
    assert core is not None
    text = core.call('gt4_payload', set_left=181,
                     lot_number=gt4_payload.GOLDEN_LOT,
                     pow_msg=gt4_payload.GOLDEN_POW,
                     pow_sign='sign', passtime=1887)
    digest = hashlib.sha256(text.encode()).hexdigest()
    assert digest == gt4_payload.GOLDEN_SHA256


@needs_core
def test_native_upload_entity_matches_the_python_builder():
    import hashlib
    import json

    from replica.domain import rundata

    fixture = json.loads(UPLOAD_FIXTURE.read_text(encoding='utf-8'))
    core = rustcore.get()
    assert core is not None
    native = core.call('upload_entity', run=fixture['run'], ctx=fixture['ctx'])
    reference = rundata._build_upload_entity(fixture['run'], fixture['ctx'])
    assert native == reference
    assert list(native)[:2] == ['address', 'allLocJson']
    text = json.dumps(native, ensure_ascii=False, separators=(',', ':'))
    assert hashlib.sha256(text.encode()).hexdigest() == fixture['expected_sha256']


def test_upload_entity_can_route_through_the_native_core():
    src = (ROOT / 'replica' / 'domain' / 'rundata.py').read_text(encoding='utf-8')
    assert 'rustcore' in src
    assert "core.call('upload_entity'" in src


@needs_core
def test_native_obs_object_matches_the_python_builder():
    import base64
    import gzip
    import random

    from replica.domain.rundata import generate_run_data
    from replica.transport import obs

    run = generate_run_data(1.0, 6.0, 150, center_lat=22.4108,
                            center_lng=112.9408, rng=random.Random(7))
    native = obs.build_obs_object(run, 1344757460, 'uuid-1', '2023110421', '[]')
    reference = obs._build_obs_object(run, 1344757460, 'uuid-1', '2023110421', '[]')
    assert list(native) == list(reference)
    assert len(native) == 11
    for key in native:
        assert gzip.decompress(base64.b64decode(native[key])) == \
            gzip.decompress(base64.b64decode(reference[key])), key


def test_obs_object_can_route_through_the_native_core():
    src = (ROOT / 'replica' / 'transport' / 'obs.py').read_text(encoding='utf-8')
    assert "core.call('obs_object'" in src
    assert 'wire_points' in src


@needs_core
def test_native_device_validate_matches_the_python_reference():
    from replica.application import device_profile as DP

    sample = {
        'version': 1,
        'platform': 'iOS',
        'source': 'rotated',
        'created_at': 1_792_800_000_000,
        'DeviceId': 'AFA505AE-1E60-3779-7256-4C220FB24311',
        'deviceName': 'iPhone 15',
        'osVersion': '18.1',
        'cpuModel': 'arm64e',
        'physicPixel': '1179x2556',
        'logicPixel': '393x852',
        'isRoot': '0',
        'idfa': '00000000-0000-0000-0000-000000000000',
        'appInstallTime': '1785436971435',
        'appUpdateTime': '1788581965711',
        'rotated_at': 1_792_900_000_000,
        'rotated_from': 'afa505ae1e60377972564c220fb24311',
        'previous_device_masked': 'afa5…4311',
    }
    core = rustcore.get()
    assert core is not None
    native = core.call('device_validate', profile=sample,
                       now_ms=1_792_800_000_000)
    reference = DP._validate_profile(sample)
    assert native == reference


def test_device_profile_can_route_through_the_native_core():
    src = (ROOT / 'replica' / 'application' / 'device_profile.py').read_text(
        encoding='utf-8')
    assert "core.call('device_validate'" in src
    assert "core.call('device_generate'" in src


@needs_core
def test_native_run_core_keeps_the_columns_closed():
    core = rustcore.get()
    assert core is not None
    out = core.call(
        'run_core', total_meters=1000, total_seconds=360,
        seg_m=[1000.0], seg_t=[360], pace_min_per_km=6.0,
        n_points=72, cadence=150, total_steps=900,
        t0=1_794_411_506_893, seed=7)
    assert len(out['cum_before']) == 361
    assert out['cum_before'][-1] == pytest.approx(1000.0, abs=1e-6)
    assert len(out['times']) == 72
    assert out['times'][0] == 0.0
    assert out['times'][-1] == 360.0
    assert sum(x['distance'] for x in out['speed_json']) == \
        pytest.approx(1000.0, abs=0.01)
    assert sum(x['stepsNum'] for x in out['step_freq_json']) == 900
    assert len(out['segment_json']) == 12
    assert sum(x['distance'] for x in out['segment_json']) == \
        pytest.approx(1000.0, abs=0.01)


def test_run_core_can_route_through_the_native_core():
    src = (ROOT / 'replica' / 'domain' / 'rundata.py').read_text(encoding='utf-8')
    assert "'run_core'" in src
    assert '_pace_core_py' in src


def test_frozen_builds_require_the_native_core(monkeypatch):
    import os

    import app as entry
    from replica import build_info

    monkeypatch.delenv('JHP_RUST_CORE', raising=False)
    monkeypatch.setattr(build_info, 'is_compiled', lambda: True)
    entry._pin_native_core()
    assert os.environ['JHP_RUST_CORE'] == '1'
    # The explicit override stays available for troubleshooting.
    monkeypatch.setenv('JHP_RUST_CORE', '0')
    entry._pin_native_core()
    assert os.environ['JHP_RUST_CORE'] == '0'


def test_a_failed_ping_is_retried_on_the_next_login(monkeypatch):
    """One transient ping failure must not disable the layer until restart."""
    class FlakyCore:
        pings = 0

        def __init__(self, exe, timeout=8.0):
            self.exe = exe

        def call(self, op, **kwargs):
            assert op == 'ping'
            FlakyCore.pings += 1
            if FlakyCore.pings == 1:
                raise rustcore.RustCoreError('busy')
            return {'pong': True}

        def close(self):
            pass

    monkeypatch.setattr(rustcore, '_Core', FlakyCore)
    monkeypatch.setattr(rustcore, 'find_core', lambda: 'hxp-core.exe')
    monkeypatch.setattr(rustcore, '_core', None)
    monkeypatch.setattr(rustcore, '_probed', False)
    monkeypatch.delenv('JHP_RUST_CORE', raising=False)
    try:
        assert rustcore.get() is None
        assert FlakyCore.pings == 1
        # The probe is retried, and success latches.
        assert rustcore.get() is not None
        assert FlakyCore.pings == 2
        assert rustcore.get() is not None
        assert FlakyCore.pings == 2
    finally:
        rustcore._core = None
        rustcore._probed = False


@needs_core
def test_native_self_test_passes():
    core = rustcore.get()
    assert core is not None, 'JHP_RUST_CORE=0 disables the core'
    results = core.call('self_test')
    failed = [item for item in results if not item.get('ok')]
    assert not failed, failed


@needs_core
def test_native_envelope_matches_the_python_contract():
    core = rustcore.get()
    assert core is not None
    four = '86123.456789'
    ts = 1_700_000_000_000
    plain = '{"hello":"世界"}'
    data = core.call('build_envelope', plain=plain, order='insert',
                     four=four, ts_ms=ts, platform=2)
    container = netsec.serialize_container(
        netsec.b64e(plain.encode()), ts,
        [netsec.KEYDATA_ONE, netsec.KEYDATA_TWO, netsec.KEYDATA_THREE, four], 2)
    assert data['container'] == container.decode('utf-8')
    assert data['h'] == netsec.md5_hex(container)
    back = netsec.aes_cbc_decrypt(data['req_key'].encode(),
                                  netsec.b64d(data['d']))
    assert back == container
    assert len(netsec.b64d(data['k'])) == 128


@needs_core
def test_native_signing_matches_the_golden_vectors():
    core = rustcore.get()
    assert core is not None
    assert core.call('token_sign', ts='1700000000000',
                     token='abc123', uid='2023110421') == \
        '7fd46d4c3bac075da93ce694a45c19db'
    assert core.call('token_sign_ios', ts='1700000000000',
                     token='abc123', uid='2023110421') == \
        '1fe038ba08262f0184a34c2a8e4c9466'
    assert core.call('runschool_sign', params={'b': '2', 'a': '1'}) == \
        'ed04c91cf6f6ab5a01a31c0295c5da34'
    assert crypto.token_sign('1700000000000', 'abc123', '2023110421') == \
        '7fd46d4c3bac075da93ce694a45c19db'

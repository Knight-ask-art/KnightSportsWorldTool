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

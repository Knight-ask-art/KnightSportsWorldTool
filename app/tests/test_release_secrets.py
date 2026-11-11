"""Phase 0 release-secret boundary tests.

Covers the scanner used by the build gate, the dev-only module exclusion, and
the frozen-mode crypto contract: a packaged build must ask the native core and
must fail closed without leaking a key or an internal path.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_ROOT.parent
SCANNER_PATH = REPO_ROOT / 'scripts' / 'scan_release_secrets.py'


def _load_scanner():
    spec = importlib.util.spec_from_file_location(
        'scan_release_secrets_for_tests', SCANNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCANNER = _load_scanner()
SECRETS = SCANNER.SECRETS


# ---- scanner -------------------------------------------------------------

@pytest.mark.parametrize('label', sorted(SECRETS))
def test_scanner_detects_each_forbidden_literal(label):
    blob = b'header' + SECRETS[label].encode('utf-8') + b'trailer'
    assert SCANNER.scan_bytes(blob) == [label]


@pytest.mark.parametrize('label', sorted(SECRETS))
def test_scanner_detects_utf16_copies(label):
    blob = b'\x00\x00' + SECRETS[label].encode('utf-16-le') + b'\x00\x00'
    assert SCANNER.scan_bytes(blob) == [label]


def test_scanner_clean_file_returns_zero(tmp_path, capsys):
    sample = tmp_path / 'clean.bin'
    sample.write_bytes(b'no protocol constants here')
    assert SCANNER.main([str(sample)]) == 0
    assert 'clean' in capsys.readouterr().out


def test_scanner_reports_label_without_the_literal(tmp_path, capsys):
    sample = tmp_path / 'dirty.bin'
    sample.write_bytes(b'x' + SECRETS['APP_ID'].encode() + b'y')
    assert SCANNER.main([str(sample)]) == 1
    out = capsys.readouterr().out
    assert 'HIT APP_ID' in out
    assert SECRETS['APP_ID'] not in out


def test_scanner_missing_file_is_usage_error(tmp_path, capsys):
    assert SCANNER.main([str(tmp_path / 'nope.exe')]) == 2
    assert 'cannot read' in capsys.readouterr().err


def test_old_release_exe_is_rejected_by_the_scanner():
    """The v0.2.2 package predates Phase 0; it must fail the new gate."""
    exe = APP_ROOT / 'dist' / '好想跑_v0.2.2' / '好想跑.exe'
    if not exe.is_file():
        pytest.skip('v0.2.2 release exe is not present')
    hits = SCANNER.scan_file(exe)
    assert hits, 'the pre-Phase-0 exe should still contain a plaintext secret'


# ---- source boundary -----------------------------------------------------

def test_production_python_has_no_plaintext_constants():
    """Only ``_dev_secrets`` may contain these literals in the shipped unit."""
    files = list((APP_ROOT / 'replica').rglob('*.py')) + [APP_ROOT / 'app.py']
    offenders = []
    for path in files:
        if path.name == '_dev_secrets.py':
            continue
        data = path.read_bytes()
        for label, value in SECRETS.items():
            if value.encode('utf-8') in data:
                offenders.append(f'{path.relative_to(APP_ROOT)}:{label}')
    assert not offenders, offenders


def test_build_excludes_the_dev_secrets_module():
    build = (APP_ROOT / 'packaging' / 'build.ps1').read_text(encoding='utf-8')
    spec = (APP_ROOT / 'packaging' / 'build.spec').read_text(encoding='utf-8')
    assert '--nofollow-import-to=replica.domain._dev_secrets' in build
    assert 'replica.domain._dev_secrets' in spec
    assert 'scan_release_secrets.py' in build
    assert 'release secret scan failed' in build


# ---- frozen contract -----------------------------------------------------

@pytest.fixture
def frozen(monkeypatch):
    from replica.domain import crypto
    from replica.transport import rustcore

    monkeypatch.setattr(crypto, 'native_required', lambda: True)
    monkeypatch.setattr(rustcore, 'get', lambda: None)
    return crypto


def _frozen_calls(crypto):
    return {
        'runschool_sign': lambda: crypto.runschool_sign({'a': '1'}),
        'token_sign': lambda: crypto.token_sign('1', 't', 'u'),
        'token_sign_explicit_salt': lambda: crypto.token_sign(
            '1', 't', 'u', 'S'),
        'official_sign': lambda: crypto.official_sign({'a': '1'}),
        'official_sign_explicit_salt': lambda: crypto.official_sign(
            {'a': '1'}, 'S'),
        'points_sign': lambda: crypto.points_sign('h', '/p'),
        'sign_with_suffix': lambda: crypto.sign_with_suffix('1', 't', 'u'),
        'protocol_constant': lambda: crypto.protocol_constant('app_id'),
        'upload_headers': lambda: crypto.upload_headers(),
        'module_attr_app_id': lambda: crypto.APP_ID,
        'module_attr_secret': lambda: crypto.RUNSCHOOL_SECRET,
    }


@pytest.mark.parametrize('name', sorted(_frozen_calls(
    __import__('replica.domain.crypto', fromlist=['x']))))
def test_frozen_crypto_fails_closed_without_native(frozen, name):
    call = _frozen_calls(frozen)[name]
    with pytest.raises(frozen.NativeCryptoRequired) as excinfo:
        call()
    message = str(excinfo.value)
    assert not any(value in message for value in SECRETS.values())
    assert 'hxp-core' not in message
    assert '\\' not in message and '/' not in message


def test_frozen_crypto_routes_every_signing_call_to_native(monkeypatch):
    from replica.domain import crypto

    calls = []

    def fake_native(op, **args):
        calls.append((op, args))
        return {
            'runschool_sign': 'runschool',
            'token_sign': 'token',
            'official_sign': 'official',
            'points_sign': 'points',
            'token_sign_ios': 'ios',
            'protocol_constants': {'host': 'h', 'app_id': 'a',
                                   'runec_key': 'r'},
        }[op]

    monkeypatch.setattr(crypto, 'native_required', lambda: True)
    monkeypatch.setattr(crypto, '_native_call', fake_native)

    assert crypto.runschool_sign({'a': 1}) == 'runschool'
    assert crypto.token_sign('1', 't', 'u') == 'token'
    assert crypto.official_sign({'a': '1'}) == 'official'
    assert crypto.points_sign('h', '/p') == 'points'
    assert crypto.sign_with_suffix('1', 't', 'u') == 'ios'
    assert crypto.protocol_constant('app_id') == 'a'

    ops = [op for op, _ in calls]
    assert ops == ['runschool_sign', 'token_sign', 'official_sign',
                   'points_sign', 'token_sign_ios', 'protocol_constants']
    # No dev-only salt is supplied for the default path; explicit salts cross
    # the boundary as caller data, not as a recovered constant.
    assert 'salt' not in calls[1][1]
    assert 'salt' not in calls[2][1]
    assert crypto.token_sign('1', 't', 'u', 'X') == 'token'
    assert calls[-1] == ('token_sign', {'ts': '1', 'token': 't', 'uid': 'u',
                                        'salt': 'X'})
    assert crypto.official_sign({'a': '1'}, 'S') == 'official'
    assert calls[-1] == ('official_sign', {'params': {'a': '1'},
                                           'salt': 'S'})


def test_source_checkout_keeps_the_python_fallback(monkeypatch):
    from replica.domain import crypto

    monkeypatch.setattr(crypto, 'native_required', lambda: False)
    monkeypatch.setattr(crypto, '_source_core', lambda: None)

    assert crypto.runschool_sign({'b': '2', 'a': '1'}) == \
        'ed04c91cf6f6ab5a01a31c0295c5da34'
    assert crypto.token_sign('1700000000000', 'abc123', '2023110421') == \
        '7fd46d4c3bac075da93ce694a45c19db'
    assert crypto.official_sign(
        {'timeStamp': '1700000000000', 'token': 'abc123',
         'uid': '2023110421'}) == '1fe038ba08262f0184a34c2a8e4c9466'
    assert crypto.sign_with_suffix(
        '1700000000000', 'abc123', '2023110421') == \
        '1fe038ba08262f0184a34c2a8e4c9466'
    assert crypto.points_sign(
        'run.gxapp.iydsj.com', '/api/v560/get/1/distance/2000') == \
        '45732c140c185c94e096316a297a6065'
    assert crypto.protocol_constant('host') == 'run.gxapp.iydsj.com'
    assert crypto.APP_ID == 'prj7sr2xk2bntjccmzbjckps7m9ogmlz'

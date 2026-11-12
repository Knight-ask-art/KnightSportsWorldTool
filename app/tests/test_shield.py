"""Tests for the four-layer device credential orchestrator.

None of these tests need the Go/Rust/C++ binaries: the layer calls are
injected, the frame codec is exercised with in-memory pipes, and the
seal/check tests drive a Python child process as a controllable stand-in.
"""
from __future__ import annotations

import io
import struct
import subprocess
import sys
import time

import pytest

from replica.application import account_guard as AG
from replica.application import shield


@pytest.fixture(autouse=True)
def _shield_enabled(monkeypatch):
    monkeypatch.delenv('JHP_SHIELD', raising=False)


def test_disabled_skips_without_touching_binaries(monkeypatch):
    monkeypatch.setenv('JHP_SHIELD', '0')
    monkeypatch.setattr(shield, '_binaries',
                        lambda: pytest.fail('must not probe binaries'))
    assert shield.require('store') == {'allowed': True, 'skipped': True}


def test_source_checkout_without_all_binaries_skips(monkeypatch):
    monkeypatch.setattr(shield, '_binaries', lambda: (None, None, None))
    monkeypatch.setattr(shield, '_is_frozen', lambda: False)
    assert shield.require('store') == {'allowed': True, 'skipped': True}


def test_frozen_build_with_a_missing_layer_blocks(monkeypatch):
    monkeypatch.setattr(shield, '_binaries', lambda: ('shield.exe', None, 'core.exe'))
    monkeypatch.setattr(shield, '_is_frozen', lambda: True)
    out = shield.require('store')
    assert out == {'blocked': True, 'message': shield.SHIELD_MESSAGE}


def test_all_layers_passing_allows_the_login(monkeypatch):
    seen = {}

    monkeypatch.setattr(shield, '_binaries',
                        lambda: ('shield.exe', 'guard.exe', 'core.exe'))
    monkeypatch.setattr(shield, '_is_frozen', lambda: True)

    def attest(nonce, store_dir):
        seen['store'] = store_dir
        seen['nonce'] = bytes(nonce)
        return b'G' * 32

    def mix(blob, nonce):
        # Copy: require() zeroes the mutable buffers when it returns.
        seen['mix'] = (bytes(blob), bytes(nonce))
        return b'R' * 32

    def seal(exe, nonce, blob_go, blob_rust):
        seen['seal'] = (exe, len(nonce), len(blob_go), len(blob_rust))
        return True

    monkeypatch.setattr(shield, '_go_attest', attest)
    monkeypatch.setattr(shield, '_rust_mix', mix)
    monkeypatch.setattr(shield, '_cpp_seal_check', seal)

    assert shield.require('the-store') == {'allowed': True}
    assert seen['store'] == 'the-store'
    assert len(seen['nonce']) == 16
    assert seen['mix'] == (b'G' * 32, seen['nonce'])
    assert seen['seal'] == ('shield.exe', 16, 32, 32)


def test_check_returning_zero_blocks(monkeypatch):
    monkeypatch.setattr(shield, '_binaries',
                        lambda: ('shield.exe', 'guard.exe', 'core.exe'))
    monkeypatch.setattr(shield, '_is_frozen', lambda: True)
    monkeypatch.setattr(shield, '_go_attest', lambda nonce, store: b'A' * 32)
    monkeypatch.setattr(shield, '_rust_mix', lambda blob, nonce: b'B' * 32)
    monkeypatch.setattr(shield, '_cpp_seal_check',
                        lambda exe, nonce, go, rust: False)

    assert shield.require('store') == {
        'blocked': True, 'message': shield.SHIELD_MESSAGE}


def test_mix_failure_blocks(monkeypatch):
    monkeypatch.setattr(shield, '_binaries',
                        lambda: ('shield.exe', 'guard.exe', 'core.exe'))
    monkeypatch.setattr(shield, '_is_frozen', lambda: True)
    monkeypatch.setattr(shield, '_go_attest', lambda nonce, store: b'A' * 32)

    def broken_mix(blob, nonce):
        raise shield.ShieldError('mix layer failed')

    monkeypatch.setattr(shield, '_rust_mix', broken_mix)
    assert shield.require('store')['blocked'] is True


def test_frame_error_blocks(monkeypatch):
    monkeypatch.setattr(shield, '_binaries',
                        lambda: ('shield.exe', 'guard.exe', 'core.exe'))
    monkeypatch.setattr(shield, '_is_frozen', lambda: True)
    monkeypatch.setattr(shield, '_go_attest', lambda nonce, store: b'A' * 32)
    monkeypatch.setattr(shield, '_rust_mix', lambda blob, nonce: b'B' * 32)

    def bad_frame(exe, nonce, go, rust):
        raise shield.ShieldError('frame layer failed')

    monkeypatch.setattr(shield, '_cpp_seal_check', bad_frame)
    assert shield.require('store')['blocked'] is True


def test_attest_layer_failure_blocks(monkeypatch):
    monkeypatch.setattr(shield, '_binaries',
                        lambda: ('shield.exe', 'guard.exe', 'core.exe'))
    monkeypatch.setattr(shield, '_is_frozen', lambda: True)

    def broken_attest(nonce, store):
        raise RuntimeError('attest failed')

    monkeypatch.setattr(shield, '_go_attest', broken_attest)
    assert shield.require('store')['blocked'] is True


def test_block_message_is_the_fixed_sentence_and_never_names_a_layer():
    assert shield.SHIELD_MESSAGE == '设备校验未通过，请重启软件后再试'
    for token in ('go', 'rust', 'c++', 'exe', 'nonce', 'blob', 'token',
                  '路径', '超时'):
        assert token.lower() not in shield.SHIELD_MESSAGE.lower()


def test_frame_header_matches_the_cpp_protocol():
    frame = shield._frame(shield._OP_SEAL, b'abc')
    magic, version, op, length = struct.unpack('<IHHI', frame[:12])
    assert magic == 0x53505848
    assert version == 1
    assert op == 0x01
    assert length == 3
    assert frame[12:] == b'abc'


def test_read_response_is_strict_about_magic_version_op_and_length():
    def header(op, length, magic=shield._MAGIC, version=shield._VERSION):
        return struct.pack('<IHHI', magic, version, op, length)

    class Pipe:
        def __init__(self, raw):
            self.raw = io.BytesIO(raw)

        def read(self, size):
            return self.raw.read(size)

    class Proc:
        def __init__(self, raw):
            self.stdout = Pipe(raw)

    good = header(shield._OP_CHECK, 1) + b'\x01'
    assert shield._read_response(Proc(good), shield._OP_CHECK, 1) == b'\x01'

    for raw in (
        header(shield._OP_CHECK, 1, magic=0) + b'\x01',
        header(shield._OP_CHECK, 1, version=2) + b'\x01',
        header(shield._OP_SEAL, 1) + b'\x01',
        header(shield._OP_CHECK, 2) + b'\x01\x01',
        header(shield._OP_CHECK, shield._MAX_PAYLOAD + 1),
    ):
        with pytest.raises(shield.ShieldError):
            shield._read_response(Proc(raw), shield._OP_CHECK, 1)


def test_find_shield_honours_the_explicit_path(tmp_path, monkeypatch):
    exe = tmp_path / 'hxp-shield.exe'
    exe.write_bytes(b'MZ')
    monkeypatch.setenv('JHP_SHIELD_PATH', str(exe))
    assert shield.find_shield() == str(exe)


def test_account_guard_attest_parses_the_blob(monkeypatch, tmp_path):
    blob = bytes(range(32))
    monkeypatch.setattr(AG, 'find_guard', lambda: 'hxp-guard.exe')

    class Proc:
        returncode = 0
        stdout = '{"ok":true,"blob":"' + blob.hex() + '"}\n'
        stderr = ''

    seen = {}

    def fake_run(args, **kwargs):
        seen['args'] = args
        seen['kwargs'] = kwargs
        return Proc()

    monkeypatch.setattr(AG.subprocess, 'run', fake_run)
    nonce = bytes(range(16))
    assert AG.attest(nonce, str(tmp_path)) == blob
    assert seen['args'][1:2] == ['op']
    assert seen['args'][seen['args'].index('--code') + 1] == '17'
    # The nonce travels on stdin, never in argv.
    assert '--nonce' not in seen['args']
    assert nonce.hex() not in ' '.join(seen['args'])
    assert seen['kwargs']['input'] == nonce.hex() + '\n'


def test_account_guard_attest_timeout_does_not_attach_the_command_line(
        monkeypatch, tmp_path):
    nonce = bytes.fromhex('00112233445566778899aabbccddeeff')
    monkeypatch.setattr(AG, 'find_guard', lambda: 'hxp-guard.exe')

    def slow_run(args, **kwargs):
        # Simulate subprocess.TimeoutExpired, whose str()/cmd carries argv.
        raise subprocess.TimeoutExpired(
            ['hxp-guard.exe', 'op', '--state', 'x', '--code', '17',
             '--nonce', nonce.hex()], 1)

    monkeypatch.setattr(AG.subprocess, 'run', slow_run)
    with pytest.raises(RuntimeError) as excinfo:
        AG.attest(nonce, str(tmp_path))
    assert excinfo.value.__cause__ is None
    assert nonce.hex() not in str(excinfo.value)


# --------------------------------------------------------------------------
# real frame exchange against a controllable child process
# --------------------------------------------------------------------------

_GOOD_CHILD = r'''
import struct, sys
MAGIC, VERSION = 0x53505848, 1
H = struct.Struct('<IHHI')
def read_frame():
    head = sys.stdin.buffer.read(H.size)
    magic, version, op, length = H.unpack(head)
    assert (magic, version) == (MAGIC, VERSION)
    body = sys.stdin.buffer.read(length)
    assert len(body) == length
    return op, body
op, body = read_frame()
assert (op, len(body)) == (1, 80)
token = bytes(range(32))
sys.stdout.buffer.write(
    H.pack(MAGIC, VERSION, 1, 40) + token + (123).to_bytes(8, 'little'))
sys.stdout.buffer.flush()
op, body = read_frame()
assert (op, len(body)) == (2, 40)
assert body[:32] == token
sys.stdout.buffer.write(H.pack(MAGIC, VERSION, 2, 1) + b'\x01')
sys.stdout.buffer.flush()
'''

_HALF_FRAME_CHILD = r'''
import struct, sys, time
H = struct.Struct('<IHHI')
sys.stdout.buffer.write(H.pack(0x53505848, 1, 1, 40)[:6])
sys.stdout.buffer.flush()
time.sleep(30)
'''

_SEAL_THEN_HANG_CHILD = r'''
import struct, sys, time
MAGIC, VERSION = 0x53505848, 1
H = struct.Struct('<IHHI')
head = sys.stdin.buffer.read(H.size)
length = H.unpack(head)[3]
sys.stdin.buffer.read(length)
sys.stdout.buffer.write(
    H.pack(MAGIC, VERSION, 1, 40) + bytes(range(32)) + (1).to_bytes(8, 'little'))
sys.stdout.buffer.flush()
time.sleep(30)
'''

_WRONG_RESPONSE_CHILD = r'''
import sys
head = sys.stdin.buffer.read(12)
length = int.from_bytes(head[8:12], 'little')
sys.stdin.buffer.read(length)
sys.stdout.buffer.write(bytes.fromhex("__RAW__"))
sys.stdout.buffer.flush()
'''


def _record_popen(monkeypatch):
    box = {}
    real_popen = shield.subprocess.Popen

    def recording_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        box['proc'] = proc
        return proc

    monkeypatch.setattr(shield.subprocess, 'Popen', recording_popen)
    return box


def _run_child(monkeypatch, script):
    box = _record_popen(monkeypatch)
    result = shield._cpp_seal_check(
        sys.executable, bytes(range(16)), b'G' * 32, b'R' * 32,
        argv=[sys.executable, '-c', script])
    return result, box['proc']


def _run_child_expect_error(monkeypatch, script):
    box = _record_popen(monkeypatch)
    with pytest.raises(shield.ShieldError):
        shield._cpp_seal_check(
            sys.executable, bytes(range(16)), b'G' * 32, b'R' * 32,
            argv=[sys.executable, '-c', script])
    return box['proc']


def test_cpp_seal_check_completes_a_real_frame_exchange(monkeypatch):
    started = time.monotonic()
    result, proc = _run_child(monkeypatch, _GOOD_CHILD)
    assert result is True
    assert time.monotonic() - started < 3
    assert proc.poll() is not None


def test_half_a_frame_times_out_and_reaps_the_child(monkeypatch):
    started = time.monotonic()
    proc = _run_child_expect_error(monkeypatch, _HALF_FRAME_CHILD)
    assert time.monotonic() - started < 3
    # The helper kills and waits for a child that will not exit on its own.
    assert proc.poll() is not None


@pytest.mark.parametrize('magic,version,length', [
    (0, 1, 40),
    (0x53505848, 2, 40),
    (0x53505848, 1, 32),
])
def test_wrong_reply_frames_are_rejected(monkeypatch, magic, version, length):
    raw = struct.pack('<IHHI', magic, version, 1, length) + b'\x00' * length
    script = _WRONG_RESPONSE_CHILD.replace('__RAW__', raw.hex())
    proc = _run_child_expect_error(monkeypatch, script)
    assert proc.poll() is not None


def test_seal_then_silence_times_out_and_kills(monkeypatch):
    started = time.monotonic()
    proc = _run_child_expect_error(monkeypatch, _SEAL_THEN_HANG_CHILD)
    assert time.monotonic() - started < 3
    assert proc.poll() is not None


def test_require_returns_blocked_within_the_ttl_when_the_child_hangs(monkeypatch):
    monkeypatch.setattr(shield, '_binaries',
                        lambda: ('shield.exe', 'guard.exe', 'core.exe'))
    monkeypatch.setattr(shield, '_is_frozen', lambda: True)
    monkeypatch.setattr(shield, '_go_attest', lambda nonce, store: b'A' * 32)
    monkeypatch.setattr(shield, '_rust_mix', lambda blob, nonce: b'B' * 32)
    box = _record_popen(monkeypatch)
    real_seal = shield._cpp_seal_check

    def fake_seal(exe, nonce, blob_go, blob_rust):
        return real_seal(sys.executable, nonce, blob_go, blob_rust,
                         argv=[sys.executable, '-c', _HALF_FRAME_CHILD])

    monkeypatch.setattr(shield, '_cpp_seal_check', fake_seal)
    started = time.monotonic()
    assert shield.require('store') == {
        'blocked': True, 'message': shield.SHIELD_MESSAGE}
    assert time.monotonic() - started < 3
    assert box['proc'].poll() is not None


def test_a_changed_sidecar_blocks_before_any_layer_runs(monkeypatch):
    monkeypatch.setattr(shield, '_binaries',
                        lambda: pytest.fail('must not probe binaries'))
    monkeypatch.setattr(shield.integrity, 'mismatch_message',
                        lambda: shield.integrity.REPAIR_MESSAGE)
    assert shield.require('store') == {
        'blocked': True, 'message': shield.integrity.REPAIR_MESSAGE}


def test_a_missing_sidecar_is_left_to_the_layer_check(monkeypatch):
    monkeypatch.setattr(shield.integrity, 'mismatch_message', lambda: None)
    monkeypatch.setattr(shield, '_binaries', lambda: (None, None, None))
    monkeypatch.setattr(shield, '_is_frozen', lambda: False)
    assert shield.require('store') == {'allowed': True, 'skipped': True}


def test_jhp_shield_zero_does_not_hide_a_changed_sidecar(monkeypatch):
    # The switch skips the derivation for local debugging; it must not make a
    # modified install valid.
    monkeypatch.setenv('JHP_SHIELD', '0')
    monkeypatch.setattr(shield.integrity, 'mismatch_message',
                        lambda: shield.integrity.REPAIR_MESSAGE)
    out = shield.require('store')
    assert out == {'blocked': True, 'message': shield.integrity.REPAIR_MESSAGE}


def test_selfcheck_is_skipped_when_the_binary_is_absent(monkeypatch):
    monkeypatch.setattr(shield, 'find_shield', lambda: None)
    assert shield.selfcheck() == {'ok': None, 'skipped': True}


def test_selfcheck_reads_the_sidecar_verdict(monkeypatch, tmp_path):
    exe = tmp_path / 'hxp-shield.exe'
    exe.write_bytes(b'stub')
    seen = {}

    class Proc:
        def __init__(self, code, err=b''):
            self.returncode = code
            self.stderr = err

    def fake_run(argv, **kwargs):
        seen['argv'] = argv
        seen['stdin'] = kwargs.get('stdin')
        return Proc(0)

    monkeypatch.setattr(shield.subprocess, 'run', fake_run)
    assert shield.selfcheck(str(exe)) == {'ok': True}
    assert seen['argv'] == [str(exe), '--selfcheck']
    assert seen['stdin'] is shield.subprocess.DEVNULL

    monkeypatch.setattr(shield.subprocess, 'run',
                        lambda argv, **kw: Proc(1, b'selfcheck: image\n'))
    assert shield.selfcheck(str(exe)) == {'ok': False, 'reason': 'image'}

    monkeypatch.setattr(shield.subprocess, 'run',
                        lambda argv, **kw: Proc(1, b'selfcheck: debugger\n'))
    assert shield.selfcheck(str(exe)) == {'ok': False, 'reason': 'debugger'}


def test_selfcheck_failure_to_start_is_reported_not_raised(monkeypatch, tmp_path):
    exe = tmp_path / 'hxp-shield.exe'
    exe.write_bytes(b'stub')

    def nope(argv, **kwargs):
        raise OSError('cannot start')

    monkeypatch.setattr(shield.subprocess, 'run', nope)
    assert shield.selfcheck(str(exe)) == {'ok': False, 'reason': 'unavailable'}


def test_read_exact_raises_when_the_pipe_stays_blocked():
    class BlockedPipe:
        def read(self, size):
            time.sleep(1)
            return b'\x00' * size

    started = time.monotonic()
    with pytest.raises(shield.ShieldError) as excinfo:
        shield._read_exact(BlockedPipe(), 4, timeout=0.05)
    assert 'timeout' in str(excinfo.value)
    assert time.monotonic() - started < 0.5


def test_account_guard_attest_failure_does_not_echo_the_nonce(
        monkeypatch, tmp_path):
    nonce = bytes.fromhex('00112233445566778899aabbccddeeff')
    monkeypatch.setattr(AG, 'find_guard', lambda: 'hxp-guard.exe')

    class Proc:
        returncode = 2
        stdout = '{"ok":false,"error":"attest failed"}\n'
        stderr = ''

    monkeypatch.setattr(AG.subprocess, 'run', lambda *a, **k: Proc())
    with pytest.raises(RuntimeError) as excinfo:
        AG.attest(nonce, str(tmp_path))
    assert nonce.hex() not in str(excinfo.value)

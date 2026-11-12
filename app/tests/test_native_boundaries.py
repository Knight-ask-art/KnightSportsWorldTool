"""Guardrails for the four-language native boundary and the passive decoy.

These are source-level checks, not a replacement for the release build's
``dumpbin /exports`` verification.  They fail fast when a future change adds a
readable FFI surface, turns a sidecar into a library, or wires the honeypot
into real login/submit code.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / 'app'
REPLICA = APP / 'replica'
CPP_DIR = ROOT / 'cpp' / 'hxp-shield'
RUST_DIR = ROOT / 'rust' / 'hxp-core'
GO_DIR = ROOT / 'go' / 'hxp-guard'

NATIVE_SOURCE_SUFFIXES = {'.cpp', '.cc', '.c', '.h', '.hpp', '.go', '.rs', '.toml'}
FORBIDDEN_NATIVE_MARKERS = (
    'extern "C"',
    '__declspec(dllexport)',
    '#[no_mangle]',
    '#[export_name',
    'import "C"',
    '//export',
    'c-shared',
    'c-archive',
    'LoadLibrary',
    'GetProcAddress',
)
FORBIDDEN_CARGO_TYPES = ('cdylib', 'staticlib', 'dylib')


def _read(path: Path) -> str:
    return path.read_text(encoding='utf-8', errors='ignore')


def _native_sources() -> list[Path]:
    roots = [CPP_DIR, RUST_DIR / 'src', GO_DIR]
    out: list[Path] = []
    for root in roots:
        if root.is_dir():
            out.extend(
                p for p in root.rglob('*')
                if p.is_file() and p.suffix.lower() in NATIVE_SOURCE_SUFFIXES
            )
    return out


def test_native_sources_do_not_expose_a_clear_ffi_surface():
    offenders: list[str] = []
    for path in _native_sources():
        text = _read(path)
        for marker in FORBIDDEN_NATIVE_MARKERS:
            if marker in text:
                offenders.append(f'{path.relative_to(ROOT)}: {marker}')
    assert not offenders, (
        'native boundary must stay process-to-process; found: ' + ', '.join(offenders)
    )


def test_rust_core_is_a_binary_not_a_cdylib():
    cargo = _read(RUST_DIR / 'Cargo.toml')
    for kind in FORBIDDEN_CARGO_TYPES:
        assert kind not in cargo, f'Cargo.toml must not build {kind}'
    assert '[[bin]]' in cargo, 'hxp-core must remain a standalone binary'
    # The process boundary is the point: a Rust library API would be a
    # readable call surface for a patcher.
    for path in (RUST_DIR / 'src').rglob('*.rs'):
        text = _read(path)
        assert 'pub extern' not in text
        assert 'extern "C"' not in text


def test_go_guard_stays_a_process_with_five_collectors():
    sources = list(GO_DIR.rglob('*.go'))
    assert sources
    for path in sources:
        text = _read(path)
        assert 'import "C"' not in text
        assert '//export' not in text
    attest = _read(GO_DIR / 'guard' / 'attest.go')
    assert attest.count('func() (string, error) {') >= 5
    assert 'go func(' in attest
    assert 'rng.Shuffle' in attest
    assert 'nonce' in attest.lower()


def test_cpp_shield_keeps_the_veh_state_machine_and_short_ttl():
    text = _read(CPP_DIR / 'main.cpp')
    assert 'kTtlMs = 3000' in text
    assert 'AddVectoredExceptionHandler' in text
    assert 'RaiseException' in text
    assert 'g_states' in text
    assert 'kOpSeal' in text and 'kOpCheck' in text
    assert 'HMAC' in text.upper()


def test_python_native_bridges_use_processes_not_ctypes():
    paths = {
        'shield.py': REPLICA / 'application' / 'shield.py',
        'rustcore.py': REPLICA / 'transport' / 'rustcore.py',
        'account_guard.py': REPLICA / 'application' / 'account_guard.py',
    }
    for name, path in paths.items():
        text = _read(path)
        for marker in ('ctypes', 'cffi', 'CDLL', 'WinDLL', 'LoadLibrary'):
            assert marker not in text, f'{name} must not load a native library ({marker})'
        assert 'subprocess' in text, f'{name} must use a child process'


def _imported_modules(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def test_decoy_is_passive_and_not_wired_into_real_flows():
    decoy_path = REPLICA / 'application' / 'license_decoy.py'
    tree = ast.parse(_read(decoy_path), filename=str(decoy_path))
    imported = _imported_modules(tree)
    assert imported <= {'__future__', 'time'}, f'decoy imports unexpected modules: {imported}'
    for forbidden in (
        'shield', 'tokenkeeper', 'realclient', 'login_flow', 'session_store',
        'subprocess', 'socket', 'requests', 'os', 'pathlib',
    ):
        matches = {name for name in imported if name.endswith(forbidden)}
        assert not matches, f'decoy must not import {matches}'
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not calls & {'open', 'exec', 'eval', '__import__', 'system', 'Popen'}

    # The decoy may be reached only by an explicit bridge method, never by the
    # real login/submit/session path.  Imports inside functions are allowed;
    # module-level imports are not.
    importers: list[str] = []
    top_level: list[str] = []
    for path in REPLICA.rglob('*.py'):
        if path == decoy_path or '__pycache__' in path.parts:
            continue
        source = _read(path)
        if 'license_decoy' not in source:
            continue
        rel = path.relative_to(REPLICA).as_posix()
        importers.append(rel)
        mod = ast.parse(source, filename=str(path))
        for node in mod.body:
            if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith('license_decoy'):
                top_level.append(rel)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.endswith('license_decoy'):
                        top_level.append(rel)
    assert importers == ['adapters/bridge_api.py'], importers
    assert not top_level, f'decoy must stay lazily imported: {top_level}'

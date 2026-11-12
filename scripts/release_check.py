#!/usr/bin/env python3
"""Pre-flight checks before tagging a release.

Usage:
    python scripts/release_check.py
    python scripts/release_check.py --deep

``--deep`` also re-hashes the assets in app/dist against latest.json (a few
seconds for the 140 MB zip + setup).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_RE = re.compile(r"^VERSION\s*=\s*'([^']+)'", re.M)


class Report:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def check(self, ok, label, detail=''):
        print(f'{"PASS" if ok else "FAIL"}  {label}' + (f'  {detail}' if detail else ''))
        if not ok:
            self.errors.append(label)

    def warn(self, ok, label, detail=''):
        print(f'{"PASS" if ok else "WARN"}  {label}' + (f'  {detail}' if detail else ''))
        if not ok:
            self.warnings.append(label)


def git(*args: str) -> str:
    return subprocess.check_output(
        ['git', *args], cwd=ROOT, text=True, encoding='utf-8',
        errors='replace').strip()


def read_version() -> str:
    text = (ROOT / 'app' / 'replica' / 'build_info.py').read_text(encoding='utf-8')
    match = VERSION_RE.search(text)
    if not match:
        raise SystemExit('VERSION not found in app/replica/build_info.py')
    return match.group(1)


def load_manifest(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except ValueError as exc:
        print(f'FAIL  {path.relative_to(ROOT)}  invalid JSON: {exc}')
        return {}


def check_manifest(report: Report, path: Path, version: str, label: str) -> dict | None:
    manifest = load_manifest(path)
    if manifest is None:
        report.warn(False, f'{label} exists', f'{path.relative_to(ROOT)} missing')
        return None
    if manifest == {}:
        report.check(False, f'{label} parses', str(path.relative_to(ROOT)))
        return None
    actual = manifest.get('version')
    report.check(actual == version, f'{label} version',
                 '' if actual == version else f'{actual} != {version}')
    rollback = (manifest.get('rollback') or {}).get('version')
    report.check(bool(rollback), f'{label} rollback', str(rollback or 'missing'))
    assets = manifest.get('assets') or {}
    for key, expected in (('windows-x64', 4), ('windows-x64-setup', 4)):
        asset = assets.get(key) or {}
        urls = asset.get('urls') or []
        report.check(len(urls) >= expected,
                     f'{label} {key} urls',
                     f'{len(urls)} (expected >= {expected})')
    rollback_urls = (manifest.get('rollback') or {}).get('urls') or []
    report.warn(len(rollback_urls) >= 4, f'{label} rollback urls',
                f'{len(rollback_urls)} (expected >= 4)')
    return manifest


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def check_signing_key(report: Report) -> None:
    """The public key is committed; the private key is not.  An empty constant
    ships a build that refuses every login, so fail before compiling."""
    text = (ROOT / 'app' / 'replica' / 'application' / 'integrity.py').read_text(
        encoding='utf-8')
    match = re.search(r"^MANIFEST_PUBKEY = '([0-9a-f]{64})'$", text, re.M)
    report.check(bool(match), 'manifest public key',
                 'embedded' if match else
                 'missing; run python scripts/manifest_sign.py keygen --apply')


def check_launcher(report: Report, version: str) -> None:
    """The packaged install must carry the launcher and its version file."""
    install = ROOT / 'app' / 'dist' / f'好想跑_v{version}'
    launcher = install / 'hxp-launcher.exe'
    info_path = install / 'build_info.json'
    report.check(launcher.is_file(), 'launcher exists',
                 str(launcher.relative_to(ROOT)))
    report.check(info_path.is_file(), 'build_info.json exists',
                 str(info_path.relative_to(ROOT)))
    if not info_path.is_file():
        return
    try:
        info = json.loads(info_path.read_text(encoding='utf-8'))
    except ValueError as exc:
        report.check(False, 'build_info.json parses', str(exc))
        return
    actual = str(info.get('version') or '')
    report.check(actual == version, 'build_info.json version',
                 '' if actual == version else f'{actual} != {version}')


def deep_integrity(report: Report, version: str) -> None:
    """Verify the signature and hashes of the packaged install, not the source."""
    install = ROOT / 'app' / 'dist' / f'好想跑_v{version}'
    manifest = install / 'runtime' / 'integrity.json'
    report.check(manifest.is_file(), 'deep integrity manifest',
                 str(manifest.relative_to(ROOT)))
    if not manifest.is_file():
        return
    sys.path.insert(0, str(ROOT / 'app'))
    from replica.application import integrity as I
    try:
        doc = json.loads(manifest.read_text(encoding='utf-8'))
    except ValueError as exc:
        report.check(False, 'deep integrity parses', str(exc))
        return
    report.check(doc.get('version') == I.MANIFEST_VERSION,
                 'deep integrity version', str(doc.get('version')))
    out = I.check_native(manifest_path=str(manifest), root=str(install))
    detail = 'ok' if out.get('ok') else json.dumps(
        {k: out.get(k) for k in ('reason', 'missing', 'mismatch') if out.get(k)},
        ensure_ascii=False)
    report.check(out.get('ok') is True, 'deep integrity verify', detail)


def deep_assets(report: Report, manifest: dict | None) -> None:
    if not manifest:
        return
    assets = manifest.get('assets') or {}
    for key in ('windows-x64', 'windows-x64-setup'):
        asset = assets.get(key) or {}
        name = asset.get('file')
        if not name:
            report.check(False, f'deep {key} file', 'missing')
            continue
        path = ROOT / 'app' / 'dist' / name
        report.check(path.is_file(), f'deep {key} exists', name)
        if not path.is_file():
            continue
        report.check(path.stat().st_size == asset.get('size'),
                     f'deep {key} size',
                     f'{path.stat().st_size} != {asset.get("size")}')
        report.check(sha256(path) == asset.get('sha256'),
                     f'deep {key} sha256', name)
    rollback = manifest.get('rollback') or {}
    name = rollback.get('file')
    if name:
        path = ROOT / 'app' / 'dist' / name
        report.check(path.is_file(), 'deep rollback exists', name)
        if path.is_file():
            report.check(path.stat().st_size == rollback.get('size'),
                         'deep rollback size', name)
            report.check(sha256(path) == rollback.get('sha256'),
                         'deep rollback sha256', name)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deep', action='store_true',
                        help='re-hash dist assets against latest.json')
    parser.add_argument('--source-only', action='store_true',
                        help='skip manifests/tag checks (pre-build)')
    parser.add_argument('--allow-dirty', action='store_true',
                        help='do not fail on an uncommitted working tree')
    args = parser.parse_args(argv)

    report = Report()
    version = read_version()
    print(f'version: {version}\n')

    # 1. every place that shows a version
    report.check(True, 'build_info', version)
    pyproject = (ROOT / 'app' / 'pyproject.toml').read_text(encoding='utf-8')
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
    actual = match.group(1) if match else 'missing'
    report.check(actual == version, 'pyproject version',
                 '' if actual == version else f'{actual} != {version}')
    usage = (ROOT / 'app' / 'packaging' / 'USAGE.txt').read_text(encoding='utf-8')
    report.check(f'v{version}' in usage, 'USAGE.txt version', f'v{version}')
    notes = ROOT / f'RELEASE_NOTES_{version}.md'
    report.warn(notes.is_file(), 'release notes',
                notes.name if notes.is_file() else f'{notes.name} missing')
    try:
        make_latest = (ROOT / 'app' / 'packaging' / 'make_latest.py').read_text(
            encoding='utf-8')
    except OSError:
        make_latest = ''
    report.check(f"'{version}': [" in make_latest, 'make_latest notes',
                 f"RELEASE_NOTES['{version}']")
    report.warn(f"DEFAULT_VERSION = '{version}'" in make_latest,
                'make_latest fallback', version)
    index = (ROOT / 'app' / 'frontend' / 'index.html').read_text(encoding='utf-8')
    report.check(index.count(f'v{version}') >= 3, 'index.html version',
                 f'{index.count(f"v{version}")} occurrences')

    # 2. NSIS is UTF-8 with BOM and carries the same version
    nsi = ROOT / 'app' / 'packaging' / 'installer.nsi'
    head = nsi.read_bytes()[:3]
    report.check(head == b'\xef\xbb\xbf', 'installer.nsi BOM', head.hex(' '))
    nsi_text = nsi.read_text(encoding='utf-8-sig')
    report.check(f'VERSION "{version}"' in nsi_text,
                 'installer.nsi version', version)
    report.check('!define EXENAME "hxp-launcher.exe"' in nsi_text,
                 'installer.nsi launcher entry', 'hxp-launcher.exe')

    # 2b. the shipping build must be able to sign its native manifest
    check_signing_key(report)

    # 3. manifests that drive the updater (post-build only)
    if not args.source_only:
        check_manifest(report, ROOT / 'github-release' / 'latest.json',
                       version, 'github-release/latest.json')
        dist_manifest = check_manifest(
            report, ROOT / 'app' / 'dist' / 'latest.json',
            version, 'dist/latest.json')
        check_launcher(report, version)
        if args.deep:
            report.check(dist_manifest is not None, 'deep dist manifest',
                         'app/dist/latest.json')
            deep_assets(report, dist_manifest)
            deep_integrity(report, version)

    # 4. release hygiene
    dirty = git('status', '--porcelain')
    report.warn(not dirty or args.allow_dirty, 'working tree clean',
                'dirty' if dirty else 'clean')
    if not args.source_only:
        tag_exists = subprocess.run(
            ['git', 'rev-parse', '-q', '--verify', f'refs/tags/v{version}'],
            cwd=ROOT, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL).returncode == 0
        report.warn(not tag_exists, f'tag v{version} absent',
                    'already exists' if tag_exists else 'ok')

    print()
    if report.errors:
        print(f'BLOCKED: {len(report.errors)} error(s)')
        return 1
    print(f'PASS: {len(report.warnings)} warning(s)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

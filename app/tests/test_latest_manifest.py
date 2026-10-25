"""latest.json: the auto-update manifest must be deterministic and honest."""
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import quote

from replica.build_info import VERSION

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'packaging'))

import make_latest  # noqa: E402


def _fake_zip(path: Path, payload: bytes = b'PK\x03\x04test') -> Path:
    path.write_bytes(payload)
    return path


def test_read_version_matches_build_info():
    assert make_latest.read_version(ROOT) == VERSION


def test_manifest_has_the_fields_an_updater_needs(tmp_path):
    zip_path = _fake_zip(tmp_path / '好想跑_v0.1.2_win64.zip')
    m = make_latest.build_manifest(
        '0.1.2', zip_path, released_at='2026-10-25T12:00:00+08:00')

    assert m['schema'] == 1
    assert m['app'] == 'haoxiangpao'
    assert m['version'] == '0.1.2'
    assert m['min_version'] == '0.1.1'
    assert m['channel'] == 'stable'
    assert m['released_at'] == '2026-10-25T12:00:00+08:00'
    assert m['notes']                      # current release cannot be silent

    asset = m['assets']['windows-x64']
    assert asset['file'] == zip_path.name
    assert asset['url'] == quote(zip_path.name)
    assert asset['size'] == zip_path.stat().st_size
    assert asset['sha256'] == hashlib.sha256(zip_path.read_bytes()).hexdigest()
    assert asset['format'] == 'zip'
    assert asset['layout'] == 'onedir'
    assert asset['entry'] == '好想跑.exe'


def test_absolute_base_url_is_joined_once(tmp_path):
    zip_path = _fake_zip(tmp_path / '好想跑_v0.1.2_win64.zip')
    m = make_latest.build_manifest(
        '0.1.2', zip_path, base_url='https://example.com/hxp/')
    url = m['assets']['windows-x64']['url']
    assert url.startswith('https://example.com/hxp/')
    assert url.count('https://') == 1
    assert url.endswith(quote(zip_path.name))


def test_rollback_is_included_only_when_the_zip_exists(tmp_path):
    zip_path = _fake_zip(tmp_path / '好想跑_v0.1.2_win64.zip')
    old = _fake_zip(tmp_path / '好想跑_v0.1.1_win64.zip', b'PK\x03\x04old')
    m = make_latest.build_manifest(
        '0.1.2', zip_path, rollback_version='0.1.1', rollback_zip=old)
    assert m['rollback']['version'] == '0.1.1'
    assert m['rollback']['sha256'] == hashlib.sha256(b'PK\x03\x04old').hexdigest()

    missing = tmp_path / 'missing.zip'
    m = make_latest.build_manifest(
        '0.1.2', zip_path, rollback_version='0.1.1', rollback_zip=missing)
    assert 'rollback' not in m


def test_write_manifest_is_utf8_json_with_trailing_newline(tmp_path):
    zip_path = _fake_zip(tmp_path / '好想跑_v0.1.2_win64.zip')
    m = make_latest.build_manifest('0.1.2', zip_path)
    out = make_latest.write_manifest(m, tmp_path / 'latest.json')
    raw = out.read_bytes()
    assert raw.endswith(b'\n')
    assert '好想跑'.encode('utf-8') in raw
    assert json.loads(raw.decode('utf-8'))['version'] == '0.1.2'


def test_cli_writes_the_manifest_next_to_the_zip(tmp_path):
    zip_path = _fake_zip(tmp_path / '好想跑_v0.1.2_win64.zip')
    code = make_latest.main([
        '--dist', str(tmp_path),
        '--version', '0.1.2',
        '--zip', str(zip_path),
        '--out', str(tmp_path / 'latest.json'),
        '--rollback-version', '',
    ])
    assert code == 0
    m = json.loads((tmp_path / 'latest.json').read_text(encoding='utf-8'))
    assert m['assets']['windows-x64']['size'] == zip_path.stat().st_size
    assert 'rollback' not in m


def test_installer_asset_is_added_when_present(tmp_path):
    version = '0.1.3'
    zip_path = _fake_zip(tmp_path / f'haoxiangpao_v{version}_win64.zip')
    setup = _fake_zip(
        tmp_path / f'haoxiangpao_v{version}_win64_setup.exe', b'MZsetup')
    m = make_latest.build_manifest(version, zip_path, setup_path=setup)

    # windows-x64 must stay the zip: v0.1.2 clients only understand that key.
    assert m['assets']['windows-x64']['format'] == 'zip'
    assert m['assets']['windows-x64']['file'].endswith('.zip')

    installer = m['assets']['windows-x64-setup']
    assert installer['format'] == 'installer'
    assert installer['layout'] == 'installer'
    assert installer['file'] == f'haoxiangpao_v{version}_win64_setup.exe'
    assert installer['size'] == setup.stat().st_size
    assert installer['sha256'] == hashlib.sha256(setup.read_bytes()).hexdigest()


def test_installer_asset_is_skipped_when_missing(tmp_path):
    zip_path = _fake_zip(tmp_path / 'haoxiangpao_v0.1.3_win64.zip')
    m = make_latest.build_manifest('0.1.3', zip_path)
    assert 'windows-x64-setup' not in m['assets']


def test_ascii_asset_names_need_no_percent_encoding(tmp_path):
    version = '0.1.3'
    zip_path = _fake_zip(tmp_path / f'haoxiangpao_v{version}_win64.zip')
    m = make_latest.build_manifest(version, zip_path,
                                   base_url='https://example.com/r')
    assert m['assets']['windows-x64']['url'] == (
        f'https://example.com/r/haoxiangpao_v{version}_win64.zip')

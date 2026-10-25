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

"""Mirror ranking for the updater.

GitHub release assets are unreachable from a lot of networks, so the manifest
carries several links.  Picking one by ping or by HEAD latency is wrong: some
CDNs answer HEAD from cache and still throttle the body, and a captive portal
happily returns 200 with an HTML page.
"""
import hashlib
import http.server
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from replica.application import updater

ROOT = Path(__file__).resolve().parents[1]
PACKAGING_DIR = str(ROOT / 'packaging')
if PACKAGING_DIR not in sys.path:
    sys.path.insert(0, PACKAGING_DIR)

import check_update_mirrors  # noqa: E402
import make_latest  # noqa: E402

ZIP = b'PK\x03\x04' + b'\x00' * 4096


class FakeResponse:
    """Just enough of ``requests.Response`` for the streaming code paths."""

    def __init__(self, body=b'', status=200, delay=0.0):
        self._body = body
        self._pos = 0
        self._started = False
        self.status_code = status
        self.delay = delay

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, size):
        if not self._started:
            self._started = True
            if self.delay:
                time.sleep(self.delay)
        while self._pos < len(self._body):
            chunk = self._body[self._pos:self._pos + size]
            self._pos += len(chunk)
            yield chunk


def test_fastest_mirror_wins(monkeypatch):
    def fake_get(url, **kwargs):
        return FakeResponse(ZIP, delay=0.3 if 'slow' in url else 0.0)

    monkeypatch.setattr(updater.requests, 'get', fake_get)
    ranked, scores = updater.rank_mirrors(
        ['https://slow.example/a.zip', 'https://fast.example/a.zip'],
        magic=b'PK\x03\x04', budget=2.0)
    assert ranked[0] == 'https://fast.example/a.zip'
    assert set(scores) == {'https://slow.example/a.zip',
                           'https://fast.example/a.zip'}


def test_an_html_200_is_not_a_mirror(monkeypatch):
    def fake_get(url, **kwargs):
        if 'portal' in url:
            return FakeResponse(b'<html>login required</html>')
        return FakeResponse(ZIP)

    monkeypatch.setattr(updater.requests, 'get', fake_get)
    ranked, scores = updater.rank_mirrors(
        ['https://portal.example/a.zip', 'https://good.example/a.zip'],
        magic=b'PK\x03\x04', budget=2.0)
    assert 'https://portal.example/a.zip' not in scores
    assert ranked == ['https://good.example/a.zip',
                      'https://portal.example/a.zip']


def test_everything_failing_keeps_the_manifest_order(monkeypatch):
    def fake_get(url, **kwargs):
        raise RuntimeError('connection reset')

    monkeypatch.setattr(updater.requests, 'get', fake_get)
    ranked, scores = updater.rank_mirrors(
        ['https://a.example/x.zip', 'https://b.example/x.zip'])
    assert ranked == ['https://a.example/x.zip', 'https://b.example/x.zip']
    assert scores == {}


def test_a_single_mirror_is_not_probed(monkeypatch):
    def fake_get(url, **kwargs):
        raise AssertionError('probe should not run for one URL')

    monkeypatch.setattr(updater.requests, 'get', fake_get)
    assert updater.rank_mirrors(['https://only.example/x.zip']) == (
        ['https://only.example/x.zip'], {})


def test_download_moves_to_the_next_mirror(monkeypatch, tmp_path):
    payload = ZIP
    sha = hashlib.sha256(payload).hexdigest()

    def fake_get(url, **kwargs):
        if 'bad' in url:
            return FakeResponse(b'PK\x03\x04' + b'\xff' * 4096)
        return FakeResponse(payload)

    monkeypatch.setattr(updater.requests, 'get', fake_get)
    monkeypatch.setattr(updater, 'rank_mirrors',
                        lambda urls, *a, **k: (list(urls), {}))
    info = updater.download(['https://bad.example/x.zip',
                             'https://good.example/x.zip'],
                            str(tmp_path), sha256=sha, size=len(payload),
                            file='x.zip')
    assert info['mirror'] == 'https://good.example/x.zip'
    assert info['sha256'] == sha
    assert info['tried'] and 'bad.example' in info['tried'][0]
    assert not list(tmp_path.glob('*.part'))


def test_download_gives_up_when_every_mirror_is_wrong(monkeypatch, tmp_path):
    def fake_get(url, **kwargs):
        return FakeResponse(b'PK\x03\x04' + b'\xff' * 16)

    monkeypatch.setattr(updater.requests, 'get', fake_get)
    monkeypatch.setattr(updater, 'rank_mirrors',
                        lambda urls, *a, **k: (list(urls), {}))
    try:
        updater.download(['https://a.example/x.zip', 'https://b.example/x.zip'],
                         str(tmp_path), sha256='0' * 64, file='x.zip')
    except ValueError as e:
        assert 'sha256' in str(e)
    else:  # pragma: no cover - the assertion above is the point
        raise AssertionError('a bad checksum must not be accepted')
    assert not list(tmp_path.glob('*.part'))


def test_normalize_keeps_every_usable_mirror():
    manifest = {
        'schema': 1, 'app': 'haoxiangpao', 'version': '0.1.4',
        'assets': {'windows-x64': {
            'file': 'x.zip', 'url': 'https://a.example/x.zip',
            'urls': ['https://a.example/x.zip', 'https://b.example/x.zip',
                     'ftp://nope/x.zip', ''],
            'sha256': '0' * 64, 'size': 10,
        }},
    }
    out = updater.normalize(manifest, source='https://m.example/latest.json',
                            current='0.1.3', prefer='zip')
    assert out['url'] == 'https://a.example/x.zip'
    assert out['urls'] == ['https://a.example/x.zip',
                           'https://b.example/x.zip']


def test_normalize_without_urls_still_works():
    manifest = {
        'schema': 1, 'app': 'haoxiangpao', 'version': '0.1.4',
        'assets': {'windows-x64': {
            'file': 'x.zip', 'url': 'https://a.example/x.zip',
            'sha256': '0' * 64,
        }},
    }
    out = updater.normalize(manifest, source='https://m.example/latest.json',
                            current='0.1.3', prefer='zip')
    assert out['urls'] == ['https://a.example/x.zip']


# ---- M1: mirror registry + make_latest + release health check ------------

def _write_asset(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    return path


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def test_default_mirror_registry_seeds_20_to_30_candidates():
    reg_path = ROOT / 'packaging' / 'update_mirrors.json'
    data = make_latest.load_mirror_registry(reg_path)
    mirrors = data['mirrors']
    assert 20 <= len(mirrors) <= 30
    assert mirrors[0]['name'] == 'official'
    assert mirrors[0]['template'] == '{base}'
    assert mirrors[0]['priority'] == 0
    assert mirrors[0]['enabled'] is True

    names = [m['name'] for m in mirrors]
    templates = [m['template'] for m in mirrors]
    assert len(names) == len(set(names))
    assert len(templates) == len(set(templates))
    for entry in mirrors[1:]:
        assert entry['status'] == 'candidate'
        assert entry['template'].startswith('https://')
        assert '{base}' in entry['template']


def test_registry_expands_enabled_official_first_dedupe_and_skips_disabled(tmp_path):
    zip_path = _write_asset(tmp_path / 'haoxiangpao_v0.3.0_win64.zip', ZIP)
    setup_path = _write_asset(tmp_path / 'haoxiangpao_v0.3.0_win64_setup.exe', b'MZ' + b'\x00' * 128)
    rb_path = _write_asset(tmp_path / 'haoxiangpao_v0.2.9_win64.zip', ZIP)

    reg = {
        'schema': 1,
        'mirrors': [
            {'name': 'slow', 'template': 'https://slow.example/{base}', 'priority': 30, 'enabled': True},
            {'name': 'disabled', 'template': 'https://off.example/{base}', 'priority': 5, 'enabled': False},
            {'name': 'official', 'template': '{base}', 'priority': 99, 'enabled': True},
            {'name': 'fast', 'template': 'https://fast.example/{base}', 'priority': 10, 'enabled': True},
            {'name': 'dup', 'template': 'https://fast.example/{base}', 'priority': 20, 'enabled': True},
        ],
    }
    base = 'https://github.com/o/r/releases/download/v0.3.0'
    rb_base = 'https://github.com/o/r/releases/download/v0.2.9'
    manifest = make_latest.build_manifest(
        '0.3.0',
        zip_path,
        base_url=base,
        mirrors=(
            'https://fast.example/' + base,  # duplicate of registry entry
            'https://manual.example/' + base,
        ),
        mirror_registry=reg,
        setup_path=setup_path,
        rollback_version='0.2.9',
        rollback_zip=rb_path,
        rollback_base_url=rb_base,
        rollback_mirrors=('https://rb-manual.example/' + rb_base,),
    )

    expected_zip_urls = [
        f'{base}/{zip_path.name}',
        f'https://fast.example/{base}/{zip_path.name}',
        f'https://slow.example/{base}/{zip_path.name}',
        f'https://manual.example/{base}/{zip_path.name}',
    ]
    assert manifest['assets']['windows-x64']['url'] == expected_zip_urls[0]
    assert manifest['assets']['windows-x64']['urls'] == expected_zip_urls

    expected_setup_urls = [
        f'{base}/{setup_path.name}',
        f'https://fast.example/{base}/{setup_path.name}',
        f'https://slow.example/{base}/{setup_path.name}',
        f'https://manual.example/{base}/{setup_path.name}',
    ]
    assert manifest['assets']['windows-x64-setup']['url'] == expected_setup_urls[0]
    assert manifest['assets']['windows-x64-setup']['urls'] == expected_setup_urls

    expected_rb_urls = [
        f'{rb_base}/{rb_path.name}',
        f'https://fast.example/{rb_base}/{rb_path.name}',
        f'https://slow.example/{rb_base}/{rb_path.name}',
        f'https://rb-manual.example/{rb_base}/{rb_path.name}',
    ]
    assert manifest['rollback']['url'] == expected_rb_urls[0]
    assert manifest['rollback']['urls'] == expected_rb_urls


def test_registry_uses_official_base_when_primary_is_a_proxy(tmp_path):
    zip_path = _write_asset(tmp_path / 'haoxiangpao_v0.3.0_win64.zip', ZIP)
    reg = {
        'schema': 1,
        'mirrors': [
            {'name': 'official', 'template': '{base}', 'priority': 0, 'enabled': True},
            {'name': 'fast', 'template': 'https://fast.example/{base}', 'priority': 10, 'enabled': True},
        ],
    }
    official = 'https://github.com/o/r/releases/download/v0.3.0'
    primary = 'https://gh-proxy.com/' + official
    manifest = make_latest.build_manifest(
        '0.3.0',
        zip_path,
        base_url=primary,
        registry_base_url=official,
        mirror_registry=reg,
    )
    urls = manifest['assets']['windows-x64']['urls']
    expected = [
        f'{primary}/{zip_path.name}',
        f'{official}/{zip_path.name}',
        f'https://fast.example/{official}/{zip_path.name}',
    ]
    assert manifest['assets']['windows-x64']['url'] == expected[0]
    assert urls == expected


def test_cli_registry_base_url_avoids_double_proxy(tmp_path):
    zip_path = _write_asset(tmp_path / 'haoxiangpao_v0.3.0_win64.zip', ZIP)
    reg = {
        'schema': 1,
        'mirrors': [
            {'name': 'official', 'template': '{base}', 'priority': 0, 'enabled': True},
            {'name': 'fast', 'template': 'https://fast.example/{base}', 'priority': 10, 'enabled': True},
        ],
    }
    reg_path = tmp_path / 'mirrors.json'
    reg_path.write_text(json.dumps(reg), encoding='utf-8')
    official = 'https://github.com/o/r/releases/download/v0.3.0'
    primary = 'https://gh-proxy.com/' + official
    out_path = tmp_path / 'latest.json'

    assert make_latest.main([
        '--dist', str(tmp_path),
        '--version', '0.3.0',
        '--zip', str(zip_path),
        '--out', str(out_path),
        '--base-url', primary,
        '--registry-base-url', official,
        '--mirror-registry', str(reg_path),
        '--rollback-version', '',
    ]) == 0
    urls = json.loads(out_path.read_text(encoding='utf-8'))['assets']['windows-x64']['urls']
    assert urls == [
        f'{primary}/{zip_path.name}',
        f'{official}/{zip_path.name}',
        f'https://fast.example/{official}/{zip_path.name}',
    ]


def test_health_filter_uses_registry_base_for_proxy_primary():
    official = 'https://github.com/o/r/releases/download/v0.3.0'
    primary = 'https://gh-proxy.com/' + official
    name = 'haoxiangpao_v0.3.0_win64.zip'
    registry = {
        'schema': 1,
        'mirrors': [
            {'name': 'official', 'template': '{base}', 'priority': 0, 'enabled': True},
            {'name': 'fast', 'template': 'https://fast.example/{base}', 'priority': 10, 'enabled': True},
            {'name': 'dead', 'template': 'https://dead.example/{base}', 'priority': 20, 'enabled': True},
        ],
    }
    manifest = {
        'assets': {
            'windows-x64': {
                'file': name,
                'url': f'{primary}/{name}',
                'urls': [
                    f'{primary}/{name}',
                    f'{official}/{name}',
                    f'https://fast.example/{official}/{name}',
                    f'https://dead.example/{official}/{name}',
                ],
            },
        },
    }
    results = {
        f'{primary}/{name}': {'ok': True},
        f'{official}/{name}': {'ok': True},
        f'https://fast.example/{official}/{name}': {'ok': True},
        f'https://dead.example/{official}/{name}': {'ok': False},
    }
    filtered = check_update_mirrors.filter_registry(
        registry, manifest, results, registry_base_url=official,
    )
    assert [m['name'] for m in filtered['mirrors']] == ['official', 'fast']


def test_registry_caps_urls_at_40(tmp_path):
    zip_path = _write_asset(tmp_path / 'haoxiangpao_v0.3.0_win64.zip', ZIP)
    mirrors = [{'name': 'official', 'template': '{base}', 'priority': 0, 'enabled': True}]
    for i in range(50):
        mirrors.append({
            'name': f'm{i}',
            'template': f'https://m{i}.example/{{base}}',
            'priority': i + 1,
            'enabled': True,
        })
    manifest = make_latest.build_manifest(
        '0.3.0',
        zip_path,
        base_url='https://github.com/o/r/releases/download/v0.3.0',
        mirror_registry={'schema': 1, 'mirrors': mirrors},
    )
    urls = manifest['assets']['windows-x64']['urls']
    assert len(urls) == 40
    assert urls[0].startswith('https://github.com/')


@pytest.mark.parametrize('bad_template', [
    'http://insecure.example/{base}',
    'file:///tmp/{base}',
    'ftp://files.example/{base}',
    'https://no-placeholder.example/static',
])
def test_registry_rejects_non_https_and_missing_base(tmp_path, bad_template):
    zip_path = _write_asset(tmp_path / 'haoxiangpao_v0.3.0_win64.zip', ZIP)
    reg_path = tmp_path / 'bad_mirrors.json'
    reg_path.write_text(json.dumps({
        'schema': 1,
        'mirrors': [
            {'name': 'official', 'template': '{base}', 'priority': 0, 'enabled': True},
            {'name': 'bad', 'template': bad_template, 'priority': 10, 'enabled': True},
        ],
    }), encoding='utf-8')
    out_path = tmp_path / 'latest.json'

    with pytest.raises(ValueError):
        make_latest.load_mirror_registry(reg_path)

    code = make_latest.main([
        '--dist', str(tmp_path),
        '--version', '0.3.0',
        '--zip', str(zip_path),
        '--out', str(out_path),
        '--base-url', 'https://github.com/o/r/releases/download/v0.3.0',
        '--mirror-registry', str(reg_path),
        '--rollback-version', '',
    ])
    assert code == 2
    assert not out_path.exists()


@pytest.mark.parametrize('rel_base', ['', 'files/rel'])
def test_relative_base_url_skips_registry_with_warning(tmp_path, capsys, rel_base):
    zip_path = _write_asset(tmp_path / 'haoxiangpao_v0.3.0_win64.zip', ZIP)
    out_path = tmp_path / 'latest.json'

    code = make_latest.main([
        '--dist', str(tmp_path),
        '--version', '0.3.0',
        '--zip', str(zip_path),
        '--out', str(out_path),
        '--base-url', rel_base,
        '--rollback-version', '',
    ])
    assert code == 0
    err = capsys.readouterr().err
    assert 'warning' in err.lower()
    manifest = json.loads(out_path.read_text(encoding='utf-8'))
    asset = manifest['assets']['windows-x64']
    expected = f'{rel_base}/{zip_path.name}' if rel_base else zip_path.name
    assert asset['url'] == expected
    assert 'urls' not in asset


def test_require_mirrors_threshold_passes_and_fails(tmp_path):
    zip_path = _write_asset(tmp_path / 'haoxiangpao_v0.3.0_win64.zip', ZIP)
    reg_path = tmp_path / 'mirrors.json'
    reg_path.write_text(json.dumps({
        'schema': 1,
        'mirrors': [
            {'name': 'official', 'template': '{base}', 'priority': 0, 'enabled': True},
            {'name': 'm1', 'template': 'https://m1.example/{base}', 'priority': 10, 'enabled': True},
            {'name': 'm2', 'template': 'https://m2.example/{base}', 'priority': 20, 'enabled': True},
            {'name': 'm3', 'template': 'https://m3.example/{base}', 'priority': 30, 'enabled': False},
        ],
    }), encoding='utf-8')
    out_ok = tmp_path / 'ok.json'
    out_fail = tmp_path / 'fail.json'
    out_rel = tmp_path / 'rel.json'

    common = [
        '--dist', str(tmp_path),
        '--version', '0.3.0',
        '--zip', str(zip_path),
        '--mirror-registry', str(reg_path),
        '--rollback-version', '',
    ]
    assert make_latest.main(common + [
        '--base-url', 'https://github.com/o/r/releases/download/v0.3.0',
        '--require-mirrors', '2',
        '--out', str(out_ok),
    ]) == 0
    assert out_ok.is_file()

    assert make_latest.main(common + [
        '--base-url', 'https://github.com/o/r/releases/download/v0.3.0',
        '--require-mirrors', '3',
        '--out', str(out_fail),
    ]) == 2
    assert not out_fail.exists()

    assert make_latest.main(common + [
        '--base-url', '',
        '--require-mirrors', '1',
        '--out', str(out_rel),
    ]) == 2
    assert not out_rel.exists()


def test_no_mirror_registry_keeps_manifest_unchanged_for_old_clients(tmp_path):
    zip_path = _write_asset(tmp_path / 'haoxiangpao_v0.3.0_win64.zip', ZIP)
    out_single = tmp_path / 'single.json'
    code = make_latest.main([
        '--dist', str(tmp_path),
        '--version', '0.3.0',
        '--zip', str(zip_path),
        '--out', str(out_single),
        '--base-url', 'https://github.com/o/r/releases/download/v0.3.0',
        '--no-mirror-registry',
        '--rollback-version', '',
    ])
    assert code == 0
    single = json.loads(out_single.read_text(encoding='utf-8'))
    assert single['assets']['windows-x64']['url'] == (
        f'https://github.com/o/r/releases/download/v0.3.0/{zip_path.name}'
    )
    assert 'urls' not in single['assets']['windows-x64']


def test_health_checker_local_http_server_and_exit_codes(tmp_path):
    recorded_ranges = []
    stop_slow = threading.Event()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            recorded_ranges.append((self.path, self.headers.get('Range')))
            if self.path == '/good/pkg.zip':
                self.send_response(206)
                self.end_headers()
                self.wfile.write(ZIP)
            elif self.path == '/mirror1/pkg.zip':
                self.send_response(200)
                self.end_headers()
                self.wfile.write(ZIP)
            elif self.path == '/good/setup.exe':
                self.send_response(206)
                self.end_headers()
                self.wfile.write(b'MZ' + b'\x00' * 512)
            elif self.path == '/mirror1/setup.exe':
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'MZ' + b'\x00' * 512)
            elif self.path == '/portal/pkg.zip':
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'<html>captive portal</html>')
            elif self.path == '/slow/pkg.zip':
                stop_slow.wait(3.0)
                try:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(ZIP)
                except OSError:
                    pass
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):  # noqa: A002
            return

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    dead_port = _free_port()

    try:
        base = f'http://127.0.0.1:{port}'
        manifest = {
            'schema': 1,
            'app': 'haoxiangpao',
            'version': '0.3.0',
            'assets': {
                'windows-x64': {
                    'file': 'pkg.zip',
                    'format': 'zip',
                    'url': f'{base}/good/pkg.zip',
                    'urls': [
                        f'{base}/good/pkg.zip',
                        f'{base}/mirror1/pkg.zip',
                        f'{base}/portal/pkg.zip',
                        f'{base}/missing/pkg.zip',
                        f'{base}/slow/pkg.zip',
                        f'http://127.0.0.1:{dead_port}/dead/pkg.zip',
                    ],
                },
                'windows-x64-setup': {
                    'file': 'setup.exe',
                    'format': 'installer',
                    'url': f'{base}/good/setup.exe',
                    'urls': [
                        f'{base}/good/setup.exe',
                        f'{base}/mirror1/setup.exe',
                        f'{base}/missing/setup.exe',
                    ],
                },
            },
        }
        manifest_path = tmp_path / 'latest.json'
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')

        # No writes unless --out/--write-registry is supplied.
        before = set(tmp_path.iterdir())
        code_ok = check_update_mirrors.main([
            '--manifest', str(manifest_path),
            '--min-healthy', '2',
            '--budget', '0.8',
        ])
        assert code_ok == 0
        assert set(tmp_path.iterdir()) == before

        out_health = tmp_path / 'mirror-health.json'
        code_fail = check_update_mirrors.main([
            '--manifest', str(manifest_path),
            '--min-healthy', '3',
            '--budget', '0.8',
            '--out', str(out_health),
        ])
        assert code_fail == 1
        assert out_health.is_file()

        report = json.loads(out_health.read_text(encoding='utf-8'))
        assert report[f'{base}/good/pkg.zip']['ok'] is True
        assert report[f'{base}/good/pkg.zip']['status'] == 206
        assert report[f'{base}/good/pkg.zip']['ttfb_ms'] is not None
        assert report[f'{base}/good/pkg.zip']['speed_bps'] > 0
        assert report[f'{base}/good/pkg.zip']['error'] is None

        assert report[f'{base}/mirror1/pkg.zip']['ok'] is True
        assert report[f'{base}/good/setup.exe']['ok'] is True
        assert report[f'{base}/mirror1/setup.exe']['ok'] is True
        assert report[f'{base}/portal/pkg.zip']['ok'] is False
        assert 'magic' in report[f'{base}/portal/pkg.zip']['error']
        assert report[f'{base}/missing/pkg.zip']['ok'] is False
        assert report[f'{base}/missing/pkg.zip']['status'] == 404
        assert report[f'{base}/slow/pkg.zip']['ok'] is False
        assert report[f'http://127.0.0.1:{dead_port}/dead/pkg.zip']['ok'] is False

        assert any(
            path == '/good/pkg.zip' and rng == 'bytes=0-65535'
            for path, rng in recorded_ranges
        )
    finally:
        stop_slow.set()
        server.shutdown()
        server.server_close()


def test_health_checker_write_registry_filters_healthy_only(monkeypatch, tmp_path):
    base = 'https://github.com/o/r/releases/download/v0.3.0'
    registry = {
        'schema': 1,
        'mirrors': [
            {'name': 'official', 'template': '{base}', 'priority': 0, 'enabled': True},
            {'name': 'good1', 'template': 'https://good1.example/{base}', 'priority': 10, 'enabled': True},
            {'name': 'portal', 'template': 'https://portal.example/{base}', 'priority': 20, 'enabled': True},
            {'name': 'dead', 'template': 'https://dead.example/{base}', 'priority': 30, 'enabled': True},
            {'name': 'disabled', 'template': 'https://off.example/{base}', 'priority': 40, 'enabled': False},
        ],
    }
    zip_path = _write_asset(tmp_path / 'haoxiangpao_v0.3.0_win64.zip', ZIP)
    setup_path = _write_asset(tmp_path / 'haoxiangpao_v0.3.0_win64_setup.exe', b'MZ' + b'\x00' * 128)
    reg_path = tmp_path / 'mirrors.json'
    reg_path.write_text(json.dumps(registry), encoding='utf-8')

    manifest = make_latest.build_manifest(
        '0.3.0',
        zip_path,
        base_url=base,
        mirror_registry=reg_path,
        setup_path=setup_path,
    )
    manifest_path = tmp_path / 'latest.json'
    make_latest.write_manifest(manifest, manifest_path)

    def fake_get(url, **kwargs):
        assert kwargs['headers']['Range'] == 'bytes=0-65535'
        if 'portal.example' in url:
            return FakeResponse(b'<html>oops</html>', status=200)
        if 'dead.example' in url:
            return FakeResponse(b'', status=502)
        if url.endswith('.exe'):
            return FakeResponse(b'MZ' + b'\x00' * 512, status=206)
        return FakeResponse(ZIP, status=206)

    monkeypatch.setattr(check_update_mirrors.requests, 'get', fake_get)

    filtered_path = tmp_path / 'healthy_mirrors.json'
    code = check_update_mirrors.main([
        '--manifest', str(manifest_path),
        '--min-healthy', '2',
        '--registry', str(reg_path),
        '--write-registry', str(filtered_path),
    ])
    assert code == 0

    filtered = make_latest.load_mirror_registry(filtered_path)
    assert [m['name'] for m in filtered['mirrors']] == ['official', 'good1']
    assert 'healthy_mirrors.json' in str(filtered_path)

    # Filtered registry feeds straight back into make_latest --require-mirrors.
    final_manifest_path = tmp_path / 'final.json'
    assert make_latest.main([
        '--dist', str(tmp_path),
        '--version', '0.3.0',
        '--zip', str(zip_path),
        '--setup', str(setup_path),
        '--base-url', base,
        '--mirror-registry', str(filtered_path),
        '--require-mirrors', '1',
        '--rollback-version', '',
        '--out', str(final_manifest_path),
    ]) == 0
    final = json.loads(final_manifest_path.read_text(encoding='utf-8'))
    assert len(final['assets']['windows-x64']['urls']) == 2

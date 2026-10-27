"""Mirror ranking for the updater.

GitHub release assets are unreachable from a lot of networks, so the manifest
carries several links.  Picking one by ping or by HEAD latency is wrong: some
CDNs answer HEAD from cache and still throttle the body, and a captive portal
happily returns 200 with an HTML page.
"""
import hashlib
import time

from replica.application import updater

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

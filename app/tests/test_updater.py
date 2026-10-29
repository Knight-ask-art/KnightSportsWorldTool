"""Auto-update manifest: version math, URL resolution, download verification."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
import zipfile
from unittest import mock

from replica.adapters import bridge_api as RA
from replica.application import updater


def _manifest(version='9.9.9', url='pkg.zip', **extra):
    data = {
        'schema': 1,
        'app': 'haoxiangpao',
        'version': version,
        'notes': ['新增东西', 123, '', 'x' * 400],
        'assets': {'windows-x64': {
            'file': '好想跑_v%s_win64.zip' % version,
            'url': url,
            'size': 123,
            'sha256': 'a' * 64,
        }},
    }
    data.update(extra)
    return data


class VersionTest(unittest.TestCase):
    def test_parse_accepts_prefix_and_suffix(self):
        self.assertEqual(updater.parse_version('v0.1.2+abc'), (0, 1, 2))
        self.assertEqual(updater.parse_version(' 1.2 '), (1, 2))
        self.assertIsNone(updater.parse_version('nightly'))

    def test_compare_pads_short_versions(self):
        self.assertTrue(updater.is_newer('0.1.3', '0.1.2'))
        self.assertTrue(updater.is_newer('0.2', '0.1.9'))
        self.assertFalse(updater.is_newer('0.1.2', '0.1.2'))
        self.assertTrue(updater.same_version('0.1.2', '0.1.2.0'))


class NormalizeTest(unittest.TestCase):
    def test_newer_manifest_is_available(self):
        result = updater.normalize(_manifest(), 'https://example.com/latest.json',
                                   current='0.1.2', checked_at=1)
        self.assertEqual(result['state'], 'available')
        self.assertEqual(result['latest'], '9.9.9')
        self.assertEqual(result['url'], 'https://example.com/pkg.zip')
        self.assertEqual(result['checked_at'], 1)

    def test_relative_url_resolves_against_the_manifest(self):
        result = updater.normalize(
            _manifest(url='files/a.zip'),
            'https://example.com/releases/latest.json', current='0.1.2')
        self.assertEqual(result['url'],
                         'https://example.com/releases/files/a.zip')

    def test_same_and_older_versions_are_not_updates(self):
        same = updater.normalize(_manifest(version='0.1.2'),
                                 'https://example.com/latest.json',
                                 current='0.1.2')
        older = updater.normalize(_manifest(version='0.1.1'),
                                  'https://example.com/latest.json',
                                  current='0.1.2')
        self.assertEqual(same['state'], 'current')
        self.assertEqual(older['state'], 'ahead')

    def test_notes_are_cleaned(self):
        result = updater.normalize(_manifest(), 'https://example.com/latest.json')
        self.assertEqual(len(result['notes']), 2)
        self.assertTrue(all(len(n) <= 300 for n in result['notes']))

    def test_bad_manifests_raise(self):
        cases = [
            {'schema': 2, 'version': '1.0.0', 'assets': {}},
            {'schema': 1, 'app': 'other', 'version': '1.0.0', 'assets': {}},
            {'schema': 1, 'version': '', 'assets': {}},
            {'schema': 1, 'version': '1.0.0', 'assets': {}},
            _manifest(url='file:///etc/passwd'),
        ]
        for manifest in cases:
            with self.assertRaises(ValueError):
                updater.normalize(manifest, 'https://example.com/latest.json')

    def test_installer_asset_is_selected_when_asked(self):
        manifest = _manifest()
        manifest['assets']['windows-x64-setup'] = {
            'file': 'haoxiangpao_v9.9.9_win64_setup.exe',
            'url': 'setup.exe', 'size': 1, 'sha256': 'b' * 64,
            'format': 'installer', 'layout': 'installer',
        }
        result = updater.normalize(manifest,
                                   'https://example.com/latest.json',
                                   prefer='installer')
        self.assertEqual(result['format'], 'installer')
        self.assertEqual(result['layout'], 'installer')
        self.assertEqual(result['url'], 'https://example.com/setup.exe')

    def test_installer_falls_back_to_the_zip(self):
        result = updater.normalize(_manifest(),
                                   'https://example.com/latest.json',
                                   prefer='installer')
        self.assertEqual(result['format'], 'zip')

    def test_installer_without_sha256_is_refused(self):
        manifest = _manifest()
        manifest['assets']['windows-x64-setup'] = {
            'file': 'setup.exe', 'url': 'setup.exe', 'format': 'installer'}
        with self.assertRaises(ValueError):
            updater.normalize(manifest, 'https://example.com/latest.json',
                              prefer='installer')


class InstallStateTest(unittest.TestCase):
    def test_source_checkout_is_never_treated_as_installed(self):
        with mock.patch.object(updater, 'is_compiled', return_value=False):
            self.assertFalse(updater.is_installed())

    def test_installed_copy_must_live_under_the_registry_dir(self):
        with mock.patch.object(updater, 'is_compiled', return_value=True), \
             mock.patch.object(updater, 'installed_dir',
                               return_value=r'C:\Users\me\AppData\Local\Programs\好想跑'):
            self.assertTrue(updater.is_installed(
                r'C:\Users\me\AppData\Local\Programs\好想跑\current'))
            self.assertFalse(updater.is_installed(r'D:\portable\好想跑_v0.1.2'))


class CheckTest(unittest.TestCase):
    def test_falls_back_to_the_next_manifest_url(self):
        calls = []

        def fake_fetch(url):
            calls.append(url)
            if len(calls) == 1:
                raise RuntimeError('blocked')
            return _manifest()

        with mock.patch.object(updater, 'manifest_urls',
                               return_value=('https://a/latest.json',
                                             'https://b/latest.json')), \
             mock.patch.object(updater, 'fetch_manifest', side_effect=fake_fetch):
            result = updater.check(current='0.1.2')

        self.assertEqual(result['state'], 'available')
        self.assertEqual(result['source'], 'https://b/latest.json')
        self.assertEqual(result['url'], 'https://b/pkg.zip')

    def test_all_sources_failing_is_reported_honestly(self):
        with mock.patch.object(updater, 'manifest_urls',
                               return_value=('https://a/latest.json',)), \
             mock.patch.object(updater, 'fetch_manifest',
                               side_effect=RuntimeError('offline')):
            result = updater.check(current='0.1.2')
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['current'], '0.1.2')
        self.assertIn('offline', result['message'])


class _FakeResponse:
    def __init__(self, chunks, status=200):
        self._chunks = list(chunks)
        self.status_code = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, size=1024):
        yield from self._chunks


class DownloadTest(unittest.TestCase):
    def test_second_start_returns_the_running_state(self):
        # `start_download` assigns `_DOWNLOAD_STATE`; without the global
        # declaration the early-return path raised UnboundLocalError.
        class _Alive:
            def is_alive(self):
                return True

        old_thread, old_state = updater._DOWNLOAD_THREAD, updater._DOWNLOAD_STATE
        try:
            updater._DOWNLOAD_THREAD = _Alive()
            updater._DOWNLOAD_STATE = {'state': 'downloading', 'received': 7}
            state = updater.start_download('https://example.com/a.zip',
                                           tempfile.gettempdir())
            self.assertEqual(state, {'state': 'downloading', 'received': 7})
        finally:
            updater._DOWNLOAD_THREAD = old_thread
            updater._DOWNLOAD_STATE = old_state

    def test_download_verifies_hash_and_size(self):
        payload = b'zip-bytes' * 500
        sha = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                    updater.requests, 'get',
                    return_value=_FakeResponse([payload[:100], payload[100:]])):
                info = updater.download('https://example.com/a.zip', tmp,
                                        sha256=sha, size=len(payload),
                                        file='好想跑_v9.zip')
            self.assertTrue(os.path.isfile(info['path']))
            self.assertEqual(info['size'], len(payload))
            self.assertEqual(info['sha256'], sha)
            self.assertEqual(info['file'], '好想跑_v9.zip')

    def test_bad_hash_leaves_no_partial_file(self):
        payload = b'zip-bytes'
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(updater.requests, 'get',
                                   return_value=_FakeResponse([payload])):
                with self.assertRaises(ValueError):
                    updater.download('https://example.com/a.zip', tmp,
                                     sha256='0' * 64, size=len(payload),
                                     file='x.zip')
            self.assertEqual(os.listdir(tmp), [])

    def test_non_http_url_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                updater.download('file:///c:/windows/notepad.exe', tmp)


class BridgeTest(unittest.TestCase):
    def test_check_update_never_turns_a_network_error_into_500(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = RA.ReplicaApi('http://x', 'http://x', b'', tmp)
            with mock.patch.object(updater, 'manifest_urls',
                                   return_value=('https://a/latest.json',)), \
                 mock.patch.object(updater, 'fetch_manifest',
                                   side_effect=RuntimeError('offline')):
                result = api.real_check_update()
        self.assertEqual(result['code'], 200)
        self.assertEqual(result['data']['state'], 'failed')

    def test_download_update_rejects_a_non_http_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = RA.ReplicaApi('http://x', 'http://x', b'', tmp)
            result = api.real_download_update('file:///c:/windows/notepad.exe')
        self.assertEqual(result['code'], 500)
        self.assertIn('下载失败', result['message'])

    def test_apply_update_refuses_a_source_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            updates = os.path.join(tmp, 'updates')
            os.makedirs(updates)
            _make_zip(os.path.join(updates, 'x.zip'), {'好想跑.exe': b'e'})
            api = RA.ReplicaApi('http://x', 'http://x', b'', tmp)
            with mock.patch.object(updater, 'is_compiled', return_value=False):
                result = api.real_apply_update('x.zip')
        self.assertEqual(result['code'], 500)
        self.assertIn('源码运行', result['message'])


    def test_quit_app_schedules_a_later_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = RA.ReplicaApi('http://x', 'http://x', b'', tmp)
            thread = mock.Mock()
            with mock.patch.object(RA.threading, 'Thread',
                                   return_value=thread) as factory:
                result = api.real_quit_app()
        self.assertEqual(result['code'], 200)
        factory.assert_called_once()
        thread.start.assert_called_once()


def _make_zip(path, entries):
    with zipfile.ZipFile(path, 'w') as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


class StageTest(unittest.TestCase):
    def test_stage_extracts_and_finds_the_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            updates = os.path.join(tmp, 'updates')
            os.makedirs(updates)
            package = os.path.join(updates, 'pkg.zip')
            _make_zip(package, {'好想跑.exe': b'exe',
                                '_internal/a.txt': b'a',
                                'lib/x.dll': b'x'})
            result = updater.stage(package, tmp)
            self.assertTrue(os.path.isfile(result['entry']))
            self.assertEqual(result['files'], 3)

    def test_stage_rejects_zip_slip(self):
        with tempfile.TemporaryDirectory() as tmp:
            updates = os.path.join(tmp, 'updates')
            os.makedirs(updates)
            package = os.path.join(updates, 'evil.zip')
            _make_zip(package, {'../evil.txt': b'x', '好想跑.exe': b'e'})
            with self.assertRaises(ValueError):
                updater.stage(package, tmp)

    def test_stage_refuses_to_overwrite_live_data(self):
        for name in ('data/x.json', 'Data/x.json', 'data\\x.json'):
            with tempfile.TemporaryDirectory() as tmp:
                updates = os.path.join(tmp, 'updates')
                os.makedirs(updates)
                package = os.path.join(updates, 'pkg.zip')
                _make_zip(package, {'好想跑.exe': b'e', name: b'{}'})
                with self.assertRaises(ValueError):
                    updater.stage(package, tmp)

    def test_stage_rejects_packages_outside_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = os.path.join(tmp, 'pkg.zip')
            _make_zip(package, {'好想跑.exe': b'e'})
            with self.assertRaises(ValueError):
                updater.stage(package, os.path.join(tmp, 'data'))

    def test_stage_requires_the_exe(self):
        with tempfile.TemporaryDirectory() as tmp:
            updates = os.path.join(tmp, 'updates')
            os.makedirs(updates)
            package = os.path.join(updates, 'pkg.zip')
            _make_zip(package, {'readme.txt': b'x'})
            with self.assertRaises(ValueError):
                updater.stage(package, tmp)


class ApplyTest(unittest.TestCase):
    def test_apply_writes_a_detached_swap_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            updates = os.path.join(tmp, 'updates')
            os.makedirs(updates)
            package = os.path.join(updates, 'pkg.zip')
            _make_zip(package, {'好想跑.exe': b'exe', '_internal/a.txt': b'a'})
            spawn_calls = []
            with mock.patch.object(updater, 'is_compiled', return_value=True), \
                 mock.patch.object(updater, '_spawn',
                                   side_effect=lambda s, c: spawn_calls.append(s)):
                result = updater.apply(package, tmp,
                                       install_dir=os.path.join(tmp, 'install'),
                                       pid=4242)
            script = open(result['script'], encoding='utf-8-sig').read()
        self.assertEqual(len(spawn_calls), 1)
        self.assertIn('$appPid = 4242', script)
        self.assertIn('robocopy', script)
        self.assertIn('Start-Process', script)
        self.assertIn("$dst\\data", script)
        self.assertTrue(script.rstrip().endswith('}') or
                        'Remove-Item $MyInvocation' in script)

    def test_apply_installer_runs_setup_silently(self):
        with tempfile.TemporaryDirectory() as tmp:
            updates = os.path.join(tmp, 'updates')
            os.makedirs(updates)
            package = os.path.join(updates, 'setup.exe')
            with open(package, 'wb') as fh:
                fh.write(b'MZ' + b'\0' * 256)
            spawn_calls = []
            with mock.patch.object(updater, 'is_compiled', return_value=True), \
                 mock.patch.object(updater, '_spawn',
                                   side_effect=lambda s, c: spawn_calls.append(s)):
                result = updater.apply(package, tmp,
                                       install_dir=os.path.join(tmp, 'install'),
                                       pid=4242)
            script = open(result['script'], encoding='utf-8-sig').read()
        self.assertEqual(result['format'], 'installer')
        self.assertEqual(len(spawn_calls), 1)
        self.assertIn('$appPid = 4242', script)
        self.assertIn("'/S'", script)
        self.assertIn('Start-Process', script)

    def test_apply_installer_rejects_a_non_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            updates = os.path.join(tmp, 'updates')
            os.makedirs(updates)
            package = os.path.join(updates, 'setup.exe')
            with open(package, 'wb') as fh:
                fh.write(b'not-a-pe')
            with mock.patch.object(updater, 'is_compiled', return_value=True):
                with self.assertRaises(ValueError):
                    updater.apply(package, tmp,
                                  install_dir=os.path.join(tmp, 'install'))


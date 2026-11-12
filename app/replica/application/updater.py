"""Read the release manifest, fetch the package, and stage an in-place update.

The manifest is published from the GitHub repo ``KnightSportsWorldTool``.
``packaging/build.ps1 -DownloadBaseUrl`` writes absolute asset URLs, so the
same file works both on ``raw.githubusercontent.com`` and next to the zips.

A running onedir build cannot overwrite its own exe/DLLs, so the swap is done
by a detached PowerShell script: it waits for this process to exit, then either

* copies the staged zip over the install directory (portable layout, never
  ``data/``), or
* runs the downloaded NSIS installer with ``/S`` (installed layout, the
  installer itself keeps ``data/``).

The UI only has to call ``apply`` and then quit; the package extension decides
which mode runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeout
from urllib.parse import unquote, urljoin, urlparse

import requests

from ..build_info import VERSION, is_compiled
from . import update_signature

#: Override with a comma-separated list (local tests / a mirror).
MANIFEST_ENV = 'JHP_UPDATE_MANIFEST'
DEFAULT_MANIFESTS = (
    'https://raw.githubusercontent.com/Knight-ask-art/KnightSportsWorldTool/main/latest.json',
    # GitHub raw is not reachable on every network; jsDelivr mirrors the repo.
    'https://cdn.jsdelivr.net/gh/Knight-ask-art/KnightSportsWorldTool@main/latest.json',
)
REQUEST_TIMEOUT = (3.05, 10)
DOWNLOAD_TIMEOUT = (5, 60)
MAX_DOWNLOAD_BYTES = 400 * 1024 * 1024
MAX_NOTES = 12
USER_AGENT = 'haoxiangpao-updater'
ENTRY_NAME = '好想跑.exe'

#: Mirror ranking.  One ranged 64 KB request per mirror, measured on first
#: byte plus first-chunk throughput; the whole probe has a 6 s budget and
#: only runs when the operator actually clicks download.
PROBE_BYTES = 64 * 1024
PROBE_TIMEOUT = (4.0, 6.0)
PROBE_BUDGET = 6.0
PROBE_MAX_MIRRORS = 4
#: A captive portal or a broken proxy answers 200 with an HTML page.  Check
#: the first bytes instead of waiting for a 76 MB sha256 mismatch.
MAGIC_BYTES = {'.zip': b'PK\x03\x04', '.exe': b'MZ'}

#: Registry key the NSIS installer writes (HKCU).  Its presence is what tells
#: the updater "this copy is installed", so the installer asset is used instead
#: of the zip.  A portable copy keeps updating in place with the zip.
INSTALL_REG_KEY = r'Software\好想跑'

#: Asset keys per update format.  ``windows-x64`` must stay the zip: v0.1.2
#: clients only know that key and only understand zip.
ASSET_KEYS = {
    'installer': ('windows-x64-setup', 'windows-setup'),
    'zip': ('windows-x64', 'windows', 'win64'),
}
PACKAGE_SUFFIXES = ('.zip', '.exe')


def manifest_urls() -> tuple[str, ...]:
    raw = (os.environ.get(MANIFEST_ENV) or '').strip()
    if raw:
        return tuple(u.strip() for u in raw.split(',') if u.strip())
    return DEFAULT_MANIFESTS


def parse_version(text) -> tuple[int, ...] | None:
    """``v0.1.2+abc`` -> ``(0, 1, 2)``; anything without a number -> None."""
    match = re.match(r'\s*v?(\d+(?:\.\d+)*)', str(text or ''))
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split('.'))


def _padded(value: tuple[int, ...], width: int) -> tuple[int, ...]:
    return value + (0,) * (width - len(value))


def same_version(left, right) -> bool:
    a, b = parse_version(left), parse_version(right)
    if a is None or b is None:
        return False
    width = max(len(a), len(b))
    return _padded(a, width) == _padded(b, width)


def is_newer(latest, current) -> bool:
    a, b = parse_version(latest), parse_version(current)
    if a is None or b is None:
        return False
    width = max(len(a), len(b))
    return _padded(a, width) > _padded(b, width)


def _clean_notes(value) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text:
            continue
        out.append(text[:300])
        if len(out) >= MAX_NOTES:
            break
    return out


def _pick_asset(manifest: dict, prefer: str = 'zip'):
    assets = manifest.get('assets')
    if not isinstance(assets, dict):
        return None
    if prefer == 'installer':
        keys = list(ASSET_KEYS['installer']) + list(ASSET_KEYS['zip'])
    else:
        keys = list(ASSET_KEYS['zip'])
    for key in keys:
        item = assets.get(key)
        if isinstance(item, dict):
            return item
    return None


def _asset_format(asset: dict, url: str) -> str:
    fmt = str(asset.get('format') or '').strip().lower()
    if fmt in ('zip', 'installer'):
        return fmt
    return 'installer' if urlparse(url).path.lower().endswith('.exe') else 'zip'


def _is_safe_url(parsed) -> bool:
    """https only; loopback http is allowed for local tests/mirrors."""
    if not parsed.netloc:
        return False
    if parsed.scheme == 'https':
        return True
    return (parsed.scheme == 'http'
            and (parsed.hostname or '').lower() in
            ('localhost', '127.0.0.1', '::1'))


def installed_dir() -> str:
    """Install directory recorded by the NSIS installer ('' when absent).

    Named ``installed_dir`` rather than ``install_dir`` because ``apply``
    takes an ``install_dir`` parameter; shadowing the two would be a trap.
    """
    if os.name != 'nt':
        return ''
    try:
        import winreg
    except ImportError:  # pragma: no cover - non-Windows
        return ''
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INSTALL_REG_KEY) as key:
            value, _ = winreg.QueryValueEx(key, 'InstallDir')
    except OSError:
        return ''
    return str(value or '').strip()


def is_installed(install: str | None = None) -> bool:
    """True when this process runs from the NSIS install directory.

    A portable copy must keep using the zip: installing over it would leave
    the old folder behind and start a second copy with an empty ``data/``.
    """
    if not is_compiled():
        return False
    known = installed_dir()
    if not known:
        return False
    from ..build_info import app_root
    root = os.path.realpath(install or app_root())
    known = os.path.realpath(known)
    try:
        return os.path.commonpath([known, root]) == known
    except ValueError:  # different drives
        return False


def _int_or_zero(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _update_public_keys() -> dict:
    """Signing keys injected by ``packaging/build.ps1``.

    The generated module only exists in a release build; a source checkout
    never carries it, and the updater only runs in an installed app.
    """
    if not is_compiled():
        return {}
    try:
        from .._update_pubkey import PUBLIC_KEYS
    except Exception:  # noqa: BLE001 - missing injected file is not a crash
        return {}
    return dict(PUBLIC_KEYS) if isinstance(PUBLIC_KEYS, dict) else {}


def _verify_manifest(manifest: dict) -> None:
    """Refuse an unsigned or tampered ``latest.json`` in a release build."""
    keys = _update_public_keys()
    if not keys:
        if is_compiled():
            raise ValueError('更新清单未配置验签公钥，已拒绝自动更新')
        return
    try:
        update_signature.verify_manifest_raw(manifest, keys)
    except update_signature.ManifestSignatureError as exc:
        raise ValueError('更新清单签名校验失败: %s' % exc) from exc


def normalize(manifest, source: str, current: str = VERSION,
              checked_at: int | None = None, prefer: str = 'zip') -> dict:
    """Validate one manifest.  Raises ``ValueError`` when it is unusable.

    ``prefer='installer'`` picks ``windows-x64-setup`` and falls back to the
    zip when the manifest has no installer.  Both zip and installer must carry
    a sha256: the updater is about to execute/unpack them.
    """
    if not isinstance(manifest, dict):
        raise ValueError('清单不是 JSON 对象')
    if str(manifest.get('schema', 1)) != '1':
        raise ValueError('清单版本不支持')
    app = manifest.get('app')
    if app not in (None, '', 'haoxiangpao'):
        raise ValueError('清单不属于这个应用')

    _verify_manifest(manifest)

    latest = str(manifest.get('version') or '').strip()
    if parse_version(latest) is None:
        raise ValueError('清单缺少有效版本号')

    asset = _pick_asset(manifest, prefer)
    if asset is None:
        raise ValueError('清单缺少 Windows 下载项')
    raw_url = str(asset.get('url') or '').strip()
    if not raw_url:
        raise ValueError('下载项缺少地址')
    url = urljoin(source, raw_url)
    parsed = urlparse(url)
    if not _is_safe_url(parsed):
        raise ValueError('下载地址必须是 https（localhost 除外）')

    # Every mirror the manifest carries, primary first.  Unknown or malformed
    # entries are dropped rather than failing the whole update: an old client
    # that only understands `url` must still work when a mirror is bad.
    urls = [url]
    for extra in asset.get('urls') or []:
        if not isinstance(extra, str) or not extra.strip():
            continue
        candidate = urljoin(source, extra.strip())
        parts = urlparse(candidate)
        if _is_safe_url(parts) and candidate not in urls:
            urls.append(candidate)

    file_name = os.path.basename(str(asset.get('file') or '').replace('\\', '/'))
    if not file_name:
        file_name = os.path.basename(unquote(parsed.path)) or 'update.zip'

    sha = str(asset.get('sha256') or '').strip().lower()
    if sha and not re.fullmatch(r'[0-9a-f]{64}', sha):
        sha = ''

    fmt = _asset_format(asset, url)
    if not sha:
        # Both zip and installer are executed automatically (zip is unpacked
        # over the app directory).  A manifest without a hash is refused.
        raise ValueError('下载项缺少 sha256，拒绝自动更新')
    layout = str(asset.get('layout') or
                 ('installer' if fmt == 'installer' else 'onedir'))

    if is_newer(latest, current):
        state, message = 'available', '发现新版本 v%s' % latest
    elif same_version(latest, current):
        state, message = 'current', '已是最新版本 v%s' % current
    else:
        # A source checkout or a hand-built zip can be ahead of the release.
        state, message = 'ahead', '当前版本 v%s 比清单更新' % current

    minimum = str(manifest.get('min_version') or '').strip()
    required = bool(minimum and is_newer(minimum, current))

    return {
        'state': state,
        'current': current,
        'latest': latest,
        'min_version': minimum,
        'required': required,
        'file': file_name,
        'url': url,
        'urls': urls,
        'size': _int_or_zero(asset.get('size')),
        'sha256': sha,
        'format': fmt,
        'layout': layout,
        'notes': _clean_notes(manifest.get('notes')),
        'checked_at': int(checked_at if checked_at is not None
                          else time.time() * 1000),
        'source': source,
        'message': message,
    }


def fetch_manifest(url: str) -> dict:
    response = requests.get(url, timeout=REQUEST_TIMEOUT,
                            headers={'User-Agent': USER_AGENT},
                            allow_redirects=True)
    if response.status_code != 200:
        raise RuntimeError('HTTP %s' % response.status_code)
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError('清单不是 JSON 对象')
    return data


def check(current: str = VERSION, manifest_url: str | None = None,
          prefer: str | None = None) -> dict:
    """Try every manifest URL; return one honest state, never raise.

    Without an explicit ``prefer``, an installed copy asks for the NSIS
    installer and a portable copy asks for the zip.  Both stay on their own
    track, so nobody has to move their ``data/`` directory.
    """
    if prefer is None:
        prefer = 'installer' if is_installed() else 'zip'
    urls = (manifest_url,) if manifest_url else manifest_urls()
    errors: list[str] = []
    checked_at = int(time.time() * 1000)
    for url in urls:
        try:
            data = fetch_manifest(url)
        except Exception as e:  # noqa: BLE001 - network/CDN failure
            errors.append('%s: %s' % (url, e))
            continue
        try:
            return normalize(data, source=url, current=current,
                             checked_at=checked_at, prefer=prefer)
        except ValueError as e:
            errors.append('%s: %s' % (url, e))
    detail = errors[-1] if errors else '没有配置清单地址'
    return {
        'state': 'failed',
        'current': current,
        'latest': '',
        'min_version': '',
        'required': False,
        'file': '',
        'url': '',
        'size': 0,
        'sha256': '',
        'format': '',
        'layout': '',
        'notes': [],
        'checked_at': checked_at,
        'source': '',
        'message': '检查更新失败：%s' % detail,
    }


def _safe_name(name: str, url: str) -> str:
    name = os.path.basename(str(name or '').replace('\\', '/')).strip()
    if not name:
        name = os.path.basename(unquote(urlparse(url).path)) or 'update.zip'
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .')[:120]
    return name or 'update.zip'


def _magic_for(name: str) -> bytes:
    return MAGIC_BYTES.get(os.path.splitext(str(name or ''))[1].lower(), b'')


def _probe_mirror(url: str, magic: bytes = b'') -> tuple:
    """Measure one mirror: ``(ttfb_seconds, bytes_per_second)``.

    A ranged GET, not HEAD: several CDNs answer HEAD from cache in a few
    milliseconds and are still slow on the body.  The stream is closed after
    the first 64 KB, so a server that ignores ``Range`` cannot make us pull
    the whole package twice.
    """
    started = time.monotonic()
    with requests.get(
            url, stream=True, timeout=PROBE_TIMEOUT,
            headers={'User-Agent': USER_AGENT,
                     'Range': 'bytes=0-%d' % (PROBE_BYTES - 1)},
            allow_redirects=True) as response:
        if response.status_code not in (200, 206):
            raise RuntimeError('HTTP %s' % response.status_code)
        first = next(response.iter_content(4096), b'')
        if not first:
            raise RuntimeError('空响应')
        ttfb = time.monotonic() - started
        if magic and not first.startswith(magic):
            raise RuntimeError('返回的不是安装包')
        got = len(first)
        for chunk in response.iter_content(1 << 16):
            got += len(chunk)
            if got >= PROBE_BYTES:
                break
        elapsed = max(time.monotonic() - started, 1e-6)
    return ttfb, got / elapsed


def rank_mirrors(urls, magic: bytes = b'', budget: float = PROBE_BUDGET):
    """Order mirrors by measured cost: ``ttfb + 64 KB / speed``.

    Latency alone is the wrong metric -- a 20 ms mirror throttled to 50 KB/s
    is worse than a 300 ms mirror that streams at 5 MB/s -- so the score is an
    estimate of how long this package's first block takes.

    Returns ``(ranked, scores)``.  Mirrors that fail or miss the budget keep
    the manifest order behind the ranked ones, so a broken probe can only cost
    2.5 s, never the update.
    """
    urls = [u for u in urls if u]
    if len(urls) < 2:
        return list(urls), {}
    results = {}
    pool = ThreadPoolExecutor(max_workers=min(PROBE_MAX_MIRRORS, len(urls)))
    try:
        futures = {pool.submit(_probe_mirror, u, magic): u for u in urls}
        try:
            for future in as_completed(futures, timeout=budget):
                url = futures[future]
                try:
                    ttfb, speed = future.result()
                except Exception:  # noqa: BLE001 - a dead mirror is expected
                    continue
                results[url] = ttfb + PROBE_BYTES / max(speed, 1.0)
        except FutureTimeout:
            pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    ranked = sorted(results, key=lambda u: results[u])
    ranked += [u for u in urls if u not in results]
    return ranked, results


class DownloadCancelled(RuntimeError):
    """The operator closed the progress dialog."""


def _download_one(url: str, dest_dir: str, name: str, sha256: str,
                  expected_size: int, progress=None, cancel=None) -> dict:
    """One mirror, all the way to a verified file.

    A ``.part`` from the same URL/size is resumed with a Range request; every
    final file still goes through the manifest's sha256, so a corrupt partial
    can only cost one retry, never a bad install.
    """
    target = os.path.join(os.path.realpath(dest_dir), name)
    part = target + '.part'
    meta_path = part + '.meta'
    resume = 0
    if os.path.exists(part) and os.path.exists(meta_path):
        try:
            with open(meta_path, encoding='utf-8') as fh:
                meta = json.load(fh)
            if (str(meta.get('url') or '') == url and
                    (not expected_size or int(meta.get('size') or 0) == expected_size)):
                resume = os.path.getsize(part)
        except (OSError, ValueError, TypeError):
            resume = 0
    if expected_size and resume > expected_size:
        resume = 0

    headers = {'User-Agent': USER_AGENT}
    if resume:
        headers['Range'] = 'bytes=%d-' % resume
    digest = hashlib.sha256()
    total = 0
    if resume:
        try:
            with open(part, 'rb') as fh:
                while True:
                    block = fh.read(1 << 20)
                    if not block:
                        break
                    digest.update(block)
                    total += len(block)
        except OSError:
            resume, total = 0, 0
            digest = hashlib.sha256()
    try:
        with open(meta_path, 'w', encoding='utf-8') as fh:
            json.dump({'url': url, 'size': expected_size}, fh)
        with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT,
                          headers=headers) as response:
            if response.status_code == 416 and expected_size and total == expected_size:
                pass  # already complete; verify below
            elif response.status_code not in (200, 206):
                raise RuntimeError('HTTP %s' % response.status_code)
            elif resume and response.status_code == 200:
                # Server ignored Range: restart rather than append twice.
                resume, total = 0, 0
                digest = hashlib.sha256()
            if not (response.status_code == 416 and total == expected_size):
                mode = 'ab' if resume else 'wb'
                last_report = 0.0
                with open(part, mode) as fh:
                    for chunk in response.iter_content(1 << 20):
                        if cancel is not None and cancel.is_set():
                            raise DownloadCancelled('下载已取消')
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise ValueError('安装包超过大小上限')
                        digest.update(chunk)
                        fh.write(chunk)
                        now = time.monotonic()
                        if progress is not None and now - last_report > 0.35:
                            last_report = now
                            progress(total, expected_size)
        if expected_size and total != expected_size:
            raise ValueError('大小不一致（%d != %d）' % (total, expected_size))
        actual = digest.hexdigest()
        if sha256 and actual != sha256.lower():
            # A partial from another mirror can be structurally complete but
            # wrong; keep it for inspection? No: retry from zero next time.
            try:
                os.remove(part)
                os.remove(meta_path)
            except OSError:
                pass
            raise ValueError('sha256 校验失败')
        os.replace(part, target)
        try:
            os.remove(meta_path)
        except OSError:
            pass
    except Exception:
        # Keep `.part` for a resume; a human can delete data/updates manually.
        raise
    return {'file': name, 'path': target, 'size': total, 'sha256': actual}


def download(url, dest_dir: str, sha256: str = '', size: int = 0,
             file: str = '', progress=None, cancel=None) -> dict:
    """Stream a package into ``dest_dir``; verify size and sha256 first.

    ``progress(received, total)`` is called while downloading; ``cancel`` is a
    ``threading.Event`` that turns a closed dialog into a clean stop.
    """
    candidates = [url] if isinstance(url, str) else [
        str(u) for u in (url or []) if u]
    if not candidates:
        raise ValueError('下载地址不合法')
    for candidate in candidates:
        parsed = urlparse(candidate)
        if not _is_safe_url(parsed):
            raise ValueError('下载地址不合法（必须是 https）')

    expected_size = _int_or_zero(size)
    if expected_size > MAX_DOWNLOAD_BYTES:
        raise ValueError('安装包超过大小上限')

    os.makedirs(dest_dir, exist_ok=True)
    name = _safe_name(file, candidates[0])
    ranked, scores = rank_mirrors(candidates, _magic_for(name))
    tried = []
    last = None
    for candidate in ranked:
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled('下载已取消')
        try:
            info = _download_one(candidate, dest_dir, name, sha256,
                                 expected_size, progress=progress,
                                 cancel=cancel)
        except DownloadCancelled:
            raise
        except Exception as e:  # noqa: BLE001 - try the next mirror
            tried.append('%s: %s' % (urlparse(candidate).netloc, e))
            last = e
            continue
        info['mirror'] = candidate
        info['probe'] = round(float(scores.get(candidate, 0.0)), 3)
        info['tried'] = tried
        return info
    raise last if last is not None else RuntimeError('没有可用的下载地址')


# --------------------------------------------------------------------------
# background download job (UI progress + cancel)
# --------------------------------------------------------------------------
_DOWNLOAD_LOCK = threading.Lock()
_DOWNLOAD_THREAD = None
_DOWNLOAD_CANCEL = threading.Event()
_DOWNLOAD_STATE = {'state': 'idle'}


def _download_state_path(store_dir: str) -> str:
    return os.path.join(_updates_dir(store_dir), 'download.state.json')


def _publish_download_state(store_dir: str, **patch) -> dict:
    global _DOWNLOAD_STATE
    with _DOWNLOAD_LOCK:
        state = dict(_DOWNLOAD_STATE)
        state.update(patch)
        state['updated_at'] = int(time.time() * 1000)
        _DOWNLOAD_STATE = state
        snapshot = dict(state)
    try:
        os.makedirs(_updates_dir(store_dir), exist_ok=True)
        tmp = _download_state_path(store_dir) + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(snapshot, fh, ensure_ascii=False)
        os.replace(tmp, _download_state_path(store_dir))
    except OSError:
        pass
    return snapshot


def download_status(store_dir: str) -> dict:
    with _DOWNLOAD_LOCK:
        state = dict(_DOWNLOAD_STATE)
    if state.get('state') in (None, 'idle'):
        try:
            with open(_download_state_path(store_dir), encoding='utf-8') as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            state = {'state': 'idle'}
    return state


def start_download(url, dest_dir: str, sha256: str = '', size: int = 0,
                   file: str = '', store_dir: str = '') -> dict:
    """Start a download in a daemon thread and return the initial state."""
    global _DOWNLOAD_THREAD, _DOWNLOAD_STATE
    store = store_dir or dest_dir
    with _DOWNLOAD_LOCK:
        if _DOWNLOAD_THREAD is not None and _DOWNLOAD_THREAD.is_alive():
            return dict(_DOWNLOAD_STATE)
        _DOWNLOAD_CANCEL.clear()
        _DOWNLOAD_STATE = {'state': 'probing', 'received': 0,
                           'total': _int_or_zero(size), 'message': '正在测速…'}

    def job():
        started = time.monotonic()
        last = [0.0]

        def progress(received, total):
            now = time.monotonic()
            if now - last[0] < 0.25:
                return
            last[0] = now
            elapsed = max(now - started, 0.2)
            _publish_download_state(
                store, state='downloading', received=int(received),
                total=int(total or 0), speed=round(received / elapsed, 1),
                message='下载中…')

        try:
            _publish_download_state(store, state='probing',
                                    message='正在测速并选择线路…')
            info = download(url, dest_dir, sha256=sha256, size=size,
                            file=file, progress=progress,
                            cancel=_DOWNLOAD_CANCEL)
            _publish_download_state(
                store, state='done', received=int(info.get('size') or 0),
                total=int(info.get('size') or size or 0),
                file=info.get('file') or '', mirror=info.get('mirror') or '',
                speed=0, message='下载完成，sha256 已校验')
        except DownloadCancelled:
            _publish_download_state(store, state='cancelled',
                                    message='已取消；下次可继续下载')
        except Exception as e:  # noqa: BLE001
            _publish_download_state(store, state='failed',
                                    error=str(e), message='下载失败：%s' % e)
        finally:
            _DOWNLOAD_CANCEL.clear()

    _DOWNLOAD_THREAD = threading.Thread(target=job, daemon=True,
                                        name='hxp-update-download')
    _DOWNLOAD_THREAD.start()
    return download_status(store)


def cancel_download(store_dir: str) -> dict:
    _DOWNLOAD_CANCEL.set()
    return _publish_download_state(store_dir, state='cancelling',
                                   message='正在取消…')


def last_result(store_dir: str) -> dict:
    path = os.path.join(_updates_dir(store_dir), 'last-result.json')
    try:
        # PowerShell 5.1 writes Out-File -Encoding utf8 *with* a BOM; the
        # update script uses it, so a plain utf-8 json.load would silently
        # drop every "update finished / failed" notice.  utf-8-sig accepts
        # both the BOM and a normal file.
        with open(path, encoding='utf-8-sig') as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def clear_last_result(store_dir: str) -> None:
    try:
        os.remove(os.path.join(_updates_dir(store_dir), 'last-result.json'))
    except OSError:
        pass


def _updates_dir(store_dir: str) -> str:
    return os.path.realpath(os.path.join(store_dir, 'updates'))


def locate_package(store_dir: str, name: str = '') -> str:
    """Return a package inside ``data/updates`` (named, or the newest one)."""
    updates = _updates_dir(store_dir)
    if not os.path.isdir(updates):
        raise ValueError('还没有下载更新包')
    if name:
        base = os.path.basename(str(name).replace('\\', '/')).strip()
        path = os.path.realpath(os.path.join(updates, base))
        if os.path.commonpath([updates, path]) != updates:
            raise ValueError('文件名不合法')
        if not os.path.isfile(path):
            raise ValueError('更新包不存在: %s' % base)
        return path
    candidates = [os.path.join(updates, f) for f in os.listdir(updates)
                  if f.lower().endswith(PACKAGE_SUFFIXES)]
    if not candidates:
        raise ValueError('还没有下载更新包')
    return max(candidates, key=os.path.getmtime)


def stage(zip_path: str, store_dir: str) -> dict:
    """Extract a downloaded package into ``data/updates/staging``.

    Only zips that live in ``data/updates`` are accepted, and every entry is
    checked to stay inside the staging directory (zip-slip).
    """
    updates = _updates_dir(store_dir)
    real = os.path.realpath(zip_path)
    if os.path.commonpath([updates, real]) != updates:
        raise ValueError('更新包不在 data/updates 目录')
    if not os.path.isfile(real):
        raise ValueError('更新包不存在')

    staging = os.path.join(updates, 'staging')
    if os.path.isdir(staging):
        shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)

    try:
        with zipfile.ZipFile(real) as zf:
            infos = zf.infolist()
            if len(infos) > 20000:
                raise ValueError('更新包文件数异常')
            if sum(max(0, i.file_size) for i in infos) > 2 * 1024 * 1024 * 1024:
                raise ValueError('更新包解压后超过 2 GiB')
            base = os.path.realpath(staging)
            for info in infos:
                name = info.filename
                head = re.split(r'[\\/]', name.lstrip('/\\'), 1)[0].lower()
                if head == 'data':
                    # Never let a package overwrite the live data directory.
                    raise ValueError('更新包包含 data 目录')
                dest = os.path.realpath(os.path.join(base, name))
                if dest != base and os.path.commonpath([base, dest]) != base:
                    raise ValueError('更新包包含非法路径')
            zf.extractall(staging)
    except zipfile.BadZipFile as e:
        raise ValueError('更新包不是有效 zip') from e

    entry = os.path.join(staging, ENTRY_NAME)
    if not os.path.isfile(entry):
        raise ValueError('更新包里没有 %s' % ENTRY_NAME)
    files = sum(len(names) for _, _, names in os.walk(staging))
    return {'path': staging, 'entry': entry, 'files': files}


_ZIP_SCRIPT_TEMPLATE = r"""$ErrorActionPreference = 'Stop'
$appPid = %(pid)d
$src = %(src)s
$dst = %(dst)s
$exe = %(exe)s
$log = %(log)s
$version = %(version)s
function Write-Log($m) {
  try {
    ('[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) |
      Out-File -FilePath $log -Append -Encoding utf8
  } catch {}
}
function Write-Result($state, $message) {
  try {
    $dir = Join-Path $dst 'data\updates'
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $obj = [ordered]@{ state=$state; version=$version; message=$message;
      at=(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') }
    ($obj | ConvertTo-Json -Compress) | Out-File -FilePath (Join-Path $dir 'last-result.json') -Encoding utf8
  } catch {}
}
Write-Log "zip update: wait for pid $appPid"
$gone = $false
for ($i = 0; $i -lt 60; $i++) {
  if (-not (Get-Process -Id $appPid -ErrorAction SilentlyContinue)) { $gone = $true; break }
  Start-Sleep -Milliseconds 500
}
if (-not $gone) { Write-Result 'failed' '旧进程没有退出'; Write-Log 'still running; abort'; exit 2 }
Start-Sleep -Milliseconds 500
$new = "$dst.__new"
$old = "$dst.__old"
try {
  Remove-Item $new -Recurse -Force -ErrorAction SilentlyContinue
  Remove-Item $old -Recurse -Force -ErrorAction SilentlyContinue
  robocopy $src $new /E /IS /IT /R:1 /W:1 /XD "$dst\data" /NFL /NDL /NJH /NJS /NP | Out-Null
  if ($LASTEXITCODE -ge 8) { throw "robocopy exit $LASTEXITCODE" }
  if (-not (Test-Path (Join-Path $new '好想跑.exe'))) { throw '新目录里没有 好想跑.exe' }
  if (Test-Path (Join-Path $dst 'data')) {
    # A release zip must not carry a data folder, but if it ever does, the
    # operator's data wins: remove the placeholder before the move, otherwise
    # Move-Item nests data\data and the app starts with an empty profile.
    $newData = Join-Path $new 'data'
    if (Test-Path $newData) { Remove-Item $newData -Recurse -Force -ErrorAction SilentlyContinue }
    Move-Item (Join-Path $dst 'data') $newData
  }
  Move-Item $dst $old
  Move-Item $new $dst
  # Record the result before the new process reads it at boot; a start
  # failure overwrites this with 'failed' in the catch below.
  Write-Result 'ok' '更新完成'
  Start-Process -FilePath $exe -WorkingDirectory $dst
  Remove-Item $old -Recurse -Force -ErrorAction SilentlyContinue
  # The package and staging copy are no longer needed (last-result.json stays).
  Remove-Item (Join-Path $dst 'data\updates\staging') -Recurse -Force -ErrorAction SilentlyContinue
  Get-ChildItem (Join-Path $dst 'data\updates') -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Extension -in '.zip', '.exe' } |
    Remove-Item -Force -ErrorAction SilentlyContinue
  Write-Log 'ok'
} catch {
  $msg = $_.Exception.Message
  Write-Log ('failed: ' + $msg)
  if (-not (Test-Path $dst) -and (Test-Path $old)) {
    try { Move-Item $old $dst } catch {}
  }
  Write-Result 'failed' $msg
  exit 1
}
Start-Sleep -Milliseconds 300
Remove-Item $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
"""


#: Installer mode: the NSIS package already knows how to install, keep data\
#: and register itself.  The script only waits for this process to exit, runs
#: it silently, then starts the new exe.
_INSTALLER_SCRIPT_TEMPLATE = r"""$ErrorActionPreference = 'Stop'
$appPid = %(pid)d
$setup = %(setup)s
$exe = %(exe)s
$install = %(install)s
$log = %(log)s
$version = %(version)s
function Write-Log($m) {
  try {
    ('[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) |
      Out-File -FilePath $log -Append -Encoding utf8
  } catch {}
}
function Write-Result($state, $message) {
  try {
    $dir = Join-Path $install 'data\updates'
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $obj = [ordered]@{ state=$state; version=$version; message=$message;
      at=(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') }
    ($obj | ConvertTo-Json -Compress) | Out-File -FilePath (Join-Path $dir 'last-result.json') -Encoding utf8
  } catch {}
}
Write-Log "installer: wait for pid $appPid"
$gone = $false
for ($i = 0; $i -lt 120; $i++) {
  if (-not (Get-Process -Id $appPid -ErrorAction SilentlyContinue)) { $gone = $true; break }
  Start-Sleep -Milliseconds 500
}
if (-not $gone) { Write-Result 'failed' '旧进程没有退出'; Write-Log 'still running; abort'; exit 2 }
Start-Sleep -Milliseconds 800
try {
  $proc = Start-Process -FilePath $setup -ArgumentList '/S' -PassThru -Wait
  Write-Log ("setup exit " + $proc.ExitCode)
  if ($proc.ExitCode -ne 0) { throw ("安装包退出码 " + $proc.ExitCode) }
  for ($i = 0; $i -lt 80; $i++) {
    if (Test-Path $exe) { break }
    Start-Sleep -Milliseconds 250
  }
  if (-not (Test-Path $exe)) { throw '安装后没有找到 好想跑.exe' }
  Write-Result 'ok' '更新完成'
  Start-Process -FilePath $exe -WorkingDirectory (Split-Path $exe -Parent)
  Remove-Item $setup -Force -ErrorAction SilentlyContinue
  Write-Log 'ok'
} catch {
  Write-Log ('failed: ' + $_.Exception.Message)
  Write-Result 'failed' $_.Exception.Message
  exit 1
}
Start-Sleep -Milliseconds 300
Remove-Item $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
"""


def _quote(path: str) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _spawn(script: str, cwd: str) -> None:
    flags = 0
    if os.name == 'nt':
        # DETACHED_PROCESS | CREATE_NO_WINDOW: the script must outlive us and
        # must not flash a console window over the app.
        flags = 0x00000008 | 0x08000000
    subprocess.Popen(
        ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
         '-File', script],
        cwd=cwd, close_fds=True, creationflags=flags,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)


def apply(package: str, store_dir: str, install_dir: str | None = None,
          pid: int | None = None, version: str = '') -> dict:
    """Hand the swap to a detached PowerShell script.

    The script is written to the system temp directory, not ``data/updates``:
    a portable swap moves the whole install directory (including ``data``)
    and a running .ps1 inside it would lock the move on Windows.
    """
    if os.name != 'nt':
        raise RuntimeError('自动更新只支持 Windows')
    if not is_compiled():
        raise RuntimeError('源码运行时不执行自动替换')

    from ..build_info import app_root
    install = os.path.realpath(install_dir or app_root())
    if str(package).lower().endswith('.exe'):
        return _apply_installer(package, store_dir, install, pid, version)

    staged = stage(package, store_dir)
    updates = _updates_dir(store_dir)
    script = os.path.join(tempfile.gettempdir(),
                          'hxp-update-%s.ps1' % time.time_ns())
    log = os.path.join(updates, 'apply-update.log')
    app_pid = int(pid if pid is not None else os.getpid())
    text = _ZIP_SCRIPT_TEMPLATE % {
        'pid': app_pid,
        'src': _quote(staged['path']),
        'dst': _quote(install),
        'exe': _quote(os.path.join(install, ENTRY_NAME)),
        'log': _quote(log),
        'version': _quote(version),
    }
    with open(script, 'w', encoding='utf-8-sig', newline='\r\n') as fh:
        fh.write(text)
    _spawn(script, tempfile.gettempdir())
    return {'format': 'zip', 'staging': staged['path'],
            'files': staged['files'], 'install': install,
            'script': script, 'log': log, 'pid': app_pid,
            'version': version}


def _apply_installer(package: str, store_dir: str, install: str,
                     pid: int | None = None, version: str = '') -> dict:
    """Run the NSIS installer silently after this process exits."""
    updates = _updates_dir(store_dir)
    real = os.path.realpath(package)
    if os.path.commonpath([updates, real]) != updates:
        raise ValueError('更新包不在 data/updates 目录')
    if not os.path.isfile(real):
        raise ValueError('更新包不存在')
    with open(real, 'rb') as fh:
        if fh.read(2) != b'MZ':
            raise ValueError('安装包不是有效的 Windows 程序')

    script = os.path.join(tempfile.gettempdir(),
                          'hxp-update-%s.ps1' % time.time_ns())
    log = os.path.join(updates, 'apply-update.log')
    app_pid = int(pid if pid is not None else os.getpid())
    text = _INSTALLER_SCRIPT_TEMPLATE % {
        'pid': app_pid,
        'setup': _quote(real),
        'exe': _quote(os.path.join(install, ENTRY_NAME)),
        'install': _quote(install),
        'log': _quote(log),
        'version': _quote(version),
    }
    with open(script, 'w', encoding='utf-8-sig', newline='\r\n') as fh:
        fh.write(text)
    _spawn(script, tempfile.gettempdir())
    return {'format': 'installer', 'package': real, 'install': install,
            'script': script, 'log': log, 'pid': app_pid,
            'version': version}


def self_test() -> list[tuple[str, bool, str]]:
    out = [
        ('parse v-prefix', parse_version('v0.1.2+abc') == (0, 1, 2), ''),
        ('newer patch', is_newer('0.1.3', '0.1.2'), ''),
        ('same is not newer', not is_newer('0.1.2', '0.1.2'), ''),
        ('relative asset resolved',
         normalize({'schema': 1, 'version': '9.9.9',
                    'assets': {'windows-x64': {'url': 'a.zip'}}},
                   'https://example.com/latest.json')['url']
         == 'https://example.com/a.zip', ''),
        ('installer asset chosen',
         normalize({'schema': 1, 'version': '9.9.9',
                    'assets': {'windows-x64-setup': {
                        'url': 'a.exe', 'sha256': 'a' * 64}}},
                   'https://example.com/latest.json',
                   prefer='installer')['format'] == 'installer', ''),
        ('zip still chosen for portable',
         normalize({'schema': 1, 'version': '9.9.9',
                    'assets': {'windows-x64': {'url': 'a.zip'}}},
                   'https://example.com/latest.json',
                   prefer='installer')['format'] == 'zip', ''),
    ]
    return out

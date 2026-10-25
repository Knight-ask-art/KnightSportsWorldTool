"""Read the release manifest, fetch the package, and stage an in-place update.

The manifest is published from the GitHub repo ``KnightSportsWorldTool``.
``packaging/build.ps1 -DownloadBaseUrl`` writes absolute asset URLs, so the
same file works both on ``raw.githubusercontent.com`` and next to the zips.

A running onedir build cannot overwrite its own exe/DLLs, so the swap is done
by a detached PowerShell script: it waits for this process to exit, copies the
staged files over the install directory (never ``data/``), and starts the new
exe.  The UI only has to call ``apply`` and then quit.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import time
import zipfile
from urllib.parse import unquote, urljoin, urlparse

import requests

from ..build_info import VERSION, is_compiled

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


def _pick_asset(manifest: dict):
    assets = manifest.get('assets')
    if not isinstance(assets, dict):
        return None
    for key in ('windows-x64', 'windows', 'win64'):
        item = assets.get(key)
        if isinstance(item, dict):
            return item
    return None


def _int_or_zero(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def normalize(manifest, source: str, current: str = VERSION,
              checked_at: int | None = None) -> dict:
    """Validate one manifest.  Raises ``ValueError`` when it is unusable."""
    if not isinstance(manifest, dict):
        raise ValueError('清单不是 JSON 对象')
    if str(manifest.get('schema', 1)) != '1':
        raise ValueError('清单版本不支持')
    app = manifest.get('app')
    if app not in (None, '', 'haoxiangpao'):
        raise ValueError('清单不属于这个应用')

    latest = str(manifest.get('version') or '').strip()
    if parse_version(latest) is None:
        raise ValueError('清单缺少有效版本号')

    asset = _pick_asset(manifest)
    if asset is None:
        raise ValueError('清单缺少 Windows 下载项')
    raw_url = str(asset.get('url') or '').strip()
    if not raw_url:
        raise ValueError('下载项缺少地址')
    url = urljoin(source, raw_url)
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise ValueError('下载地址不是 http(s)')

    file_name = os.path.basename(str(asset.get('file') or '').replace('\\', '/'))
    if not file_name:
        file_name = os.path.basename(unquote(parsed.path)) or 'update.zip'

    sha = str(asset.get('sha256') or '').strip().lower()
    if sha and not re.fullmatch(r'[0-9a-f]{64}', sha):
        sha = ''

    if is_newer(latest, current):
        state, message = 'available', '发现新版本 v%s' % latest
    elif same_version(latest, current):
        state, message = 'current', '已是最新版本 v%s' % current
    else:
        # A source checkout or a hand-built zip can be ahead of the release.
        state, message = 'ahead', '当前版本 v%s 比清单更新' % current

    return {
        'state': state,
        'current': current,
        'latest': latest,
        'file': file_name,
        'url': url,
        'size': _int_or_zero(asset.get('size')),
        'sha256': sha,
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


def check(current: str = VERSION, manifest_url: str | None = None) -> dict:
    """Try every manifest URL; return one honest state, never raise."""
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
                             checked_at=checked_at)
        except ValueError as e:
            errors.append('%s: %s' % (url, e))
    detail = errors[-1] if errors else '没有配置清单地址'
    return {
        'state': 'failed',
        'current': current,
        'latest': '',
        'file': '',
        'url': '',
        'size': 0,
        'sha256': '',
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


def download(url: str, dest_dir: str, sha256: str = '', size: int = 0,
             file: str = '') -> dict:
    """Stream a package into ``dest_dir``; verify size and sha256 first."""
    parsed = urlparse(str(url or ''))
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise ValueError('下载地址不合法')

    expected_size = _int_or_zero(size)
    if expected_size > MAX_DOWNLOAD_BYTES:
        raise ValueError('安装包超过大小上限')

    os.makedirs(dest_dir, exist_ok=True)
    name = _safe_name(file, url)
    target = os.path.join(os.path.realpath(dest_dir), name)
    part = target + '.part'
    digest = hashlib.sha256()
    total = 0
    try:
        with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT,
                          headers={'User-Agent': USER_AGENT}) as response:
            if response.status_code != 200:
                raise RuntimeError('HTTP %s' % response.status_code)
            with open(part, 'wb') as fh:
                for chunk in response.iter_content(1 << 20):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise ValueError('安装包超过大小上限')
                    digest.update(chunk)
                    fh.write(chunk)
        if expected_size and total != expected_size:
            raise ValueError('大小不一致（%d != %d）' % (total, expected_size))
        actual = digest.hexdigest()
        if sha256 and actual != sha256.lower():
            raise ValueError('sha256 校验失败')
        os.replace(part, target)
    except Exception:
        try:
            os.remove(part)
        except OSError:
            pass
        raise
    return {'file': name, 'path': target, 'size': total, 'sha256': actual}


def _updates_dir(store_dir: str) -> str:
    return os.path.realpath(os.path.join(store_dir, 'updates'))


def locate_package(store_dir: str, name: str = '') -> str:
    """Return a zip inside ``data/updates`` (named, or the newest one)."""
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
                  if f.lower().endswith('.zip')]
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


_SCRIPT_TEMPLATE = r"""$ErrorActionPreference = 'Stop'
$appPid = %(pid)d
$src = %(src)s
$dst = %(dst)s
$exe = %(exe)s
$log = %(log)s
function Write-Log($m) {
  try {
    ('[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) |
      Out-File -FilePath $log -Append -Encoding utf8
  } catch {}
}
Write-Log "wait for pid $appPid"
$gone = $false
for ($i = 0; $i -lt 60; $i++) {
  if (-not (Get-Process -Id $appPid -ErrorAction SilentlyContinue)) { $gone = $true; break }
  Start-Sleep -Milliseconds 500
}
if (-not $gone) { Write-Log 'still running; abort'; exit 2 }
Start-Sleep -Milliseconds 500
try {
  robocopy $src $dst /E /IS /IT /R:1 /W:1 /XD "$dst\data" /NFL /NDL /NJH /NJS /NP | Out-Null
  if ($LASTEXITCODE -ge 8) { throw "robocopy exit $LASTEXITCODE" }
  $oldInternal = Join-Path $dst '_internal'
  if ((Test-Path $oldInternal) -and -not (Test-Path (Join-Path $src '_internal'))) {
    Remove-Item $oldInternal -Recurse -Force -ErrorAction SilentlyContinue
  }
  Start-Process -FilePath $exe -WorkingDirectory $dst
  Remove-Item $src -Recurse -Force -ErrorAction SilentlyContinue
  Write-Log 'ok'
} catch {
  Write-Log ('failed: ' + $_.Exception.Message)
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


def apply(zip_path: str, store_dir: str, install_dir: str | None = None,
          pid: int | None = None) -> dict:
    """Stage the package and hand the swap to a detached PowerShell script."""
    if os.name != 'nt':
        raise RuntimeError('自动更新只支持 Windows')
    if not is_compiled():
        raise RuntimeError('源码运行时不执行自动替换')

    from ..build_info import app_root
    install = os.path.realpath(install_dir or app_root())
    staged = stage(zip_path, store_dir)
    updates = _updates_dir(store_dir)
    script = os.path.join(updates, 'apply-update.ps1')
    log = os.path.join(updates, 'apply-update.log')
    app_pid = int(pid if pid is not None else os.getpid())
    text = _SCRIPT_TEMPLATE % {
        'pid': app_pid,
        'src': _quote(staged['path']),
        'dst': _quote(install),
        'exe': _quote(os.path.join(install, ENTRY_NAME)),
        'log': _quote(log),
    }
    with open(script, 'w', encoding='utf-8-sig', newline='\r\n') as fh:
        fh.write(text)
    _spawn(script, updates)
    return {'staging': staged['path'], 'files': staged['files'],
            'install': install, 'script': script, 'log': log,
            'pid': app_pid}


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
    ]
    return out

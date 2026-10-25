"""Generate ``dist/latest.json`` for the in-app updater.

The manifest is deliberately small and boring: a client downloads it, compares
``version`` with its own build, then downloads the asset whose ``sha256`` it
verifies before replacing the onedir install.  Relative asset URLs are resolved
against the manifest URL, so the same file works on any host or local folder.

    python packaging/make_latest.py --dist dist
    python packaging/make_latest.py --dist dist --base-url https://example.com/hxp
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

APP_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIST = APP_ROOT / 'dist'
DEFAULT_CHANNEL = 'stable'
DEFAULT_MIN_VERSION = '0.1.1'
DEFAULT_ENTRY = '好想跑.exe'

#: Release notes are kept with the manifest generator so a rebuild cannot
#: publish a version with the previous version's notes.
RELEASE_NOTES = {
    '0.1.2': [
        '新增应用内检查更新：下载校验后自动解压并重启安装',
        '新增海拔来源：自动 / 固定 / 区间 / 本机 GPX/JSON 模板',
        '围栏校验升级为整段检查，提示越界点与越界路段',
        '新增记录原始详情 JSON 调试命令与构建标识',
        '新增首次启动新手引导；推荐手绘路线，自动功能仍在开发中',
        '闭合路线默认关闭；导入轨迹不再自动首尾相连',
        '记录详情 OBS 回读支持旧版 uuid key（v0.1.2 修复）',
        '打包改用 Nuitka 编译，发布包不再包含核心 Python 源码',
        '新增 latest.json 更新元数据（版本 / SHA-256 / 回滚）',
    ],
}


def read_version(app_root: Path = APP_ROOT) -> str:
    path = app_root / 'replica' / 'build_info.py'
    text = path.read_text(encoding='utf-8')
    match = re.search(r"^VERSION\s*=\s*'([^']+)'", text, re.M)
    if not match:
        raise SystemExit(f'cannot read VERSION from {path}')
    return match.group(1)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def asset_url(name: str, base_url: str = '') -> str:
    """Percent-encoded name, absolute when a base URL was supplied."""
    quoted = quote(name)
    base = (base_url or '').strip().rstrip('/')
    return f'{base}/{quoted}' if base else quoted


def _asset(path: Path, base_url: str, version: str,
           layout: str = 'onedir', entry: str = DEFAULT_ENTRY) -> dict:
    return {
        'file': path.name,
        'url': asset_url(path.name, base_url),
        'size': path.stat().st_size,
        'sha256': sha256_file(path),
        'format': 'zip',
        'layout': layout,
        'entry': entry,
        'version': version,
    }


def build_manifest(version: str, zip_path: Path, *,
                   base_url: str = '',
                   channel: str = DEFAULT_CHANNEL,
                   min_version: str = DEFAULT_MIN_VERSION,
                   released_at: str | None = None,
                   notes: list[str] | None = None,
                   rollback_version: str | None = None,
                   rollback_zip: Path | None = None,
                   entry: str = DEFAULT_ENTRY) -> dict:
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise SystemExit(f'zip not found: {zip_path}')
    if notes is None:
        notes = list(RELEASE_NOTES.get(version, []))
    if released_at is None:
        now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8)))
        released_at = now.replace(microsecond=0).isoformat()

    manifest = {
        'schema': 1,
        'app': 'haoxiangpao',
        'name': '好想跑',
        'channel': channel,
        'version': version,
        'min_version': min_version,
        'released_at': released_at,
        'notes': notes,
        'assets': {
            'windows-x64': _asset(zip_path, base_url, version, entry=entry),
        },
        'generated_by': 'packaging/make_latest.py',
    }

    if rollback_version and rollback_zip:
        rollback_zip = Path(rollback_zip)
        if rollback_zip.is_file():
            manifest['rollback'] = _asset(
                rollback_zip, base_url, rollback_version, entry=entry)
        else:
            print(f'warning: rollback zip not found: {rollback_zip}',
                  file=sys.stderr)
    return manifest


def find_zip(dist: Path, version: str) -> Path:
    return dist / f'好想跑_v{version}_win64.zip'


def find_rollback(dist: Path, version: str | None) -> Path | None:
    if not version:
        return None
    candidate = find_zip(dist, version)
    return candidate if candidate.is_file() else None


def write_manifest(manifest: dict, out: Path) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + '\n'
    with out.open('w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='generate dist/latest.json')
    ap.add_argument('--dist', default=str(DEFAULT_DIST))
    ap.add_argument('--version', default=None,
                    help='defaults to replica/build_info.py VERSION')
    ap.add_argument('--zip', default=None, help='asset zip path')
    ap.add_argument('--out', default=None, help='defaults to <dist>/latest.json')
    ap.add_argument('--base-url', default='',
                    help='absolute download base; omit for relative URLs')
    ap.add_argument('--channel', default=DEFAULT_CHANNEL)
    ap.add_argument('--min-version', default=DEFAULT_MIN_VERSION)
    ap.add_argument('--released-at', default=None,
                    help='ISO-8601; defaults to now in Beijing time')
    ap.add_argument('--notes-file', default=None,
                    help='JSON array of release notes; defaults to built-in notes')
    ap.add_argument('--rollback-version', default='0.1.1',
                    help='include this older zip as rollback; empty disables')
    args = ap.parse_args(argv)

    dist = Path(args.dist).resolve()
    version = args.version or read_version()
    zip_path = Path(args.zip) if args.zip else find_zip(dist, version)
    if not zip_path.is_absolute():
        zip_path = (Path.cwd() / zip_path).resolve()

    notes = None
    if args.notes_file:
        notes = json.loads(Path(args.notes_file).read_text(encoding='utf-8'))
        if not isinstance(notes, list):
            raise SystemExit('--notes-file must contain a JSON array')

    rollback_version = (args.rollback_version or '').strip() or None
    rollback_zip = find_rollback(dist, rollback_version)
    manifest = build_manifest(
        version, zip_path,
        base_url=args.base_url,
        channel=args.channel,
        min_version=args.min_version,
        released_at=args.released_at,
        notes=notes,
        rollback_version=rollback_version,
        rollback_zip=rollback_zip,
    )
    out = Path(args.out) if args.out else dist / 'latest.json'
    write_manifest(manifest, out)

    asset = manifest['assets']['windows-x64']
    print(f'latest.json: {out}')
    print(f'version:     {manifest["version"]} ({manifest["channel"]})')
    print(f'asset:       {asset["file"]} · {asset["size"]} bytes')
    print(f'sha256:      {asset["sha256"]}')
    if manifest.get('rollback'):
        print(f'rollback:    {manifest["rollback"]["version"]} · '
              f'{manifest["rollback"]["file"]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

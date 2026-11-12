"""Probe asset URLs in a generated ``latest.json`` before release.

    python packaging/check_update_mirrors.py --manifest dist/latest.json \
        --min-healthy 5 --out dist/mirror-health.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

APP_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = APP_ROOT / 'dist' / 'latest.json'
DEFAULT_MIRROR_REGISTRY = Path(__file__).resolve().with_name('update_mirrors.json')
DEFAULT_BUDGET = 6.0
DEFAULT_MAX_WORKERS = 8
PROBE_BYTES = 64 * 1024
USER_AGENT = 'haoxiangpao-mirror-check'
MAGIC_BYTES = {
    '.zip': b'PK\x03\x04',
    '.exe': b'MZ',
}
FORMAT_MAGIC = {
    'zip': b'PK\x03\x04',
    'installer': b'MZ',
}


def asset_urls(asset: dict) -> list[str]:
    raw = asset.get('urls')
    if isinstance(raw, list) and raw:
        urls = [str(u).strip() for u in raw if isinstance(u, str) and u.strip()]
    else:
        single = asset.get('url')
        urls = [single.strip()] if isinstance(single, str) and single.strip() else []
    deduped = []
    for url in urls:
        if url not in deduped:
            deduped.append(url)
    return deduped


def _magic_for_asset(asset: dict, url: str) -> bytes:
    name = str(asset.get('file') or '').strip()
    if name:
        suffix = Path(name).suffix.lower()
        if suffix in MAGIC_BYTES:
            return MAGIC_BYTES[suffix]
    fmt = str(asset.get('format') or '').strip().lower()
    if fmt in FORMAT_MAGIC:
        return FORMAT_MAGIC[fmt]
    path_suffix = Path(urlparse(url).path).suffix.lower()
    return MAGIC_BYTES.get(path_suffix, b'')


def collect_probe_targets(manifest: dict) -> list[tuple[str, bytes]]:
    targets: list[tuple[str, bytes]] = []
    seen: set[str] = set()

    candidates: list[dict] = []
    assets = manifest.get('assets')
    if isinstance(assets, dict):
        for asset in assets.values():
            if isinstance(asset, dict):
                candidates.append(asset)
    rollback = manifest.get('rollback')
    if isinstance(rollback, dict):
        candidates.append(rollback)

    for asset in candidates:
        for url in asset_urls(asset):
            if url in seen:
                continue
            seen.add(url)
            targets.append((url, _magic_for_asset(asset, url)))
    return targets


def probe_url(
    url: str,
    magic: bytes = b'',
    *,
    timeout: float | tuple[float, float] = (4.0, 6.0),
) -> dict:
    headers = {
        'User-Agent': USER_AGENT,
        'Range': f'bytes=0-{PROBE_BYTES - 1}',
    }
    started = time.monotonic()
    try:
        with requests.get(
            url,
            stream=True,
            timeout=timeout,
            headers=headers,
            allow_redirects=True,
        ) as resp:
            status = int(resp.status_code)
            if status not in (200, 206):
                return {
                    'ok': False,
                    'status': status,
                    'ttfb_ms': round((time.monotonic() - started) * 1000, 2),
                    'speed_bps': None,
                    'error': f'HTTP {status}',
                }
            got = 0
            head = bytearray()
            ttfb: float | None = None
            magic_checked = False
            for chunk in resp.iter_content(4096):
                if not chunk:
                    continue
                if ttfb is None:
                    ttfb = time.monotonic() - started
                got += len(chunk)
                if magic and not magic_checked and len(head) < len(magic):
                    need = len(magic) - len(head)
                    head.extend(chunk[:need])
                if magic and not magic_checked and len(head) >= len(magic):
                    magic_checked = True
                    if not bytes(head).startswith(magic):
                        return {
                            'ok': False,
                            'status': status,
                            'ttfb_ms': round(ttfb * 1000, 2),
                            'speed_bps': None,
                            'error': f'magic mismatch: expected {magic!r}',
                        }
                if got >= PROBE_BYTES:
                    break
            if got == 0:
                return {
                    'ok': False,
                    'status': status,
                    'ttfb_ms': round((time.monotonic() - started) * 1000, 2),
                    'speed_bps': None,
                    'error': 'empty response',
                }
            if magic and not magic_checked and not bytes(head).startswith(magic):
                return {
                    'ok': False,
                    'status': status,
                    'ttfb_ms': round((ttfb or 0.0) * 1000, 2),
                    'speed_bps': None,
                    'error': f'magic mismatch: expected {magic!r}',
                }
            elapsed = max(time.monotonic() - started, 1e-6)
            return {
                'ok': True,
                'status': status,
                'ttfb_ms': round((ttfb if ttfb is not None else elapsed) * 1000, 2),
                'speed_bps': round(got / elapsed, 1),
                'error': None,
            }
    except Exception as exc:  # noqa: BLE001
        return {
            'ok': False,
            'status': None,
            'ttfb_ms': None,
            'speed_bps': None,
            'error': str(exc) or type(exc).__name__,
        }


def check_manifest(
    manifest: dict,
    *,
    budget: float = DEFAULT_BUDGET,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> dict[str, dict]:
    targets = collect_probe_targets(manifest)
    if not targets:
        return {}
    workers = max(1, min(int(max_workers), DEFAULT_MAX_WORKERS, len(targets)))
    req_timeout = (min(4.0, max(float(budget), 0.1)), max(float(budget), 0.1))
    raw_results: dict[str, dict] = {}
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {
            pool.submit(probe_url, url, magic, timeout=req_timeout): url
            for url, magic in targets
        }
        try:
            for future in as_completed(futures, timeout=max(float(budget), 0.01)):
                url = futures[future]
                try:
                    raw_results[url] = future.result()
                except Exception as exc:  # noqa: BLE001
                    raw_results[url] = {
                        'ok': False,
                        'status': None,
                        'ttfb_ms': None,
                        'speed_bps': None,
                        'error': str(exc) or type(exc).__name__,
                    }
        except FutureTimeout:
            pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    ordered: dict[str, dict] = {}
    for url, _ in targets:
        ordered[url] = raw_results.get(
            url,
            {
                'ok': False,
                'status': None,
                'ttfb_ms': None,
                'speed_bps': None,
                'error': 'timeout',
            },
        )
    return ordered


def count_healthy(manifest: dict, results: dict[str, dict]) -> int:
    assets = manifest.get('assets')
    if isinstance(assets, dict) and assets:
        counts = []
        for asset in assets.values():
            if not isinstance(asset, dict):
                continue
            urls = asset_urls(asset)
            if urls:
                counts.append(
                    sum(1 for u in urls if results.get(u, {}).get('ok'))
                )
        if counts:
            return min(counts)
    return sum(1 for item in results.values() if item.get('ok'))


def filter_registry(
    registry: dict,
    manifest: dict,
    results: dict[str, dict],
    *,
    registry_base_url: str = '',
    rollback_registry_base_url: str = '',
) -> dict:
    assets_map = manifest.get('assets') or {}
    assets = [
        (key, asset)
        for key, asset in assets_map.items()
        if isinstance(asset, dict) and asset_urls(asset)
    ]
    healthy_mirrors: list[dict] = []
    for entry in registry.get('mirrors', []):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get('name') or '').strip()
        template = str(entry.get('template') or '').strip()
        if not name or '{base}' not in template:
            continue
        if not bool(entry.get('enabled', True)):
            continue
        matched_any = False
        all_ok = True
        for key, asset in assets:
            urls = asset_urls(asset)
            primary = str(asset.get('url') or urls[0]).strip()
            if '/' not in primary:
                continue
            override = (
                rollback_registry_base_url
                if key == 'rollback'
                else registry_base_url
            )
            base = (override or '').strip().rstrip('/')
            if not base:
                base = primary.rsplit('/', 1)[0]
            filename = str(asset.get('file') or '').strip()
            quoted_name = quote(filename) if filename else primary.rsplit('/', 1)[-1]
            candidate_base = template.replace('{base}', base).rstrip('/')
            expected_url = f'{candidate_base}/{quoted_name}'
            if expected_url in results:
                matched_any = True
                if not results[expected_url].get('ok'):
                    all_ok = False
                    break
        if matched_any and all_ok:
            healthy_mirrors.append(dict(entry))

    healthy_mirrors.sort(
        key=lambda item: (
            0 if str(item.get('name') or '').strip() == 'official' else 1,
            float(item.get('priority', 100)),
        )
    )
    return {
        'schema': registry.get('schema', 1),
        'mirrors': healthy_mirrors,
    }


def _write_json(data: dict, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    with path.open('w', encoding='utf-8', newline='\n') as fh:
        fh.write(text)
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='probe asset URLs in a generated latest.json manifest'
    )
    ap.add_argument(
        '--manifest',
        default=str(DEFAULT_MANIFEST),
        help=f'path to latest.json (default: {DEFAULT_MANIFEST})',
    )
    ap.add_argument(
        '--min-healthy',
        type=int,
        default=1,
        metavar='N',
        help='minimum healthy mirrors required to exit 0 (default: 1)',
    )
    ap.add_argument(
        '--out',
        default=None,
        metavar='PATH',
        help='optional JSON output path for per-URL probe results',
    )
    ap.add_argument(
        '--write-registry',
        default=None,
        metavar='PATH',
        help='optional output path for a filtered healthy mirror registry',
    )
    ap.add_argument(
        '--registry',
        default=str(DEFAULT_MIRROR_REGISTRY),
        metavar='PATH',
        help='source mirror registry used with --write-registry '
             f'(default: {DEFAULT_MIRROR_REGISTRY})',
    )
    ap.add_argument(
        '--registry-base-url',
        default='',
        metavar='URL',
        help='official base used to expand the registry; needed when the '
             'manifest primary URL is a proxy',
    )
    ap.add_argument(
        '--rollback-registry-base-url',
        default='',
        metavar='URL',
        help='official rollback base for registry expansion',
    )
    ap.add_argument(
        '--budget',
        type=float,
        default=DEFAULT_BUDGET,
        help=f'probe budget in seconds (default: {DEFAULT_BUDGET})',
    )
    ap.add_argument(
        '--max-workers',
        type=int,
        default=DEFAULT_MAX_WORKERS,
        help=f'max concurrent workers, capped at {DEFAULT_MAX_WORKERS}',
    )
    args = ap.parse_args(argv)

    manifest_path = Path(args.manifest)
    if not manifest_path.is_file():
        print(f'error: manifest not found: {manifest_path}', file=sys.stderr)
        return 1
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        print(f'error: cannot read manifest {manifest_path}: {exc}', file=sys.stderr)
        return 1
    if not isinstance(manifest, dict):
        print(f'error: manifest must be a JSON object: {manifest_path}', file=sys.stderr)
        return 1

    results = check_manifest(
        manifest,
        budget=args.budget,
        max_workers=args.max_workers,
    )
    healthy = count_healthy(manifest, results)

    if args.out:
        _write_json(results, Path(args.out))

    if args.write_registry:
        reg_path = Path(args.registry)
        try:
            source_registry = json.loads(reg_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            print(f'error: cannot read registry {reg_path}: {exc}', file=sys.stderr)
            return 1
        filtered = filter_registry(
            source_registry,
            manifest,
            results,
            registry_base_url=args.registry_base_url,
            rollback_registry_base_url=args.rollback_registry_base_url,
        )
        _write_json(filtered, Path(args.write_registry))

    print(
        f'mirror check: {healthy} healthy per asset '
        f'({sum(1 for r in results.values() if r.get("ok"))}/{len(results)} '
        f'URL(s) ok; min_healthy={args.min_healthy})'
    )
    return 0 if healthy >= args.min_healthy else 1


if __name__ == '__main__':
    raise SystemExit(main())

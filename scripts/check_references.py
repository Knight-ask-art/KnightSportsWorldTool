#!/usr/bin/env python3
"""Check the two upstream reference projects for changes.

State lives in output/reference-state.json (gitignored).  The first run
creates a baseline; later runs print only commits/releases/watched paths that
moved.  This is deliberately read-only: cloning and diffing happen on the
InstaBox machine, not on the laptop.

Usage:
    python scripts/check_references.py
    python scripts/check_references.py --update
    python scripts/check_references.py --fail-on-change
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = ROOT / 'output' / 'reference-state.json'

REPOS = (
    {
        'key': 'inverx',
        'repo': 'Inverx/com.zjwh.android_wh_physicalfitness',
        'watch': (
            '协议/7.3.92/core',
            '协议/7.3.92/login',
            '协议/7.3.92/policy',
            '协议/7.3.92/running',
            '协议/7.3.92/README.md',
        ),
    },
    {
        'key': 'demacia',
        'repo': 'nekohy/DemaciaSportsWorldTool',
        'watch': (
            'docs/REALISM_FINDINGS_zh.md',
            'src/track/realism.rs',
            'src/track/altitude.rs',
            'src/track/generator.rs',
            'Cargo.toml',
        ),
    },
)


def gh_api(path: str):
    """Call `gh api` and return parsed JSON, or None for a 404."""
    proc = subprocess.run(
        ['gh', 'api', path], capture_output=True, text=True,
        encoding='utf-8', errors='replace')
    if proc.returncode != 0:
        if 'HTTP 404' in proc.stderr or 'Not Found' in proc.stderr:
            return None
        raise SystemExit(f'gh api {path} failed: {proc.stderr.strip()}')
    return json.loads(proc.stdout)


def latest_commit(repo: str, path: str | None = None) -> dict | None:
    query = f'/repos/{repo}/commits?per_page=1'
    if path:
        query += '&path=' + quote(path, safe='')
    data = gh_api(query)
    if not data:
        return None
    item = data[0] if isinstance(data, list) else data
    return {
        'sha': item['sha'],
        'date': item['commit']['author']['date'],
        'message': item['commit']['message'].splitlines()[0],
    }


def latest_release(repo: str) -> dict | None:
    data = gh_api(f'/repos/{repo}/releases/latest')
    if not data:
        return None
    return {'tag': data.get('tag_name'), 'date': data.get('published_at')}


def collect(entry: dict) -> dict:
    repo = entry['repo']
    print(f"== {repo} ==")
    commit = latest_commit(repo)
    release = latest_release(repo)
    paths = {}
    for path in entry['watch']:
        item = latest_commit(repo, path)
        if item:
            paths[path] = item
    if commit:
        print(f"  HEAD    {commit['sha'][:8]} {commit['date']} {commit['message']}")
    if release:
        print(f"  release {release['tag']} {release['date'] or ''}")
    for path, item in paths.items():
        print(f"  {path}: {item['sha'][:8]} {item['date']} {item['message']}")
    return {'commit': commit, 'release': release, 'paths': paths}


def compare(entry: dict, old: dict | None, new: dict) -> list[str]:
    changes = []
    if not old:
        return [f"{entry['repo']}: baseline created"]
    old_commit = old.get('commit') or {}
    new_commit = new.get('commit') or {}
    if old_commit.get('sha') != new_commit.get('sha'):
        changes.append(
            f"{entry['repo']}: HEAD {old_commit.get('sha', '?')[:8]} -> "
            f"{new_commit.get('sha', '?')[:8]} {new_commit.get('message', '')}")
    old_release = (old.get('release') or {}).get('tag')
    new_release = (new.get('release') or {}).get('tag')
    if old_release != new_release:
        changes.append(f"{entry['repo']}: release {old_release} -> {new_release}")
    old_paths = old.get('paths') or {}
    for path, item in (new.get('paths') or {}).items():
        old_sha = (old_paths.get(path) or {}).get('sha')
        if old_sha != item.get('sha'):
            changes.append(
                f"{entry['repo']}: {path} {old_sha or 'new'}[:8] -> "
                f"{item.get('sha', '?')[:8]} {item.get('message', '')}")
    return changes


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', default=str(DEFAULT_STATE))
    parser.add_argument('--update', action='store_true',
                        help='write the new state after checking')
    parser.add_argument('--fail-on-change', action='store_true',
                        help='exit 2 when a reference moved')
    args = parser.parse_args(argv)

    state_path = Path(args.state)
    try:
        old_state = json.loads(state_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        old_state = {}

    new_state = {}
    changes = []
    for entry in REPOS:
        new_state[entry['key']] = collect(entry)
        changes += compare(entry, old_state.get(entry['key']), new_state[entry['key']])

    print()
    if changes:
        print('CHANGES:')
        for line in changes:
            print('  - ' + line)
    else:
        print('NO CHANGES')

    if args.update:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(new_state, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8')
        print(f'state written: {state_path}')

    if args.fail_on_change and changes and any('baseline' not in c for c in changes):
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

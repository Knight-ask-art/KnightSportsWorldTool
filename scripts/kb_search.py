#!/usr/bin/env python3
"""Offline search over the project knowledge base and docs.

The KB is a curated layer under ``docs/kb/``; ``docs/kb/index.json`` is the
machine-readable catalog.  This script is deliberately dependency-free and
never touches the network.

Usage:
    python scripts/kb_search.py policy
    python scripts/kb_search.py 步频 --scope kb --limit 10
    python scripts/kb_search.py 回执 --json
    python scripts/kb_search.py --check
    python scripts/kb_search.py --list
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KB_DIR = ROOT / 'docs' / 'kb'
DOCS_DIR = ROOT / 'docs'
INDEX_FILE = KB_DIR / 'index.json'

CATEGORIES = {
    'overview', 'protocol', 'risk', 'data', 'frontend',
    'release', 'ops', 'issues', 'sources', 'glossary',
}
LEVELS = {'A', 'B', 'C', 'D'}
REQUIRED_KEYS = ('id', 'title', 'file', 'category', 'tags', 'summary', 'level', 'updated')
TEXT_SUFFIXES = {'.md', '.json'}


def _configure_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError):
        pass


def load_index(kb_dir: Path = KB_DIR) -> dict:
    with open(kb_dir / 'index.json', encoding='utf-8') as fh:
        return json.load(fh)


def validate_index(kb_dir: Path = KB_DIR) -> list[str]:
    """Return a list of human-readable problems; empty means valid."""
    errors: list[str] = []
    index_path = kb_dir / 'index.json'
    if not index_path.is_file():
        return [f'missing {index_path}']

    try:
        data = json.loads(index_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        return [f'cannot read index.json: {exc}']

    if data.get('schema') != 1:
        errors.append('schema must be 1')
    for key in ('app', 'version', 'updated'):
        if not data.get(key):
            errors.append(f'missing top-level {key}')
    if not _is_iso_date(str(data.get('updated', ''))):
        errors.append('updated must be YYYY-MM-DD')

    entries = data.get('entries')
    if not isinstance(entries, list) or not entries:
        return errors + ['entries must be a non-empty list']

    seen_ids: set[str] = set()
    seen_files: set[str] = set()
    for pos, entry in enumerate(entries):
        label = f'entries[{pos}]'
        if not isinstance(entry, dict):
            errors.append(f'{label} is not an object')
            continue
        for key in REQUIRED_KEYS:
            if key not in entry:
                errors.append(f'{label} missing {key}')
        entry_id = str(entry.get('id', ''))
        if entry_id:
            label = f'entries[{pos}] ({entry_id})'
            if entry_id in seen_ids:
                errors.append(f'duplicate id: {entry_id}')
            seen_ids.add(entry_id)
        if entry.get('category') not in CATEGORIES:
            errors.append(f'{label} bad category: {entry.get("category")!r}')
        if entry.get('level') not in LEVELS:
            errors.append(f'{label} bad level: {entry.get("level")!r}')
        if not _is_iso_date(str(entry.get('updated', ''))):
            errors.append(f'{label} updated must be YYYY-MM-DD')
        tags = entry.get('tags')
        if not isinstance(tags, list) or not tags or not all(isinstance(t, str) and t for t in tags):
            errors.append(f'{label} tags must be a non-empty list of strings')
        file_name = str(entry.get('file', ''))
        if not file_name.endswith('.md'):
            errors.append(f'{label} file must end with .md')
            continue
        if file_name in seen_files:
            errors.append(f'duplicate file: {file_name}')
        seen_files.add(file_name)
        if not (kb_dir / file_name).is_file():
            errors.append(f'{label} missing file: {file_name}')

    actual = {p.name for p in kb_dir.glob('*.md') if p.name != 'README.md'}
    missing = sorted(actual - seen_files)
    extra = sorted(seen_files - actual)
    if missing:
        errors.append('unindexed KB file(s): ' + ', '.join(missing))
    if extra:
        errors.append('index points at unknown file(s): ' + ', '.join(extra))

    return errors


def _is_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def iter_files(scope: str) -> list[Path]:
    if scope == 'kb':
        files = [p for p in KB_DIR.rglob('*') if p.suffix in TEXT_SUFFIXES]
        return sorted(files)
    files = [p for p in DOCS_DIR.rglob('*.md')]
    if scope == 'all':
        for extra in (ROOT / 'README.md',):
            if extra.is_file():
                files.append(extra)
        files.extend(sorted(ROOT.glob('RELEASE_NOTES_*.md')))
    return sorted({p.resolve() for p in files})


def _entry_map(kb_dir: Path = KB_DIR) -> dict[str, dict]:
    try:
        data = load_index(kb_dir)
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, dict] = {}
    for entry in data.get('entries', []):
        name = str(entry.get('file', ''))
        if name:
            out[name] = entry
    return out


def search_files(query: str, scope: str = 'all', limit: int = 40,
                 any_term: bool = False) -> list[dict]:
    terms = [t.casefold() for t in query.split() if t]
    if not terms:
        return []
    hits: list[dict] = []
    entries = _entry_map()
    for path in iter_files(scope):
        try:
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        try:
            rel = path.relative_to(ROOT).as_posix()
        except ValueError:
            rel = path.as_posix()
        entry = entries.get(path.name, {})
        for lineno, line in enumerate(text.splitlines(), 1):
            low = line.casefold()
            if any_term:
                matched = any(t in low for t in terms)
            else:
                matched = all(t in low for t in terms)
            if not matched:
                continue
            hits.append({
                'file': rel,
                'line': lineno,
                'text': line.strip()[:400],
                'entry': entry.get('id', '') if rel.startswith('docs/kb/') else '',
                'title': entry.get('title', '') if rel.startswith('docs/kb/') else '',
            })
            if len(hits) >= limit:
                return hits
    return hits


def _print_hits(query: str, hits: list[dict]) -> None:
    if not hits:
        print(f'no matches for: {query}')
        return
    current = None
    for hit in hits:
        if hit['file'] != current:
            current = hit['file']
            heading = current
            if hit.get('title'):
                heading += f"  [{hit['entry']}] {hit['title']}"
            print(f'\n{heading}')
        print(f"  {hit['line']:>5}: {hit['text']}")


def _cmd_check() -> int:
    errors = validate_index()
    if errors:
        print(f'FAIL: {len(errors)} problem(s)')
        for err in errors:
            print(f'  - {err}')
        return 1
    data = load_index()
    print(f"PASS: {len(data['entries'])} entries, 0 problem(s) "
          f"(version {data.get('version')}, updated {data.get('updated')})")
    return 0


def _cmd_list(scope_json: bool) -> int:
    data = load_index()
    rows = data['entries']
    if scope_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    for entry in rows:
        print(f"{entry['id']:<16} {entry['category']:<10} {entry['file']:<24} {entry['title']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Search the 好想跑 knowledge base (offline).')
    parser.add_argument('query', nargs='?', help='search terms; all must match a line')
    parser.add_argument('--scope', choices=('all', 'kb', 'docs'), default='all',
                        help='all = docs + KB + root docs (default); kb = docs/kb only; docs = docs/**/*.md')
    parser.add_argument('--limit', type=int, default=40, help='max hits (default 40)')
    parser.add_argument('--any', action='store_true', help='OR terms instead of AND')
    parser.add_argument('--json', action='store_true', help='JSON output')
    parser.add_argument('--check', action='store_true', help='validate index.json and KB files')
    parser.add_argument('--list', action='store_true', help='list catalog entries')
    args = parser.parse_args(argv)

    _configure_stdout()

    if args.check:
        return _cmd_check()
    if args.list:
        return _cmd_list(args.json)
    if not args.query:
        parser.print_help()
        return 2

    hits = search_files(args.query, scope=args.scope, limit=args.limit,
                        any_term=args.any)
    if args.json:
        print(json.dumps({
            'query': args.query,
            'scope': args.scope,
            'count': len(hits),
            'hits': hits,
        }, ensure_ascii=False, indent=2))
    else:
        _print_hits(args.query, hits)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

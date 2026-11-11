"""Guards for the project knowledge base under ``docs/kb/``.

The KB is only useful if the catalog stays in sync with the files.  These
checks are cheap and run in the normal pytest pass.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
KB = REPO / 'docs' / 'kb'
SEARCH_SCRIPT = REPO / 'scripts' / 'kb_search.py'


def _load_search():
    spec = importlib.util.spec_from_file_location('kb_search', SEARCH_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _catalog() -> dict:
    return json.loads((KB / 'index.json').read_text(encoding='utf-8'))


def test_kb_index_is_consistent():
    module = _load_search()
    assert module.validate_index(KB) == []


def test_kb_required_cards_are_indexed():
    ids = {entry['id'] for entry in _catalog()['entries']}
    assert {'overview', 'protocol', 'risk-rules', 'build-release', 'ops', 'issues'} <= ids


def test_kb_search_is_offline_and_scoped():
    module = _load_search()
    hits = module.search_files('policy', scope='kb', limit=5)
    assert hits, 'expected at least one hit for "policy"'
    assert all(hit['file'].startswith('docs/kb/') for hit in hits)
    assert all(hit['line'] > 0 for hit in hits)


def test_kb_does_not_contain_obvious_credentials():
    patterns = (
        re.compile(r'ghp_[A-Za-z0-9]{20,}'),
        re.compile(r'github_pat_[A-Za-z0-9_]{20,}'),
        re.compile(r'sk-[A-Za-z0-9]{20,}'),
        re.compile(r'eyJhbGciOi[A-Za-z0-9_-]{10,}'),
        re.compile(r'AKIA[0-9A-Z]{16}'),
    )
    for path in KB.rglob('*'):
        if not path.is_file() or path.suffix not in {'.md', '.json'}:
            continue
        text = path.read_text(encoding='utf-8', errors='replace')
        for pattern in patterns:
            assert not pattern.search(text), f'possible credential in {path}'

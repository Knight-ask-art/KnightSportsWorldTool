"""Unit tests for the Agent's in-process and persisted caches."""

from __future__ import annotations

import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from replica.application import agent_cache  # noqa: E402


def test_ttl_cache_hit_miss_expiry_and_eviction(monkeypatch):
    clock = {'now': 1000.0}
    monkeypatch.setattr(agent_cache.time, 'monotonic', lambda: clock['now'])
    cache = agent_cache.TTLCache(ttl_s=10.0, max_entries=2)

    assert cache.get('missing') is None
    cache.put('a', {'value': 1})
    assert cache.get('a') == {'value': 1}
    stats = cache.stats()
    assert stats['hits'] == 1 and stats['misses'] == 1

    clock['now'] += 11.0
    assert cache.get('a') is None

    cache.put('b', 1)
    cache.put('c', 2)
    cache.put('d', 3)  # evicts the oldest-expiring entry
    assert cache.stats()['entries'] == 2
    assert cache.get('d') == 3


def test_stable_hash_ignores_dict_order(tmp_path):
    left = {'b': 2, 'a': [1, 3]}
    right = {'a': [1, 3], 'b': 2}
    assert agent_cache.stable_hash(left) == agent_cache.stable_hash(right)
    assert agent_cache.stable_hash(left) != agent_cache.stable_hash({'a': 1})

    path = tmp_path / 'memory.json'
    path.write_text('{"x": 1}', encoding='utf-8')
    first = agent_cache.file_signature(str(path))
    # Same size, and possibly the same coarse mtime on FAT32/exFAT: the
    # content hash must still change.
    time.sleep(0.01)
    path.write_text('{"x": 2}', encoding='utf-8')
    second = agent_cache.file_signature(str(path))
    assert first != second


def test_choice_cache_round_trip_and_expiry(tmp_path, monkeypatch):
    cache = agent_cache.ChoiceCache(str(tmp_path), ttl_ms=1000)
    assert cache.get('key') is None

    cache.put('key', 'lr_abcd1234', 'qwen2.5:7b')
    hit = cache.get('key')
    assert hit is not None
    assert hit['template_id'] == 'lr_abcd1234'
    assert hit['model'] == 'qwen2.5:7b'
    assert hit['hits'] >= 1

    # The file must contain no prompt text or credentials, only ids/metadata.
    raw = (tmp_path / agent_cache.CHOICE_FILENAME).read_text(encoding='utf-8')
    doc = json.loads(raw)
    assert doc['version'] == agent_cache.CHOICE_SCHEMA_VERSION
    assert 'key' in doc['entries']
    assert 'api_key' not in raw
    assert '22.4' not in raw

    # Expired entries are dropped on the next lookup.
    cache._ttl_ms = 1
    time.sleep(0.01)
    assert cache.get('key') is None
    assert cache.stats()['entries'] == 0


def test_choice_cache_is_bounded(tmp_path):
    cache = agent_cache.ChoiceCache(str(tmp_path), max_entries=2)
    cache.put('a', 'lr_a', 'm')
    cache.put('b', 'lr_b', 'm')
    cache.put('c', 'lr_c', 'm')
    doc = json.loads(
        (tmp_path / agent_cache.CHOICE_FILENAME).read_text(encoding='utf-8'))
    assert len(doc['entries']) == 2
    assert 'a' not in doc['entries']
    assert 'c' in doc['entries']

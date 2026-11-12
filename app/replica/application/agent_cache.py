"""Small, dependency-free caches for the route-memory Agent.

The Agent calls the same read-only tools repeatedly while the user changes the
target distance, flips a candidate or asks for the route again.  Those calls are
deterministic, so caching them is safe as long as the cache key includes the
memory document revision and every geometric input.

Nothing here stores raw tracks, account identifiers, API keys or coordinates.
The LLM choice cache only keeps ``template_id`` / model name / a SHA-256 key.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from typing import Any

CHOICE_FILENAME = 'agent_llm_choices.json'
CHOICE_SCHEMA_VERSION = 1
CHOICE_TTL_MS = 30 * 24 * 3600 * 1000
CHOICE_MAX_ENTRIES = 200


def stable_hash(payload: Any) -> str:
    """Stable SHA-256 for a JSON-serialisable payload (dict order ignored)."""
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def file_signature(path: str) -> str:
    """Revision marker for a memory document.

    ``mtime_ns:size`` alone collides on FAT32/exFAT (2-second mtime
    granularity) when a same-size document is rewritten.  The content hash
    makes the key exact without relying on the filesystem clock.
    """
    try:
        stat = os.stat(path)
        digest = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                digest.update(chunk)
    except OSError:
        return '0:0'
    return f'{stat.st_mtime_ns}:{stat.st_size}:{digest.hexdigest()}'


class TTLCache:
    """Thread-safe in-process TTL cache with a bounded number of entries."""

    def __init__(self, ttl_s: float = 120.0, max_entries: int = 32):
        self._ttl_s = max(1.0, float(ttl_s))
        self._max_entries = max(1, int(max_entries))
        self._lock = threading.RLock()
        self._items: dict[str, tuple[float, Any]] = {}
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Any:
        now = time.monotonic()
        with self._lock:
            item = self._items.get(key)
            if item is None:
                self._misses += 1
                return None
            expires_at, value = item
            if now >= expires_at:
                self._items.pop(key, None)
                self._misses += 1
                return None
            self._hits += 1
            return value

    def put(self, key: str, value: Any) -> None:
        now = time.monotonic()
        with self._lock:
            if len(self._items) >= self._max_entries and key not in self._items:
                # Drop the entry that expires first; all entries share the same
                # TTL, so this is also the oldest one.
                oldest = min(self._items, key=lambda k: self._items[k][0])
                self._items.pop(oldest, None)
            self._items[key] = (now + self._ttl_s, value)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def stats(self) -> dict:
        with self._lock:
            return {
                'entries': len(self._items),
                'hits': self._hits,
                'misses': self._misses,
            }


class ChoiceCache:
    """Persisted cache of LLM candidate choices.

    A hit means the exact same model, endpoint and prompt already produced a
    valid template id.  The cache is deliberately per-store rather than
    per-account because the prompt contains aggregate statistics only.
    """

    def __init__(
        self,
        store_dir: str,
        *,
        ttl_ms: int = CHOICE_TTL_MS,
        max_entries: int = CHOICE_MAX_ENTRIES,
    ):
        self._store_dir = os.path.abspath(store_dir) if store_dir else ''
        self._ttl_ms = max(1000, int(ttl_ms))
        self._max_entries = max(1, int(max_entries))
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0

    def _path(self) -> str:
        return os.path.join(self._store_dir, CHOICE_FILENAME)

    def _load(self) -> dict:
        if not self._store_dir:
            return {}
        try:
            with open(self._path(), encoding='utf-8') as f:
                doc = json.load(f)
        except (OSError, ValueError):
            return {}
        if not isinstance(doc, dict):
            return {}
        if doc.get('version') != CHOICE_SCHEMA_VERSION:
            return {}
        entries = doc.get('entries')
        return entries if isinstance(entries, dict) else {}

    def _save(self, entries: dict) -> None:
        if not self._store_dir:
            return
        folder = self._store_dir
        tmp = None
        try:
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=folder, prefix='.agent-llm-', suffix='.tmp')
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(
                    {'version': CHOICE_SCHEMA_VERSION, 'entries': entries},
                    f,
                    ensure_ascii=False,
                    separators=(',', ':'),
                )
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path())
            tmp = None
        except OSError:
            pass
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def get(self, key: str) -> dict | None:
        if not key:
            return None
        now_ms = int(time.time() * 1000)
        with self._lock:
            entries = self._load()
            item = entries.get(key)
            if not isinstance(item, dict):
                self._misses += 1
                return None
            at_ms = int(item.get('at_ms') or 0)
            template_id = str(item.get('template_id') or '')
            if (not template_id or now_ms - at_ms > self._ttl_ms
                    or now_ms < at_ms - 60_000):
                entries.pop(key, None)
                self._save(entries)
                self._misses += 1
                return None
            item['hits'] = int(item.get('hits') or 0) + 1
            item['last_hit_ms'] = now_ms
            entries[key] = item
            self._save(entries)
            self._hits += 1
            return dict(item)

    def put(self, key: str, template_id: str, model: str = '') -> None:
        if not key or not template_id:
            return
        now_ms = int(time.time() * 1000)
        with self._lock:
            entries = self._load()
            # Drop expired entries before enforcing the size limit.
            for old_key in list(entries):
                item = entries.get(old_key)
                at_ms = int(item.get('at_ms') or 0) if isinstance(item, dict) else 0
                if not isinstance(item, dict) or now_ms - at_ms > self._ttl_ms:
                    entries.pop(old_key, None)
            entries[key] = {
                'template_id': str(template_id),
                'model': str(model or '')[:200],
                'at_ms': now_ms,
                'hits': 0,
            }
            if len(entries) > self._max_entries:
                ordered = sorted(
                    entries.items(),
                    key=lambda kv: int((kv[1] or {}).get('at_ms') or 0),
                )
                for old_key, _ in ordered[:len(entries) - self._max_entries]:
                    entries.pop(old_key, None)
            self._save(entries)

    def stats(self) -> dict:
        with self._lock:
            entries = self._load()
            return {
                'entries': len(entries),
                'hits': self._hits,
                'misses': self._misses,
            }

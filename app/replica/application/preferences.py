"""Small, atomic local preferences shared by the UI and the transport layer."""

from __future__ import annotations

import json
import os
import tempfile

FILENAME = 'settings.json'


def path(store_dir: str) -> str:
    return os.path.join(store_dir, FILENAME)


def load(store_dir: str) -> dict:
    if not store_dir:
        return {}
    try:
        with open(path(store_dir), encoding='utf-8') as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def save(store_dir: str, doc: dict) -> bool:
    """Atomically replace ``settings.json``; never raises."""
    if not store_dir or not isinstance(doc, dict):
        return False
    folder = os.path.abspath(store_dir)
    try:
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix='.settings-', suffix='.json')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(doc, f, ensure_ascii=False, separators=(',', ':'))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path(folder))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except Exception:  # noqa: BLE001
        return False


def get(store_dir: str, key: str, default=None):
    return load(store_dir).get(key, default)


def set(store_dir: str, key: str, value) -> dict:
    doc = load(store_dir)
    doc[str(key)] = value
    save(store_dir, doc)
    return doc

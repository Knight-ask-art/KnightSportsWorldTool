"""Named run presets stored next to the receipts.

A preset is the set of parameters the submit form carries (distance, pace,
cadence, altitude options, route, closed flag, ...).  It is local-only: the
file never leaves ``store_dir`` and whitelisting below means a token, a
password or an OBS signature cannot be saved even by accident.

Rules the bridge/UI rely on:

* ``list`` / ``get`` never raise: a corrupt file reads as an empty table, so a
  bad write can never block startup;
* ``save`` is an upsert by name (same name replaces in place) and caps the
  table at :data:`MAX_PRESETS` rows, dropping the oldest;
* every write is an atomic replace.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time

FILENAME = 'presets.json'
MAX_PRESETS = 30
MAX_NAME_LEN = 40
MAX_BYTES = 64 * 1024

#: Scalars copied verbatim after a type check.  Anything not listed here is
#: dropped, which is what keeps credentials and tokens out of the file.
_SCALARS = {
    'km': (int, float),
    'pace': (int, float),
    'cadence': (int, float),
    'altitude': (int, float),
    'closed': (bool,),
    'altitude_mode': (str,),
    'km_times': (list,),
    'sequential': (bool,),
    'free_run': (bool,),
}
#: Structured fields kept as-is when they already have the right shape.
_STRUCTURED = {'altitude_range', 'altitude_profile', 'route', 'waypoints',
               'run_area_id'}

_ACTIONS = ('list', 'save', 'delete', 'get')


class PresetError(ValueError):
    """Caller mistake: bad action, missing name, oversized payload."""


# --------------------------------------------------------------------------
# io
# --------------------------------------------------------------------------

def presets_path(store_dir: str) -> str:
    return os.path.join(store_dir, FILENAME)


def _atomic_write(path: str, doc: dict) -> None:
    folder = os.path.dirname(path) or '.'
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.presets-', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load(store_dir: str) -> list[dict]:
    """Rows from disk; a missing, oversized or corrupt file reads as []."""
    path = presets_path(store_dir)
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return []
        with open(path, encoding='utf-8') as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return []
    rows = doc.get('presets') if isinstance(doc, dict) else doc
    if not isinstance(rows, list):
        return []
    return [r for r in (_clean_row(r) for r in rows) if r is not None]


def _store(store_dir: str, rows: list[dict]) -> None:
    _atomic_write(presets_path(store_dir),
                  {'version': 1, 'presets': rows[:MAX_PRESETS]})


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def clean_name(name) -> str:
    name = re.sub(r'\s+', ' ', str(name or '')).strip()
    if not name:
        raise PresetError('预设名称不能为空')
    if len(name) > MAX_NAME_LEN:
        raise PresetError('预设名称最多 %d 个字' % MAX_NAME_LEN)
    return name


def _clean_value(key: str, value):
    if value is None:
        return None
    kind = _SCALARS.get(key)
    if kind is not None:
        if isinstance(value, bool) and bool not in kind:
            return None                      # True is not a distance
        if not isinstance(value, kind):
            return None
        if key == 'km_times' and (
                len(value) > 100
                or not all(isinstance(v, (int, float))
                           and not isinstance(v, bool) for v in value)):
            return None
        if isinstance(value, str):
            value = value.strip()[:40]
        return value
    if key in _STRUCTURED:
        if key in ('altitude_range', 'route', 'waypoints',
                   'altitude_profile'):
            # altitude_profile is a flat list of metres (one per GPX point).
            return value if isinstance(value, list) and len(value) <= 5000 \
                else None
        return value if isinstance(value, (dict, list, str)) else None
    return None


def clean_data(data) -> dict:
    """Whitelist + light type check.  Unknown keys are dropped silently."""
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise PresetError('预设数据必须是对象')
    out = {}
    for key, value in data.items():
        cleaned = _clean_value(str(key), value)
        if cleaned is not None or (value is None and key in _SCALARS):
            out[str(key)] = cleaned
    return out


def _clean_row(row) -> dict | None:
    if not isinstance(row, dict):
        return None
    raw = str(row.get('name') or '')
    if not raw.strip():
        return None
    return {
        'name': raw.strip()[:MAX_NAME_LEN],
        'data': clean_data(row.get('data') or {}),
        'created_at': _int(row.get('created_at')),
        'updated_at': _int(row.get('updated_at')),
    }


def _int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def list_presets(store_dir: str) -> dict:
    rows = _load(store_dir)
    rows.sort(key=lambda r: r.get('updated_at') or 0, reverse=True)
    return {'presets': rows, 'count': len(rows), 'max': MAX_PRESETS}


def get_preset(store_dir: str, name) -> dict | None:
    wanted = str(name or '').strip()
    for row in _load(store_dir):
        if row['name'] == wanted:
            return row
    return None


def save_preset(store_dir: str, name, data) -> dict:
    name = clean_name(name)
    entry = clean_data(data)
    if not entry:
        raise PresetError('预设数据为空：至少填写距离或配速')
    now = int(time.time() * 1000)
    rows = _load(store_dir)
    # A delete+save inside the same millisecond must not reorder rows; the
    # next free timestamp keeps the "newest first" list stable.
    newest = max([r.get('updated_at') or 0 for r in rows] or [0])
    now = max(now, newest + 1)
    replaced = False
    for row in rows:
        if row['name'] == name:
            # Keep created_at across edits: the UI sorts by updated_at, but
            # "saved on" is still useful when a preset is reused for weeks.
            created = row.get('created_at') or now
            row['data'] = entry
            row['updated_at'] = now
            row['created_at'] = created
            replaced = True
            break
    if not replaced:
        rows.append({'name': name, 'data': entry,
                     'created_at': now, 'updated_at': now})
    rows.sort(key=lambda r: r.get('updated_at') or 0, reverse=True)
    dropped = rows[MAX_PRESETS:]
    rows = rows[:MAX_PRESETS]
    try:
        _store(store_dir, rows)
    except OSError as e:
        raise PresetError('预设写入失败: %s' % e) from e
    return {'saved': True, 'name': name, 'replaced': replaced,
            'count': len(rows), 'dropped': [r['name'] for r in dropped],
            'max': MAX_PRESETS}


def delete_preset(store_dir: str, name) -> dict:
    wanted = str(name or '').strip()
    if not wanted:
        raise PresetError('缺少预设名称')
    rows = _load(store_dir)
    kept = [r for r in rows if r['name'] != wanted]
    deleted = len(kept) != len(rows)
    if deleted:
        _store(store_dir, kept)
    return {'deleted': deleted, 'name': wanted, 'count': len(kept),
            'max': MAX_PRESETS}


def handle(store_dir: str, action, name=None, data=None) -> dict:
    """Bridge entry: one action, one result.  Raises :class:`PresetError`."""
    action = str(action or 'list').strip().lower()
    if action not in _ACTIONS:
        raise PresetError('未知操作：%s' % action)
    if action == 'list':
        return list_presets(store_dir)
    if action == 'get':
        row = get_preset(store_dir, name)
        if row is None:
            raise PresetError('没有这个预设：%s' % (name or ''))
        return {'preset': row}
    if action == 'save':
        return save_preset(store_dir, name, data)
    return delete_preset(store_dir, name)


def self_test() -> list[tuple[str, bool, str]]:
    import shutil

    tmp = tempfile.mkdtemp(prefix='presets-')
    out = []
    try:
        save_preset(tmp, '早跑', {'km': 2.5, 'pace': 6.0, 'token': 'x'})
        row = get_preset(tmp, '早跑')
        out.append(('save/get', bool(row) and row['data']['km'] == 2.5,
                    str(row)))
        out.append(('no secrets', row is not None
                    and 'token' not in row['data'], 'ok'))
        with open(presets_path(tmp), 'w', encoding='utf-8') as f:
            f.write('{broken')
        out.append(('corrupt -> empty', list_presets(tmp)['count'] == 0, 'ok'))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out

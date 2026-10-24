"""A local outdoor semester target.

The server does not always configure an outdoor goal.  A semester can be
“indoor only” (``goalMethod=2``), where ``semesterGoal`` counts indoor
sessions and there is no distance target at all; the official app then shows
no outdoor goal.  The UI still needs something to compute “还差多少”, so the
operator may enter a target here.

The value is local-only, never uploaded, and a server-side goal always wins.
"""

from __future__ import annotations

import json
import os
import time

FILENAME = 'semester_goal.json'
MIN_KM = 0.1
MAX_KM = 5000.0


def _base_dir() -> str:
    base = os.environ.get('JHP_DATA_DIR')
    if not base:
        base = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
    return base


def goal_path(store_dir: str | None = None) -> str:
    return os.path.join(store_dir or _base_dir(), FILENAME)


def load(store_dir: str | None = None) -> dict:
    """Never raises: a corrupt file simply means “no local target”."""
    try:
        with open(goal_path(store_dir), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {'target_km': None, 'sid': None, 'updated_at': None}
    if not isinstance(data, dict):
        return {'target_km': None, 'sid': None, 'updated_at': None}
    try:
        target = data.get('target_km')
        target = None if target is None else round(float(target), 2)
    except (TypeError, ValueError):
        target = None
    if target is not None and not (MIN_KM <= target <= MAX_KM):
        target = None
    return {'target_km': target, 'sid': data.get('sid'),
            'updated_at': data.get('updated_at')}


def save(store_dir: str | None = None, target_km=None, sid=None) -> dict:
    """Blank clears the target; anything else must be a sane kilometre value.

    ``sid`` ties the target to one semester: next term must not silently
    inherit last term's goal.
    """
    if target_km is None or str(target_km).strip() == '':
        clean = {'target_km': None, 'sid': None, 'updated_at': None}
    else:
        try:
            value = round(float(str(target_km).strip()), 2)
        except (TypeError, ValueError):
            raise ValueError('目标必须是数字（km）')
        if not (MIN_KM <= value <= MAX_KM):
            raise ValueError('目标需在 %s–%s km 之间' % (MIN_KM, MAX_KM))
        clean = {'target_km': value, 'sid': sid,
                 'updated_at': int(time.time() * 1000)}
    base = store_dir or _base_dir()
    os.makedirs(base, exist_ok=True)
    path = goal_path(base)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(clean, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return clean

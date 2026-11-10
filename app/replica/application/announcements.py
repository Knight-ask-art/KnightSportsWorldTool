"""In-app announcement catalog.

Add one dict to ``ANNOUNCEMENTS`` to publish a notice; no frontend change is
needed.  ``version`` is per-id: bump it when the content changes so users who
already dismissed the old text see the new one.  Images live under
``app/frontend/assets/`` and are referenced as ``assets/<name>``.

The bridge exposes the active items through ``real_get_announcements``.  The
frontend keeps the per-id seen versions in ``data/ui_state.json`` under
``announcement_seen`` (``{id: version}``), so a random localhost port does not
make the notice reappear on every launch.
"""

from __future__ import annotations

import time

#: Publish a new notice by appending a dict here.
#: Required keys: id, version, title.
#: Optional keys: kicker, body, note, image, image_alt, meta, copy, copy_label,
#: dismiss, ok, enabled.
ANNOUNCEMENTS = (
    {
        'id': 'qq-group-2026-11',
        'version': 2,
        'enabled': True,
        'kicker': '社区',
        'title': '加入 HAOXIANGPAO QQ 群',
        'body': '扫码进群，反馈问题、获取更新。',
        'note': '加群暗号：皮卡丘',
        'image': 'assets/qq-group.jpg',
        'image_alt': 'HAOXIANGPAO QQ 群二维码',
        'dismiss': '下次不再自动显示',
        'ok': '知道了',
    },
)


def current(now: float | None = None) -> list[dict]:
    """Return copies of the enabled announcements.

    ``now`` is accepted for future scheduling; the current catalog is always
    active.  Callers must not mutate the returned dicts expecting to change the
    catalog.
    """
    del now
    return [dict(item) for item in ANNOUNCEMENTS if item.get('enabled', True)]


def by_id(announcement_id: str) -> dict | None:
    for item in current():
        if item.get('id') == announcement_id:
            return item
    return None

"""Read-only post-submit verification.

This module never calls `save/record`, never signs an upload and never touches
OBS.  It reads one record through the normal detail path and explains what the
server actually stored.
"""

from __future__ import annotations

from ..domain import safety
from . import readonly


def _step(timeline, name, ok, text, level='ok'):
    timeline.append({'step': name, 'ok': bool(ok), 'level': level,
                     'text': str(text)[:300]})


def verify_record(client, acct, rrid=None, uuid=None) -> dict:
    at = safety.fmt_beijing(safety.now_ms())
    timeline = []
    if not rrid and not uuid:
        return {'ok': False, 'verdict': 'unknown', 'timeline': [
            {'step': 'input', 'ok': False, 'level': 'error',
             'text': '缺少 rrid/uuid'}],
            'detail': {}, 'track': {}, 'next': '先选择一条服务器记录', 'at': at}

    try:
        d = readonly.record_detail(client, acct, rrid, uuid=uuid)
    except Exception as e:  # noqa: BLE001
        _step(timeline, 'detail', False, '详情请求失败: %s' % e, 'error')
        return {'ok': False, 'verdict': 'unknown', 'timeline': timeline,
                'detail': {}, 'track': {}, 'next': '先确认记录是否已入库，不要重提',
                'at': at}

    _step(timeline, 'detail', bool(d.get('ok')),
          '详情读取成功' if d.get('ok') else (d.get('error') or '详情失败'),
          'ok' if d.get('ok') else 'error')
    if not d.get('ok'):
        return {'ok': False, 'verdict': 'unknown', 'timeline': timeline,
                'detail': {}, 'track': {}, 'next': '记录可能尚未入库；先只读重试',
                'at': at}

    detail = d.get('detail') or {}
    stats = d.get('stats') or {}
    analysis = d.get('analysis') or {}
    track = d.get('track') or []
    source = stats.get('track_source') or 'none'
    points = len(track)

    _step(timeline, 'track', points >= 2,
          '轨迹 %d 点（%s）' % (points, source),
          'ok' if points >= 2 else 'warn')
    if d.get('detail', {}).get('obs_error'):
        _step(timeline, 'obs', False, detail.get('obs_error'), 'warn')
    elif source == 'obs':
        _step(timeline, 'obs', True, 'OBS 轨迹已读取', 'ok')

    complete = detail.get('complete')
    _step(timeline, 'complete', complete is True,
          'complete=%s' % complete,
          'ok' if complete is True else 'warn')
    cheat = detail.get('is_cheat')
    _step(timeline, 'isCheat', cheat in (0, None),
          'isCheat=%s' % cheat,
          'ok' if cheat in (0, None) else 'error')
    state = detail.get('state') or 'unknown'
    _step(timeline, 'state', state != 'issue', '平台判定: %s' % state,
          'ok' if state != 'issue' else 'error')

    reasons = detail.get('reasons') or []
    bad = [r for r in reasons if isinstance(r, dict) and not r.get('complete')]
    _step(timeline, 'reasons', not bad,
          ('未完成项 %d 个' % len(bad)) if bad else
          ('完成度 %d/%d' % (len(reasons) - len(bad), len(reasons)) if reasons else '无 reasonList'),
          'ok' if not bad else 'warn')

    series = analysis.get('series') or []
    _step(timeline, 'analysis', bool(series),
          '图表序列 %d 点' % len(series),
          'ok' if series else 'warn')

    if state == 'issue' or cheat not in (0, None):
        verdict = 'issue'
        nxt = '不要重提；先看 reasonList/statusInfo，必要时申诉。'
    elif not complete or points < 2 or (detail.get('obs_error') and source != 'inline'):
        verdict = 'warn'
        nxt = '记录已存在但核验不完整；只读刷新一次详情，不要重复提交。'
    else:
        verdict = 'ok'
        nxt = '无需重试。'

    _step(timeline, 'verdict', verdict == 'ok', '结论: %s' % verdict,
          'ok' if verdict == 'ok' else ('error' if verdict == 'issue' else 'warn'))
    return {
        'ok': True,
        'verdict': verdict,
        'timeline': timeline,
        'detail': {
            'rrid': detail.get('rrid'),
            'uuid': detail.get('uuid'),
            'complete': complete,
            'isCheat': cheat,
            'statusInfo': detail.get('status') or '',
            'state': state,
            'reasons': reasons,
        },
        'track': {'points': points, 'source': source,
                  'distance_m': detail.get('distance_m'),
                  'duration_s': detail.get('duration_s')},
        'next': nxt,
        'at': at,
    }

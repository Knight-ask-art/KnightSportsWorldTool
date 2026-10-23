"""只读采集：从账号的最近记录里挑选达标样本，写入私有样本库。

设计约束：

* 只读：只调用 ``readonly.recent_records`` / ``readonly.record_detail``；
* 达标门槛：距离 >= :data:`corpus.MIN_DISTANCE_M`，且平台判定通过；
* 增量：新样本按 `rrid`/`uuid` 合并，不覆盖此前已收集的历史样本；
* 显式：由调用方主动触发，登录/提交流程不会隐式采集。

本模块属于私有研究区，不注册到产品 bridge，不参与打包。
"""

from __future__ import annotations

from . import corpus

#: 与公共模块的只读查询能力复用；迁移分层后改为 application 路径。
from replica.application import readonly


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def style_eligible(record: dict) -> bool:
    """A record may enter the style library only if it is a >=3 km pass.

    Deliberately conservative: a missing distance/completion flag is a no,
    not a maybe.  The list summary is enough for this gate; the caller still
    checks the detail's `complete` and track before storing.
    """
    if not isinstance(record, dict):
        return False
    if _num(record.get('distance_m')) < corpus.MIN_DISTANCE_M:
        return False
    if record.get('complete') is not True:
        return False
    reasons = record.get('reasons') or []
    if reasons and not all(bool(r.get('complete')) for r in reasons
                          if isinstance(r, dict)):
        return False
    status = str(record.get('status') or '')
    if '无效' in status or '异常' in status or '作弊' in status:
        return False
    return True


def filter_style(records: list) -> list:
    return [r for r in (records or []) if style_eligible(r)]


def collect_style(client, acct, limit: int = 50) -> dict:
    """Read-only: list -> filter >=3 km pass -> detail -> merge into store.

    Returns a small report; never raises on a single bad record.  The caller
    is responsible for having permission to read this account.
    """
    listing = readonly.recent_records(client, acct, limit=limit)
    if not listing.get('ok'):
        return {'ok': False, 'count': 0, 'skipped': 0,
                'error': listing.get('error') or listing.get('message'),
                'path': corpus.corpus_path(acct.uid)}
    eligible = filter_style(listing.get('records') or [])

    # Collection is incremental, not a replacement.  The record list is only
    # the latest N rows, so rewriting the file with just those rows silently
    # deleted older samples the operator had already collected.
    samples = {}
    for i, old in enumerate(corpus.load_samples(acct.uid)):
        if not isinstance(old, dict):
            continue
        key = str(old.get('rrid') or old.get('uuid') or ('old-%d' % i))
        samples[key] = old
    kept = len(samples)

    count = 0
    skipped = 0
    errors = []
    for rec in eligible:
        try:
            detail = readonly.record_detail(
                client, acct, rec.get('rrid'), rec.get('uuid'))
        except Exception as e:  # noqa: BLE001
            skipped += 1
            errors.append('rrid %s: %s' % (rec.get('rrid'), e))
            continue
        d = detail.get('detail') or {}
        if (not detail.get('ok') or not d.get('complete')
                or not detail.get('stats', {}).get('has_track')):
            skipped += 1
            continue
        sample = {
            'rrid': rec.get('rrid'),
            'uuid': rec.get('uuid') or '',
            'start_ms': rec.get('start_ms'),
            'summary': {
                'distance_m': rec.get('distance_m'),
                'duration_s': rec.get('duration_s'),
                'pace_s_per_km': rec.get('pace_s_per_km'),
                'cadence': rec.get('avg_step_freq'),
                'steps': rec.get('steps'),
                'calorie': rec.get('calorie'),
            },
            'detail': d,
            'track': detail.get('track') or [],
            'five_point': detail.get('five_point') or [],
            'stats': detail.get('stats') or {},
        }
        key = str(rec.get('rrid') or rec.get('uuid') or ('new-%d' % count))
        samples[key] = sample
        count += 1

    path = corpus.write_samples(acct.uid, samples.values())
    return {'ok': True, 'count': count, 'kept': kept, 'total': len(samples),
            'skipped': skipped, 'eligible': len(eligible),
            'errors': errors[:10], 'path': path,
            'min_distance_m': corpus.MIN_DISTANCE_M}


def self_test() -> list[tuple[str, bool, str]]:
    out = []
    base = {'distance_m': 3000, 'complete': True, 'reasons': [],
            'status': '有效'}
    out.append(('3km pass is eligible', style_eligible(base), 'ok'))
    out.append(('2.9km is excluded',
                not style_eligible(dict(base, distance_m=2999.9)), 'ok'))
    out.append(('incomplete is excluded',
                not style_eligible(dict(base, complete=False)), 'ok'))
    out.append(('failed reason is excluded',
                not style_eligible(dict(base, reasons=[
                    {'reason': '距离不足', 'complete': False}])), 'ok'))
    out.append(('invalid status is excluded',
                not style_eligible(dict(base, status='无效')), 'ok'))
    out.append(('filter keeps only passes',
                len(filter_style([base, dict(base, distance_m=1000)])) == 1,
                'ok'))
    return out

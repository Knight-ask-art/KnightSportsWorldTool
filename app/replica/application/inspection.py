"""Read-only pre-flight inspection: what would stop a submission right now.

This is the "doctor" card on the overview page.  It looks at local state only
(session file, keep-alive state, the local daily counter, the last receipt,
the clock offset) plus, when the caller has already fetched them, the record
list and the server-time probe.  It never submits, never retries and never
issues a request of its own: the bridge hands in whatever it already knows.

Every item has the same shape::

    {"id": "token", "label": "token 状态", "level": "ok|warn|error|info",
     "text": "...", "ok": true}

``ok`` is the overall verdict: no item at ``error`` level.
"""

from __future__ import annotations

from ..domain import safety

LEVELS = ('ok', 'warn', 'error', 'info')
#: A server-time offset beyond this is worth a warning (clock.py corrects it
#: automatically, so it is never an error).
CLOCK_WARN_MS = 60_000
DEFAULT_MAX_PER_DAY = 2


def _item(item_id: str, label: str, level: str, text: str, **extra) -> dict:
    level = level if level in LEVELS else 'info'
    out = {'id': item_id, 'label': label, 'level': level,
           'text': str(text), 'ok': level != 'error'}
    out.update(extra)
    return out


def _int(value, default=0):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def token_item(session: dict | None) -> dict:
    sess = session if isinstance(session, dict) else {}
    keep = sess.get('keepalive') if isinstance(sess.get('keepalive'), dict) \
        else {}
    if sess.get('disabled'):
        return _item('token', 'token 状态', 'error', '账号已禁用，不会发起任何请求',
                     state='disabled')
    if not sess.get('token'):
        return _item('token', 'token 状态', 'error', '未登录：先在「账号」页保存 token',
                     state='missing')
    last_ok = _int(keep.get('last_ok_at'))
    expired = bool(keep.get('token_expired'))
    text = '已保存'
    if expired:
        return _item('token', 'token 状态', 'error',
                     '服务端已拒绝该 token，需要重新登录', state='expired')
    if last_ok:
        text += ' · 最近验证 ' + safety.fmt_beijing(last_ok, '%m-%d %H:%M')
    else:
        text += ' · 本次尚未验证（点账号页的「验证」）'
    return _item('token', 'token 状态', 'ok' if last_ok else 'warn', text,
                 state='ok' if last_ok else 'unverified',
                 last_ok_at=last_ok or None)


def freeze_item(freeze_seconds=0, freeze_until_ms: int | None = None,
                now_ms: int | None = None) -> dict:
    now_ms = safety.now_ms() if now_ms is None else now_ms
    remaining = _int(freeze_seconds)
    if remaining <= 0 and freeze_until_ms:
        remaining = max(0, (_int(freeze_until_ms) - now_ms) // 1000)
    if remaining > 0:
        return _item('freeze', '风控冷却', 'warn',
                     '冷却中，剩余约 %d 分钟' % (remaining // 60 + 1),
                     remaining_s=remaining)
    return _item('freeze', '风控冷却', 'ok', '没有冷却限制', remaining_s=0)


def daily_item(daily: dict | None, max_per_day=DEFAULT_MAX_PER_DAY) -> dict:
    daily = daily if isinstance(daily, dict) else {}
    runs = daily.get('runs')
    limit = _int(max_per_day, DEFAULT_MAX_PER_DAY)
    if runs is None:
        return _item('daily', '今日次数', 'warn',
                     '今日服务端次数未读取（不影响本地提交，但会影响审计）',
                     runs=None, limit=limit)
    runs = _int(runs)
    text = '今日已提交 %d 次' % runs
    if limit > 0:
        text += ' / 上限 %d' % limit
    if runs >= limit > 0:
        return _item('daily', '今日次数', 'warn', text + '（已到上限）',
                     runs=runs, limit=limit)
    return _item('daily', '今日次数', 'ok', text, runs=runs, limit=limit)


def record_item(records, error: str = '') -> dict:
    if error:
        return _item('record', '最近记录', 'warn', '记录读取失败：' + str(error)[:120])
    rows = [r for r in (records or []) if isinstance(r, dict)]
    if not rows:
        return _item('record', '最近记录', 'info', '还没有记录', state='none')
    last = rows[0]
    start = _int(last.get('start_ms'))
    when = safety.fmt_beijing(start, '%m-%d %H:%M') if start else '时间未知'
    state = str(last.get('state') or '')
    label = {'ok': '已通过', 'warn': '待核对', 'issue': '有问题'}.get(state,
                                                                    state or '未知')
    level = {'ok': 'ok', 'warn': 'warn', 'issue': 'error'}.get(state, 'info')
    text = '%s · %.2f km · %s' % (when, float(last.get('distance_km') or 0),
                                  label)
    reasons = [r.get('reason') for r in (last.get('reasons') or [])
               if isinstance(r, dict) and not r.get('complete')]
    if reasons:
        text += ' · ' + str(reasons[0])[:40]
    return _item('record', '最近记录', level, text, state=state or 'unknown')


def clock_item(clock_state: dict | None) -> dict:
    st = clock_state if isinstance(clock_state, dict) else {}
    offset = st.get('offset_ms')
    if offset is None:
        return _item('clock', '服务器时间', 'info', '尚未校准（提交前会自动校准）')
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    try:
        from ..domain import clock as CLOCK
        shown = CLOCK.fmt_skew(offset)
    except Exception:  # noqa: BLE001
        shown = '%+d ms' % offset
    applied = bool(st.get('applied'))
    source = str(st.get('source') or 'local')
    text = '偏差 %s%s（来源 %s）' % (shown, '' if applied else
                                   ' · 未采用', source)
    if abs(offset) >= CLOCK_WARN_MS:
        return _item('clock', '服务器时间', 'warn', text, offset_ms=offset,
                     applied=applied)
    return _item('clock', '服务器时间', 'ok', text, offset_ms=offset,
                 applied=applied)


def last_receipt_item(receipts) -> dict:
    rows = [r for r in (receipts or []) if isinstance(r, dict)]
    if not rows:
        return _item('receipt', '提交回执', 'info', '还没有回执', state='none')
    last = max(rows, key=lambda r: _int(r.get('start')))
    state = str(last.get('state') or 'unknown')
    when = safety.fmt_beijing(_int(last.get('start')), '%m-%d %H:%M') \
        if last.get('start') else ''
    label = {'pending': '待确认', 'unknown': '结果未知',
             'accepted': '已受理', 'rejected': '已拒绝'}.get(state, state)
    level = {'accepted': 'ok', 'rejected': 'info'}.get(state, 'warn')
    hint = '（先去记录页只读核对，核对前不要重提）' if state in ('pending',
                                                               'unknown') else ''
    return _item('receipt', '提交回执', level,
                 ('%s · %s%s' % (when, label, hint)).strip(' ·'),
                 state=state)


def audit_item(audit: dict | None) -> dict:
    if not isinstance(audit, dict):
        return _item('audit', '提交前审计', 'info', '尚未运行')
    summary = audit.get('summary') or {}
    failed = _int(summary.get('failed'))
    warned = _int(summary.get('warned'))
    at = str(audit.get('at') or '')
    if failed:
        return _item('audit', '提交前审计', 'warn',
                     '%s · %d 项失败' % (at, failed), failed=failed)
    return _item('audit', '提交前审计', 'ok',
                 '%s · 通过（%d 项警告）' % (at, warned), failed=0)


def run(*, session=None, freeze_seconds=0, freeze_until_ms=None,
        now_ms: int | None = None, daily=None, max_per_day=DEFAULT_MAX_PER_DAY,
        records=None, records_error: str = '', clock_state: dict | None = None,
        receipts=None, audit: dict | None = None) -> dict:
    """All checks in display order.  Pure: no clock read when now_ms is given."""
    now_ms = safety.now_ms() if now_ms is None else now_ms
    items = [
        token_item(session),
        freeze_item(freeze_seconds, freeze_until_ms, now_ms),
        daily_item(daily, max_per_day),
        record_item(records, records_error),
        clock_item(clock_state),
        last_receipt_item(receipts),
        audit_item(audit),
    ]
    return {
        'ok': all(i['ok'] for i in items),
        'items': items,
        'at': safety.fmt_beijing(now_ms),
    }


def self_test() -> list[tuple[str, bool, str]]:
    now = 1_700_000_000_000
    res = run(session={}, now_ms=now)
    out = [('anonymous -> not ok', res['ok'] is False, str(res['ok'])),
           ('token error', res['items'][0]['level'] == 'error', 'ok'),
           ('has all items', len(res['items']) == 7, str(len(res['items'])))]
    good = run(session={'token': 't', 'keepalive': {'last_ok_at': now}},
               daily={'runs': 0}, records=[], clock_state={'offset_ms': 120,
                                                          'applied': True,
                                                          'source': 'gmttime'},
               now_ms=now)
    out.append(('healthy -> ok', good['ok'] is True,
                str([i['id'] for i in good['items'] if not i['ok']])))
    return out

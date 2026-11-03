"""Human-readable explanations for server and transport errors.

The server's business errors are numeric and often arrive with a short Chinese
message.  The UI used to show the raw string, which tells the operator what
failed but not what to do next.  This module is deliberately pure: it never
touches the network, credentials or local state.
"""

from __future__ import annotations

import re

#: (id, match codes, match text fragments, title, cause, action, retryable)
_RULES = [
    (
        'run-window',
        {11016},
        ('不在有效跑步时间', '有效跑步时间', 'validTime'),
        '不在学校允许的跑步时间',
        '服务端 runModePolicy 的 validTime 拒绝了这次开始/结束时间。',
        '换到 06:30–22:00 内，并确认结束时间也不超过 22:00。',
        False,
    ),
    (
        'gee-low-trust',
        {10003},
        ('验证失败', 'gee', '极验', '滑块'),
        '极验票据未被业务端接受',
        '极验校验通过了，但业务端认为票据信任度不够；自动重解通常还是 10003。',
        '停止连续重试；走手动过验证，再重新登录。',
        False,
    ),
    (
        'points-cooldown',
        {10603},
        ('频次', '频繁', 'rate limit', 'too many'),
        '点位接口进入冷却',
        '同一 uid/跑区 5 分钟内最多拉取固定次数；服务端已经记下这次请求。',
        '用已有缓存或等 5 分钟；不要连续点刷新。',
        False,
    ),
    (
        'auth',
        {10007, 10121, 401, 403},
        ('未登录', 'token', '登录失效', 'unauthorized', 'forbidden'),
        '登录状态失效',
        'token 已过期、被顶号，或请求头没有通过服务端校验。',
        '重新登录；若刚改过设备指纹，先确认新指纹已保存。',
        False,
    ),
    (
        'credentials',
        {10175},
        ('账号或密码', '密码错误', 'password'),
        '账号或密码错误',
        '登录接口明确拒绝了这组凭据。',
        '检查账号/密码；不要反复尝试以免触发风控。',
        False,
    ),
    (
        'not-found',
        {404},
        ('not found', '不存在', 'no mapping', '该接口不存在', '已下线'),
        '接口或记录不存在',
        '可能是详情路径版本不对、记录已被删除，或服务端尚未升级到该版本。',
        '先确认 rrid/uuid；详情会按 v70320 → v70260 → v61200 回退。',
        False,
    ),
    (
        'timeout',
        set(),
        ('timeout', 'timed out', '超时', 'deadline exceeded'),
        '请求超时',
        '网络或服务端在限定时间内没有返回；提交请求可能已经被服务端处理。',
        '先查回执/记录，不要直接重提；超时不是“一定失败”。',
        True,
    ),
    (
        'network',
        set(),
        ('network', 'connection', '连接', 'dns', 'ssl', 'proxy'),
        '网络连接失败',
        '本机到服务端/OBS 的连接没有建立，或代理不可用。',
        '检查网络/代理后重试；只读查询可以重试，提交先核对回执。',
        True,
    ),
    (
        'empty-response',
        set(),
        ('empty', '空响应', '返回为空', '没有返回'),
        '服务端返回空响应',
        'HTTP 可能仍是 200，但信封没有可解析的业务体；常见于验签失败或风控软拒。',
        '看 HTTP 状态、响应长度和最近一次回执；不要用空 200 判断成功。',
        False,
    ),
]

_EMPTY_200 = ('empty-response',)


def _as_code(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def explain(code=None, message: str = '', context: dict | None = None) -> dict:
    """Return ``{title, cause, action, retryable, id, code}``.

    ``context`` may carry ``status`` (HTTP status), ``empty`` (bool) and
    ``stage`` (login/submit/detail/obs).  It is only used to improve the
    explanation; no value is required.
    """
    code_i = _as_code(code)
    text = str(message or '')
    low = text.lower()
    ctx = context if isinstance(context, dict) else {}
    status = _as_code(ctx.get('status'))

    for rule_id, codes, needles, title, cause, action, retryable in _RULES:
        if code_i is not None and code_i in codes:
            return _out(rule_id, code_i, title, cause, action, retryable)
        if any(n.lower() in low for n in needles):
            return _out(rule_id, code_i, title, cause, action, retryable)

    if status == 404:
        _, _, title, cause, action, retryable = _RULES[5]
        return _out('not-found', code_i, title, cause, action, retryable)

    if ctx.get('empty') or (status == 200 and not text):
        rule_id = _EMPTY_200[0]
        for r in _RULES:
            if r[0] == rule_id:
                return _out(r[0], code_i, r[3], r[4], r[5], r[6])

    if code_i is None and not text:
        return _out('unknown', None, '原因未知',
                    '服务端没有给出可识别的错误码或文案。',
                    '保留原始响应长度/状态，先只读核对记录，不要自动重试。',
                    False)

    return _out('unclassified', code_i, '服务端返回了未归类的错误',
                text or '没有可读的 message。',
                '先只读核对记录/回执；确认未入库再人工重试。',
                False)


def _out(rule_id, code, title, cause, action, retryable):
    return {
        'id': rule_id,
        'code': code,
        'title': title,
        'cause': cause,
        'action': action,
        'retryable': bool(retryable),
    }

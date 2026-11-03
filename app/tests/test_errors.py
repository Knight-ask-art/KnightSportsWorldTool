"""Error explanations must be stable and actionable."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from replica.application.errors import explain  # noqa: E402


def test_run_window_code():
    out = explain(11016, '不在有效跑步时间内')
    assert out['id'] == 'run-window'
    assert out['retryable'] is False
    assert '22:00' in out['action']


def test_gee_low_trust_does_not_retry():
    out = explain(10003, '验证失败')
    assert out['id'] == 'gee-low-trust'
    assert out['retryable'] is False


def test_points_cooldown():
    out = explain(10603, '请求过于频繁')
    assert out['id'] == 'points-cooldown'
    assert '5 分钟' in out['action']


def test_timeout_is_retryable_but_not_for_submit():
    out = explain(None, 'read timed out')
    assert out['id'] == 'timeout'
    assert out['retryable'] is True
    assert '回执' in out['action']


def test_empty_200_is_not_success():
    out = explain(None, '', {'status': 200, 'empty': True})
    assert out['id'] == 'empty-response'
    assert out['retryable'] is False


def test_unknown_is_explicit():
    out = explain(None, '')
    assert out['id'] == 'unknown'
    assert out['retryable'] is False

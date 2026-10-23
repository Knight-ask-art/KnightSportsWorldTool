"""Record list rows must expose the server's suspicious flag."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from replica.readonly import normalise_record  # noqa: E402


def _row(**kw):
    base = {
        'rrid': 1, 'startTime': 1792500000, 'totalDis': 1000,
        'totalTime': 600, 'complete': True,
    }
    base.update(kw)
    return normalise_record(base)


def test_numeric_status_two_is_issue():
    r = _row(status=2)
    assert r['state'] == 'issue'
    assert r['status_code'] == 2


def test_numeric_status_zero_is_ok():
    assert _row(status=0)['state'] == 'ok'


def test_suspicious_status_text_is_issue_even_without_code():
    assert _row(statusInfo='你的跑步成绩已被系统判为可疑')['state'] == 'issue'


def test_failed_reason_is_issue():
    r = _row(reasonList=[{'reason': '运动数据有问题', 'complete': False}])
    assert r['state'] == 'issue'


def test_complete_false_without_bad_signal_is_warn():
    assert _row(complete=False, status=0)['state'] == 'warn'

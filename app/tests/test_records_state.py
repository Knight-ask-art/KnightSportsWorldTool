"""Record list rows must expose the server's suspicious flag."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from replica.application.readonly import normalise_record  # noqa: E402


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


def test_numeric_status_one_is_issue():
    """Real flagged runs came back with status=1, not 2."""
    assert _row(status=1)['state'] == 'issue'


def test_numeric_status_zero_is_ok():
    assert _row(status=0)['state'] == 'ok'


def test_is_cheat_one_is_issue_even_when_status_is_zero():
    """10-13 detail: isCheat=1 with a complete record must not paint green."""
    r = _row(status=0, isCheat=1)
    assert r['state'] == 'issue'
    assert _row(status=0, isCheat=0)['state'] == 'ok'


def test_uuid_only_detail_is_not_rejected_locally(monkeypatch):
    from types import SimpleNamespace
    from replica.application import readonly
    from replica.transport import obs

    payload = {'rrid': 42, 'uuid': 'u-42', 'startTime': 1792584000000,
               'totalDis': 1000, 'totalTime': 600, 'jsonFromObs': 1,
               'allLocJson': None, 'complete': True, 'status': 0,
               'reasonList': []}
    fake = readonly._FakeClient(pages=[{'error': 10000, 'data': payload}])
    monkeypatch.setattr(obs, 'get_object_bytes',
                        lambda *a, **k: (200, __import__('json').dumps({
                            'run_data': obs.gz_json({'allLocJson': '[]'}),
                            'fixed_point_json': obs.gz_str('{}'),
                            'segment_json': obs.gz_str('')}).encode()))
    out = readonly.record_detail(fake, SimpleNamespace(), None, uuid='u-42')
    assert out['ok'] is True
    assert out['detail']['rrid'] == 42


def test_suspicious_status_text_is_issue_even_without_code():
    assert _row(statusInfo='你的跑步成绩已被系统判为可疑')['state'] == 'issue'


def test_failed_reason_is_issue():
    r = _row(reasonList=[{'reason': '运动数据有问题', 'complete': False}])
    assert r['state'] == 'issue'


def test_complete_false_without_bad_signal_is_warn():
    assert _row(complete=False, status=0)['state'] == 'warn'

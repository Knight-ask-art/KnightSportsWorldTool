"""样本库 / 只读采集的私有回归测试。

运行：``cd 好想跑 && python -m pytest private/research/tests -q``
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[3]
for path in (str(ROOT / 'app'), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from private.research import collection, corpus  # noqa: E402
from replica.application import readonly  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv('JHP_CORPUS_DIR', str(tmp_path / 'corpus'))
    return tmp_path / 'corpus'


def _write(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n'
                            for r in rows), encoding='utf-8')


def test_style_gate():
    base = {'distance_m': 3000, 'complete': True, 'reasons': [],
            'status': '有效'}
    assert collection.style_eligible(base)
    assert not collection.style_eligible(dict(base, distance_m=2999.9))
    assert not collection.style_eligible(dict(base, complete=False))
    assert not collection.style_eligible(
        dict(base, reasons=[{'reason': '距离不足', 'complete': False}]))
    assert not collection.style_eligible(dict(base, status='无效'))
    assert len(collection.filter_style(
        [base, dict(base, distance_m=1000)])) == 1


def test_collect_merges_existing_samples(isolated_store):
    _write(corpus.corpus_path('1'), [{
        'rrid': 1, 'start_ms': 1, 'summary': {'distance_m': 3100},
        'track': [{'lat': 22.41, 'lng': 112.94}], 'five_point': [],
    }])
    listing = {'ok': True, 'records': [{
        'rrid': 2, 'uuid': 'u2', 'start_ms': 2,
        'distance_m': 3200, 'duration_s': 1200,
        'pace_s_per_km': 375, 'avg_step_freq': 170, 'steps': 3400,
        'calorie': 200, 'complete': True, 'reasons': [], 'status': '有效',
    }]}
    detail = {'ok': True, 'detail': {'complete': True},
              'stats': {'has_track': True},
              'track': [{'lat': 22.42, 'lng': 112.95}],
              'five_point': []}
    with mock.patch.object(readonly, 'recent_records', return_value=listing), \
         mock.patch.object(readonly, 'record_detail', return_value=detail):
        report = collection.collect_style(
            object(), SimpleNamespace(uid='1'))

    assert report['ok']
    assert (report['count'], report['kept'], report['total']) == (1, 1, 2)
    kept = list(corpus.load_samples('1'))
    assert {s['rrid'] for s in kept} == {1, 2}


def test_write_samples_is_atomic(isolated_store):
    path = corpus.write_samples('9', [{'rrid': 1}, {'rrid': 2}])
    assert Path(path) == Path(corpus.corpus_path('9'))
    assert [s['rrid'] for s in corpus.load_samples('9')] == [1, 2]
    corpus.write_samples('9', [{'rrid': 3}])
    assert [s['rrid'] for s in corpus.load_samples('9')] == [3]
    assert not [p for p in os.listdir(isolated_store)
                if p.endswith('.tmp')]

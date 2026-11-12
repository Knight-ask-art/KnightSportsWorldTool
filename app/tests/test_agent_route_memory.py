"""Agent backend tests: state machine, intent parsing, trace redaction, bridge wiring."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from replica.adapters.bridge_api import ReplicaApi, _fail, _ok  # noqa: E402
from replica.application import llm_client, llm_settings  # noqa: E402
from replica.application.agent import (  # noqa: E402
    STATES,
    AgentError,
    AgentSession,
    parse_distance_km,
)


def _closed_ring():
    return [
        [22.4101, 112.9401],
        [22.4101, 112.9411],
        [22.4111, 112.9411],
        [22.4111, 112.9401],
        [22.4101, 112.9401],
    ]


def _open_line():
    return [
        [22.4101, 112.9401],
        [22.4110, 112.9410],
        [22.4120, 112.9420],
    ]


def _make_candidates():
    return [
        {
            'template_id': 'lr_cand0001',
            'name': '操场闭环',
            'score': 0.92,
            'coverage': 1.0,
            'reasons': ['围栏内', '2/2 点位'],
            'line': _closed_ring(),
            'closed': True,
            'lap_m': 1250.0,
            'laps': 2,
            'samples': 6,
            'target_m': 2500.0,
            'missed': [],
            'fence_ok': True,
            'detour_m': 0.0,
        },
        {
            'template_id': 'lr_cand0002',
            'name': '北门环线',
            'score': 0.81,
            'coverage': 1.0,
            'reasons': ['围栏内', '2/2 点位'],
            'line': [
                [22.4120, 112.9420],
                [22.4120, 112.9430],
                [22.4130, 112.9430],
                [22.4120, 112.9420],
            ],
            'closed': True,
            'lap_m': 850.0,
            'laps': 3,
            'samples': 4,
            'target_m': 2500.0,
            'missed': [],
            'fence_ok': True,
            'detour_m': 12.0,
        },
    ]


def _make_fake_tools(
    *,
    templates=2,
    status_state='ok',
    learn_error=None,
    candidates=None,
    closed=True,
    llm_choose=None,
    slow_learn_event: threading.Event | None = None,
    slow_learn_entered: threading.Event | None = None,
):
    calls = []
    cands = _make_candidates() if candidates is None else candidates

    def route_memory_status():
        time.sleep(0.01)
        calls.append(('status', {}))
        return _ok({
            'summary': {
                'templates': templates,
                'usable': 6 if templates else 0,
                'seen': 8,
                'status': status_state if templates else 'empty',
            },
            'templates': [{'id': c['template_id'], 'name': c['name']} for c in cands[:templates]],
        })

    def route_memory_learn(limit=50, force=False):
        if slow_learn_entered is not None:
            slow_learn_entered.set()
        if slow_learn_event is not None:
            slow_learn_event.wait(timeout=3.0)
        else:
            time.sleep(0.01)
        calls.append(('learn', {'limit': limit, 'force': force}))
        if learn_error:
            return _fail(learn_error)
        return _ok({
            'summary': {
                'templates': templates,
                'usable': 6 if templates else 0,
                'seen': 8,
                'status': status_state if templates else 'empty',
            },
            'templates': [{'id': c['template_id'], 'name': c['name']} for c in cands[:templates]],
            'cached': False,
        })

    def route_memory_suggest(km=2.5, points=None, fence=None, run_area_id=None):
        time.sleep(0.01)
        calls.append(('suggest', {'km': km, 'points': points, 'fence': fence, 'run_area_id': run_area_id}))
        return _ok({
            'candidates': cands,
            'templates': templates,
            'target_m': round(float(km) * 1000.0, 1),
            'status': status_state,
        })

    def route_memory_apply(template_id, km=2.5, points=None):
        time.sleep(0.01)
        calls.append(('apply', {'template_id': template_id, 'km': km, 'points': points}))
        wps = _closed_ring() if closed else _open_line()
        if template_id == 'lr_cand0002':
            wps = cands[1]['line']
        return _ok({
            'ok': True,
            'waypoints': wps,
            'closed': closed,
            'lap_m': 1250.0,
            'laps': 2,
            'target_m': round(float(km) * 1000.0, 1),
            'missed': [],
            'fence_ok': True,
        })

    def preview_route(**kwargs):
        time.sleep(0.01)
        calls.append(('preview', kwargs))
        return _ok({
            'model': {'verdict': 'pass', 'configured': True, 'checks': []},
            'run': {'km': kwargs.get('km', 2.5), 'points': len(kwargs.get('waypoints') or [])},
        })

    def audit_run(*args, **kwargs):
        time.sleep(0.01)
        calls.append(('audit', {'args': args, 'kwargs': kwargs}))
        return _ok({
            'summary': {'ok': True, 'passed': 10, 'failed': 0, 'warned': 0},
            'checks': [],
        })

    tools = {
        'route_memory_status': route_memory_status,
        'route_memory_learn': route_memory_learn,
        'route_memory_suggest': route_memory_suggest,
        'route_memory_apply': route_memory_apply,
        'preview_route': preview_route,
        'audit_run': audit_run,
    }
    if llm_choose is not None:
        tools['llm_choose'] = llm_choose
    return tools, calls


def _wait_terminal(session: AgentSession, timeout: float = 4.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = session.status()
        if st['state'] in ('wait_confirm', 'confirmed', 'cancelled', 'error'):
            return st
        time.sleep(0.01)
    raise AssertionError(f'session did not finish in time: {session.status()}')


def test_happy_path_and_audit_no_args(tmp_path):
    tools, calls = _make_fake_tools(templates=0)

    # First status has 0 templates, learn produces 2 templates
    learn_calls = []

    def learn_with_templates(limit=50, force=False):
        time.sleep(0.01)
        learn_calls.append((limit, force))
        calls.append(('learn', {'limit': limit, 'force': force}))
        return _ok({
            'summary': {'templates': 2, 'usable': 6, 'seen': 8, 'status': 'ok'},
            'templates': [{'id': 'lr_cand0001'}, {'id': 'lr_cand0002'}],
            'cached': False,
        })

    tools['route_memory_learn'] = learn_with_templates
    session = AgentSession(tools, str(tmp_path), '1001:2002')
    pts = [{'lat': 22.4105, 'lng': 112.9405, 'isMustPoint': True, 'name': 'P1'}]

    initial = session.start(text='帮我跑 2.5 公里', points=pts, run_area_id=47203)
    assert initial['session_id'].startswith('ag_')
    assert initial['state'] in STATES

    st = _wait_terminal(session)
    assert st['state'] == 'wait_confirm'
    assert st['goal'] == {'km': 2.5, 'points': 1, 'run_area_id': 47203}
    assert st['selected'] == 'lr_cand0001'
    assert st['llm'] == {'used': False, 'model': '', 'error': ''}
    assert st['preview']['model']['verdict'] == 'pass'
    assert st['audit']['summary']['ok'] is True

    # audit_run MUST be called with no arguments
    audit_calls = [c for c in calls if c[0] == 'audit']
    assert len(audit_calls) == 1
    assert audit_calls[0][1] == {'args': (), 'kwargs': {}}

    # log contains expected tools
    logged_tools = {entry['tool'] for entry in st['log']}
    for expected in (
        'collect',
        'route_memory_status',
        'route_memory_learn',
        'route_memory_suggest',
        'route_memory_apply',
        'preview_route',
        'audit_run',
        'wait_confirm',
    ):
        assert expected in logged_tools


def test_confirm_returns_preview_args_and_never_calls_submit(tmp_path):
    tools, _ = _make_fake_tools(templates=2)
    assert 'submit' not in tools
    assert 'real_submit_run' not in tools

    session = AgentSession(tools, str(tmp_path), '1001:2002')
    pts = [{'lat': 22.4105, 'lng': 112.9405, 'must': True, 'n': '东门'}]
    session.start(km=2.5, points=pts)
    st = _wait_terminal(session)
    assert st['state'] == 'wait_confirm'

    confirmed = session.confirm()
    assert confirmed['state'] == 'confirmed'
    assert 'submit' not in confirmed
    assert 'submit' not in confirmed['preview_args']
    pa = confirmed['preview_args']
    assert pa['km'] == 2.5
    assert pa['pace'] == 6.0
    assert pa['cadence'] == 150
    assert pa['closed'] is True
    assert pa['waypoints'] == confirmed['route']
    assert len(pa['checkpoints']) == 1
    assert pa['checkpoints'][0]['lon'] == 112.9405
    assert session.status()['state'] == 'confirmed'


def test_no_templates_returns_error_with_fallback_auto(tmp_path):
    tools, _ = _make_fake_tools(templates=0, status_state='empty')
    session = AgentSession(tools, str(tmp_path), '1001:2002')
    session.start('帮我跑 2.5 公里')
    st = _wait_terminal(session)
    assert st['state'] == 'error'
    assert st['fallback'] == 'auto'
    assert st['error']


def test_weak_summary_returns_error_with_fallback_auto(tmp_path):
    tools, _ = _make_fake_tools(templates=1, status_state='weak')
    session = AgentSession(tools, str(tmp_path), '1001:2002')
    session.start('帮我跑 2.5 公里')
    st = _wait_terminal(session)
    assert st['state'] == 'error'
    assert st['fallback'] == 'auto'
    assert st['error']


def test_learn_failure_token_error_redacts_and_prompts_relogin(tmp_path):
    secret_tok = 'tok-secret-99887766'
    tools, _ = _make_fake_tools(
        templates=0,
        learn_error=f'Unauthorized token={secret_tok} 缓存中token不存在',
    )
    session = AgentSession(tools, str(tmp_path), 'uid_secret_42:47203')
    session.start('帮我跑 2.5 公里')
    st = _wait_terminal(session)
    assert st['state'] == 'error'
    assert 'token' in st['error'].lower()
    assert '重新登录' in st['error']
    assert secret_tok not in st['error']


def test_rotate_candidate_and_select_nth_rerun_preview_and_audit(tmp_path):
    tools, calls = _make_fake_tools(templates=2)
    session = AgentSession(tools, str(tmp_path), '1001:2002')
    session.start('帮我跑 2.5 公里')
    st = _wait_terminal(session)
    assert st['selected'] == 'lr_cand0001'

    res = session.message('换一条')
    assert res['state'] == 'wait_confirm'
    assert res['selected'] == 'lr_cand0002'
    apply_ids = [c[1]['template_id'] for c in calls if c[0] == 'apply']
    assert apply_ids == ['lr_cand0001', 'lr_cand0002']
    assert sum(1 for c in calls if c[0] == 'preview') == 2
    assert sum(1 for c in calls if c[0] == 'audit') == 2

    res2 = session.message('用第1条')
    assert res2['state'] == 'wait_confirm'
    assert res2['selected'] == 'lr_cand0001'
    assert sum(1 for c in calls if c[0] == 'preview') == 3


def test_reverse_closed_route_and_refuse_open_route(tmp_path):
    tools, calls = _make_fake_tools(templates=2, closed=True)
    session = AgentSession(tools, str(tmp_path), '1001:2002')
    session.start('帮我跑 2.5 公里')
    _wait_terminal(session)

    res = session.message('反向')
    assert res['state'] == 'wait_confirm'
    previews = [c[1] for c in calls if c[0] == 'preview']
    assert len(previews) == 2
    orig_wps = previews[0]['waypoints']
    rev_wps = previews[1]['waypoints']
    assert previews[1]['closed'] is True
    assert rev_wps[0] == rev_wps[-1]
    assert rev_wps[:-1] == list(reversed(orig_wps[:-1]))

    # Open route must never be reversed
    tools_open, calls_open = _make_fake_tools(templates=2, closed=False)
    s_open = AgentSession(tools_open, str(tmp_path), '1001:2002')
    s_open.start('帮我跑 2.5 公里')
    _wait_terminal(s_open)
    res_open = s_open.message('反向')
    assert res_open['state'] == 'wait_confirm'
    assert sum(1 for c in calls_open if c[0] == 'preview') == 1
    assert any('开放路线' in entry['text'] or '闭环' in entry['text'] for entry in res_open['log'])


def test_relearn_calls_learn_with_force_true(tmp_path):
    tools, calls = _make_fake_tools(templates=2)
    session = AgentSession(tools, str(tmp_path), '1001:2002')
    session.start('帮我跑 2.5 公里')
    _wait_terminal(session)

    assert not any(c[0] == 'learn' for c in calls)
    res = session.message('重新学习')
    assert res['state'] == 'wait_confirm'
    learn_calls = [c[1] for c in calls if c[0] == 'learn']
    assert len(learn_calls) == 1
    assert learn_calls[0]['force'] is True


def test_chinese_distance_parsing_and_message_update():
    assert parse_distance_km('帮我跑 2.5 公里') == 2.5
    assert parse_distance_km('2.5 km') == 2.5
    assert parse_distance_km('跑三公里') == 3.0
    assert parse_distance_km('五公里') == 5.0
    assert parse_distance_km('二点五公里') == 2.5
    assert parse_distance_km('两公里半') == 2.5
    assert parse_distance_km('半公里') == 0.5
    assert parse_distance_km('换一条') is None
    assert parse_distance_km('用第2条') is None
    assert parse_distance_km('半马') is None
    assert parse_distance_km('随便聊聊') is None


def test_distance_change_and_unknown_message(tmp_path):
    tools, calls = _make_fake_tools(templates=2)
    session = AgentSession(tools, str(tmp_path), '1001:2002')
    session.start('跑三公里')
    st = _wait_terminal(session)
    assert st['goal']['km'] == 3.0

    res_unknown = session.message('今天天气怎么样')
    assert res_unknown['state'] == 'wait_confirm'
    assert any('想跑多少公里' in e['text'] for e in res_unknown['log'])

    res_5k = session.message('五公里')
    assert res_5k['state'] == 'wait_confirm'
    assert res_5k['goal']['km'] == 5.0
    assert [c[1]['km'] for c in calls if c[0] == 'suggest'] == [3.0, 5.0]


def test_invalid_state_transitions_and_transient_guard(tmp_path):
    tools, _ = _make_fake_tools(templates=2)
    session = AgentSession(tools, str(tmp_path), '1001:2002')

    # confirm in idle returns error dict, does not throw
    res = session.confirm()
    assert res['ok'] is False
    assert res['error']
    assert session.status()['state'] == 'idle'

    # message during slow learn must return "还在处理上一条" and not start second pipeline
    entered = threading.Event()
    release = threading.Event()
    slow_tools, slow_calls = _make_fake_tools(
        templates=0,
        slow_learn_event=release,
        slow_learn_entered=entered,
    )
    slow_tools['route_memory_learn'] = lambda limit=50, force=False: (
        entered.set(),
        release.wait(timeout=3.0),
        slow_calls.append(('learn', {'limit': limit, 'force': force})),
        _ok({'summary': {'templates': 2, 'usable': 6, 'seen': 8, 'status': 'ok'}, 'templates': []}),
    )[-1]

    s2 = AgentSession(slow_tools, str(tmp_path), '1001:2002')
    s2.start('帮我跑 2.5 公里')
    assert entered.wait(timeout=2.0)

    mid = s2.message('换一条')
    assert mid['state'] == 'learn'
    assert any('还在处理上一条' in e['text'] for e in mid['log'])

    bad_confirm = s2.confirm()
    assert bad_confirm['ok'] is False

    release.set()
    _wait_terminal(s2)


def test_cancel_during_slow_learn_and_generation_guard(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    tools, _ = _make_fake_tools(templates=0)

    def slow_learn(limit=50, force=False):
        entered.set()
        release.wait(timeout=3.0)
        return _ok({
            'summary': {'templates': 2, 'usable': 6, 'seen': 8, 'status': 'ok'},
            'templates': [{'id': 'lr_cand0001'}],
        })

    tools['route_memory_learn'] = slow_learn
    session = AgentSession(tools, str(tmp_path), '1001:2002')
    session.start('帮我跑 2.5 公里')
    assert entered.wait(timeout=2.0)

    cancelled = session.cancel()
    assert cancelled['state'] == 'cancelled'
    assert cancelled['candidates'] == []

    release.set()
    time.sleep(0.05)
    # Old thread must not overwrite cancelled state
    assert session.status()['state'] == 'cancelled'
    assert session.status()['candidates'] == []

    # Generation counter: new start supersedes slow old start
    entered2 = threading.Event()
    release2 = threading.Event()

    def slow_then_fast(limit=50, force=False):
        if not entered2.is_set():
            entered2.set()
            release2.wait(timeout=3.0)
            return _fail('old failure')
        return _ok({
            'summary': {'templates': 2, 'usable': 6, 'seen': 8, 'status': 'ok'},
            'templates': [{'id': 'lr_cand0001'}],
        })

    tools['route_memory_learn'] = slow_then_fast
    session2 = AgentSession(tools, str(tmp_path), '1001:2002')
    session2.start('帮我跑 2.5 公里')
    assert entered2.wait(timeout=2.0)
    session2.start('帮我跑 4 公里')
    st_new = _wait_terminal(session2)
    assert st_new['state'] == 'wait_confirm'
    assert st_new['goal']['km'] == 4.0

    release2.set()
    time.sleep(0.05)
    assert session2.status()['state'] == 'wait_confirm'
    assert session2.status()['goal']['km'] == 4.0


def test_trace_file_redacts_secrets_uid_and_coordinates(tmp_path):
    secret_uid = 'uid_secret_8899'
    secret_tok = 'tok-super-secret-xyz'
    tools, _ = _make_fake_tools(templates=2)

    def leaky_llm(candidates, target_m):
        raise RuntimeError(f'auth failed uid={secret_uid} token={secret_tok} at 22.410123,112.940567')

    tools['llm_choose'] = leaky_llm
    session = AgentSession(tools, str(tmp_path), f'{secret_uid}:47203')
    session.start(
        text='帮我跑 2.5 公里',
        points=[{'lat': 22.410512, 'lng': 112.940534, 'name': 'secret-point'}],
    )
    st = _wait_terminal(session)
    assert st['state'] == 'wait_confirm'

    trace_path = os.path.join(str(tmp_path), 'agent_runs', f"{st['session_id']}.jsonl")
    assert os.path.isfile(trace_path)
    raw = open(trace_path, encoding='utf-8').read()
    lines = [json.loads(line) for line in raw.splitlines() if line.strip()]
    assert len(lines) >= 5
    for row in lines:
        assert {'at_ms', 'state', 'tool', 'ok', 'detail'}.issubset(row.keys())

    assert secret_tok not in raw
    assert secret_uid not in raw
    assert not re.search(r'\d{2,3}\.\d{4,}', raw)
    assert '你是跑步路线选择助手' not in raw


def test_llm_choice_success_invalid_and_failure_fallback(tmp_path):
    # Failure falls back to top 1 and records llm.used=False + error
    tools_err, _ = _make_fake_tools(
        templates=2,
        llm_choose=lambda cands, target_m: (_ for _ in ()).throw(RuntimeError('429 rate limit')),
    )
    s_err = AgentSession(tools_err, str(tmp_path), '1001:2002')
    s_err.start('帮我跑 2.5 公里')
    st_err = _wait_terminal(s_err)
    assert st_err['state'] == 'wait_confirm'
    assert st_err['selected'] == 'lr_cand0001'
    assert st_err['llm']['used'] is False
    assert '429' in st_err['llm']['error']

    # Unknown id falls back to top 1
    tools_bad_id, _ = _make_fake_tools(templates=2, llm_choose=lambda c, m: 'lr_unknown')
    s_bad = AgentSession(tools_bad_id, str(tmp_path), '1001:2002')
    s_bad.start('帮我跑 2.5 公里')
    st_bad = _wait_terminal(s_bad)
    assert st_bad['selected'] == 'lr_cand0001'
    assert st_bad['llm']['used'] is False
    assert st_bad['llm']['error']

    # Valid choice selects candidate 2
    tools_ok, _ = _make_fake_tools(templates=2, llm_choose=lambda c, m: 'lr_cand0002')
    s_ok = AgentSession(tools_ok, str(tmp_path), '1001:2002')
    s_ok.start('帮我跑 2.5 公里')
    st_ok = _wait_terminal(s_ok)
    assert st_ok['selected'] == 'lr_cand0002'
    assert st_ok['llm']['used'] is True


def test_synchronous_run_pipeline_helper(tmp_path, monkeypatch):
    class DummyThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self.target = target
            self.args = args

        def start(self):
            pass

    monkeypatch.setattr('replica.application.agent.threading.Thread', DummyThread)
    tools, _ = _make_fake_tools(templates=2)
    session = AgentSession(tools, str(tmp_path), '1001:2002')
    st0 = session.start('帮我跑 2.5 公里')
    assert st0['state'] == 'collect'

    session._run_pipeline()
    st1 = session.status()
    assert st1['state'] == 'wait_confirm'


def test_bridge_agent_methods_without_submit(tmp_path, monkeypatch):
    api = ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'', str(tmp_path))

    # Before start: status idle, cancel idle, message/confirm fail
    idle = api.real_agent_status()
    assert idle['code'] == 200
    assert idle['data']['state'] == 'idle'
    assert api.real_agent_cancel()['data']['state'] == 'idle'
    assert api.real_agent_message('换一条')['code'] != 200
    assert api.real_agent_confirm()['code'] != 200

    # Mock the 6 underlying bridge methods on api
    cands = _make_candidates()
    monkeypatch.setattr(api, 'real_route_memory_status', lambda: _ok({
        'summary': {'templates': 2, 'usable': 6, 'seen': 8, 'status': 'ok'},
        'templates': [{'id': 'lr_cand0001'}, {'id': 'lr_cand0002'}],
    }))
    monkeypatch.setattr(api, 'real_route_memory_learn', lambda limit=50, force=False: _ok({
        'summary': {'templates': 2, 'usable': 6, 'seen': 8, 'status': 'ok'},
        'templates': [{'id': 'lr_cand0001'}, {'id': 'lr_cand0002'}],
    }))
    monkeypatch.setattr(api, 'real_route_memory_suggest', lambda **kw: _ok({
        'candidates': cands,
        'templates': 2,
        'target_m': 2500.0,
        'status': 'ok',
    }))
    monkeypatch.setattr(api, 'real_route_memory_apply', lambda tid, km=2.5, points=None: _ok({
        'ok': True,
        'waypoints': _closed_ring(),
        'closed': True,
        'lap_m': 1250.0,
        'laps': 2,
        'target_m': 2500.0,
        'missed': [],
        'fence_ok': True,
    }))
    monkeypatch.setattr(api, 'real_preview_model', lambda **kw: _ok({
        'model': {'verdict': 'pass'},
        'run': {'km': kw.get('km', 2.5)},
    }))
    monkeypatch.setattr(api, 'real_audit_run', lambda: _ok({
        'summary': {'ok': True, 'passed': 5, 'failed': 0, 'warned': 0},
    }))
    monkeypatch.setattr(
        api,
        'real_submit_run',
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError('submit must never be called')),
    )

    # Mock LLM enabled (ollama needs no DPAPI key) so llm_choose closure runs
    llm_settings.save(str(tmp_path), {
        'enabled': True,
        'protocol': 'ollama',
        'base_url': 'http://127.0.0.1:11434/v1',
        'model': 'qwen2.5:7b',
    })
    monkeypatch.setattr(
        llm_client,
        'chat',
        lambda settings, messages, proxies=None: {
            'ok': True,
            'text': '2',
            'error': '',
            'latency_ms': 15,
            'status': 200,
            'error_kind': '',
        },
    )

    started = api.real_agent_start(text='帮我跑 2.5 公里')
    assert started['code'] == 200

    deadline = time.time() + 4.0
    st = None
    while time.time() < deadline:
        st = api.real_agent_status()['data']
        if st['state'] in ('wait_confirm', 'error', 'cancelled'):
            break
        time.sleep(0.01)

    assert st is not None
    assert st['state'] == 'wait_confirm'
    assert st['selected'] == 'lr_cand0002'
    assert st['llm']['used'] is True
    assert st['llm']['model'] == 'qwen2.5:7b'

    msg_res = api.real_agent_message('换一条')
    assert msg_res['code'] == 200
    assert msg_res['data']['selected'] == 'lr_cand0001'

    conf_res = api.real_agent_confirm()
    assert conf_res['code'] == 200
    assert conf_res['data']['preview_args']['km'] == 2.5
    assert api.real_agent_status()['data']['state'] == 'confirmed'


def test_agent_status_hides_session_after_account_switch(tmp_path, monkeypatch):
    api = ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'', str(tmp_path))
    session_doc = {'uid': '1001', 'unid': '2002'}
    monkeypatch.setattr(api, '_read_session', lambda: dict(session_doc))
    monkeypatch.setattr(api, 'real_route_memory_status', lambda: _ok({
        'summary': {'templates': 0, 'status': 'empty'},
        'templates': [],
    }))
    started = api.real_agent_start(text='2.5')
    assert started['code'] == 200
    first_id = started['data']['session_id']
    assert api.real_agent_status()['data']['session_id'] == first_id

    session_doc['uid'] = '9999'
    idle = api.real_agent_status()['data']
    assert idle['state'] == 'idle'
    assert idle['session_id'] == ''
    assert api.real_agent_message('换一条')['code'] != 200


def test_message_after_cancel_or_confirm_is_rejected(tmp_path):
    tools, calls = _make_fake_tools(templates=2)
    session = AgentSession(tools, str(tmp_path), '1001:2002')
    session.start('帮我跑 2.5 公里')
    st = _wait_terminal(session)
    assert st['state'] == 'wait_confirm'

    session.cancel()
    res = session.message('五公里')
    assert res['state'] == 'cancelled'
    assert res['goal']['km'] == 2.5
    assert any('会话已结束' in e['text'] for e in res['log'])
    assert [c[1]['km'] for c in calls if c[0] == 'suggest'] == [2.5]

    tools2, _ = _make_fake_tools(templates=2)
    session2 = AgentSession(tools2, str(tmp_path), '1001:2002')
    session2.start('帮我跑 2.5 公里')
    _wait_terminal(session2)
    assert session2.confirm()['ok'] is True
    res2 = session2.message('五公里')
    assert res2['state'] == 'confirmed'
    assert res2['goal']['km'] == 2.5


def test_failed_distance_change_clears_old_preview(tmp_path):
    tools, _ = _make_fake_tools(templates=2)
    session = AgentSession(tools, str(tmp_path), '1001:2002')
    session.start('帮我跑 2.5 公里')
    st = _wait_terminal(session)
    assert st['state'] == 'wait_confirm'
    assert st['preview'] and st['audit']

    session._tools['route_memory_suggest'] = lambda **kw: _ok({
        'candidates': [], 'status': 'ok', 'message': '没有匹配路线',
    })
    res = session.message('19公里')
    assert res['state'] == 'error'
    assert res['preview'] is None
    assert res['audit'] is None
    conf = session.confirm()
    assert conf['ok'] is False


def test_confirm_refuses_when_local_audit_failed(tmp_path):
    tools, _ = _make_fake_tools(templates=2)
    tools['audit_run'] = lambda *a, **kw: _ok({
        'summary': {'ok': False, 'passed': 9, 'failed': 1, 'warned': 0},
        'checks': [],
    })
    session = AgentSession(tools, str(tmp_path), '1001:2002')
    session.start('帮我跑 2.5 公里')
    st = _wait_terminal(session)
    assert st['state'] == 'wait_confirm'
    res = session.confirm()
    assert res['ok'] is False
    assert '审计' in res['error']
    assert session.status()['state'] == 'wait_confirm'


def test_parse_bare_distance_and_half():
    assert parse_distance_km('3') == 3.0
    assert parse_distance_km('2.5') == 2.5
    assert parse_distance_km('半') == 0.5
    assert parse_distance_km('半马') is None
    # CJK characters are word characters, so a \b after "km" would miss these.
    assert parse_distance_km('3km路线') == 3.0
    assert parse_distance_km('500m跑') == 0.5
    # Sentence punctuation after the unit is common in chat input.
    assert parse_distance_km('3km.') == 3.0
    assert parse_distance_km('3 km。') == 3.0
    assert parse_distance_km('500m.') == 0.5


def test_intent_negation_and_distance_priority(tmp_path):
    tools, calls = _make_fake_tools(templates=2)
    session = AgentSession(tools, str(tmp_path), '1001:2002')
    session.start('帮我跑 2.5 公里')
    st = _wait_terminal(session)
    assert st['state'] == 'wait_confirm'
    previews_before = sum(1 for c in calls if c[0] == 'preview')

    # Negated intents must not cancel or reverse the route.
    res = session.message('不要取消')
    assert res['state'] == 'wait_confirm'
    res = session.message('不要再取消')
    assert res['state'] == 'wait_confirm'
    res = session.message('别反向')
    assert res['state'] == 'wait_confirm'
    assert sum(1 for c in calls if c[0] == 'preview') == previews_before

    # "算了，跑3公里" is a new target, not a cancellation.
    res = session.message('算了，跑3公里')
    assert res['state'] == 'wait_confirm'
    assert res['goal']['km'] == 3.0

    # "打算了解" contains the substring "算了" but is not a cancel filler.
    res = session.message('打算了解3公里')
    assert res['state'] == 'wait_confirm'
    assert res['goal']['km'] == 3.0

    # A new distance is the primary intent even when "换一条" is present.
    res = session.message('换一条3公里')
    assert res['state'] == 'wait_confirm'
    assert res['goal']['km'] == 3.0
    assert res['error'] == ''

    # A short "取消3公里" still means cancel.
    res = session.message('取消3公里')
    assert res['state'] == 'cancelled'

    # The standalone filler still cancels.
    tools2, _ = _make_fake_tools(templates=2)
    session2 = AgentSession(tools2, str(tmp_path), '1001:2002')
    session2.start('帮我跑 2.5 公里')
    _wait_terminal(session2)
    assert session2.message('算了')['state'] == 'cancelled'


def test_distance_and_select_nth_in_one_message(tmp_path):
    tools, calls = _make_fake_tools(templates=2)
    session = AgentSession(tools, str(tmp_path), '1001:2002')
    session.start('帮我跑 2.5 公里')
    _wait_terminal(session)

    res = session.message('跑3公里，用第2条')
    assert res['state'] == 'wait_confirm'
    assert res['goal']['km'] == 3.0
    assert res['selected'] == 'lr_cand0002'
    assert any('第 2 条' in entry['text'] for entry in res['log'])


def test_auto_rotates_to_next_candidate_when_audit_fails(tmp_path):
    tools, calls = _make_fake_tools(templates=2)
    audit_calls = {'n': 0}

    def audit_first_fails(*args, **kwargs):
        audit_calls['n'] += 1
        calls.append(('audit', {'args': args, 'kwargs': kwargs}))
        if audit_calls['n'] == 1:
            return _ok({
                'summary': {'ok': False, 'passed': 9, 'failed': 1, 'warned': 0},
                'checks': [],
            })
        return _ok({
            'summary': {'ok': True, 'passed': 10, 'failed': 0, 'warned': 0},
            'checks': [],
        })

    tools['audit_run'] = audit_first_fails
    session = AgentSession(tools, str(tmp_path), '1001:2002')
    session.start('帮我跑 2.5 公里')
    st = _wait_terminal(session)
    assert st['state'] == 'wait_confirm'
    assert st['selected'] == 'lr_cand0002'
    assert st['audit']['summary']['ok'] is True
    apply_ids = [c[1]['template_id'] for c in calls if c[0] == 'apply']
    assert apply_ids == ['lr_cand0001', 'lr_cand0002']
    assert any('自动尝试下一条' in entry['text'] for entry in st['log'])
    assert st['stats']['candidates_tried'] == 2


def test_missing_audit_summary_is_an_error_not_a_pass(tmp_path):
    tools, _ = _make_fake_tools(templates=2)
    tools['audit_run'] = lambda *a, **kw: _ok({})
    session = AgentSession(tools, str(tmp_path), '1001:2002')
    session.start('帮我跑 2.5 公里')
    st = _wait_terminal(session)
    assert st['state'] == 'error'
    assert '格式异常' in st['error']
    conf = session.confirm()
    assert conf['ok'] is False


def test_transient_tool_error_retries_once(tmp_path):
    tools, calls = _make_fake_tools(templates=2)
    original = tools['route_memory_status']
    state = {'n': 0}

    def flaky_status():
        state['n'] += 1
        if state['n'] == 1:
            calls.append(('status', {}))
            return {'code': 500, 'message': '请求超时', 'data': None}
        return original()

    tools['route_memory_status'] = flaky_status
    session = AgentSession(tools, str(tmp_path), '1001:2002')
    session.start('帮我跑 2.5 公里')
    st = _wait_terminal(session)
    assert st['state'] == 'wait_confirm'
    assert state['n'] == 2
    assert st['stats']['tool_retries'] == 1
    assert any('自动重试' in entry['text'] for entry in st['log'])


def test_invalid_suggest_payload_is_an_error(tmp_path):
    tools, _ = _make_fake_tools(templates=2)
    tools['route_memory_suggest'] = lambda **kw: _ok({
        'candidates': {'not': 'a list'},
        'status': 'ok',
    })
    session = AgentSession(tools, str(tmp_path), '1001:2002')
    session.start('帮我跑 2.5 公里')
    st = _wait_terminal(session)
    assert st['state'] == 'error'
    assert '候选列表' in st['error'] or '格式异常' in st['error']


def test_llm_choice_that_is_far_below_top_score_is_rejected(tmp_path):
    cands = _make_candidates()
    cands[1]['score'] = 0.50
    tools, _ = _make_fake_tools(
        templates=2,
        candidates=cands,
        llm_choose=lambda c, m: 'lr_cand0002',
    )
    session = AgentSession(tools, str(tmp_path), '1001:2002')
    session.start('帮我跑 2.5 公里')
    st = _wait_terminal(session)
    assert st['state'] == 'wait_confirm'
    assert st['selected'] == 'lr_cand0001'
    assert st['llm']['used'] is False
    assert '差距' in st['llm']['error']


def test_explicit_select_is_not_silently_rotated(tmp_path):
    tools, calls = _make_fake_tools(templates=2)
    audit_calls = {'n': 0}

    def audit_second_fails(*args, **kwargs):
        audit_calls['n'] += 1
        calls.append(('audit', {'args': args, 'kwargs': kwargs}))
        ok = audit_calls['n'] == 1
        return _ok({
            'summary': {
                'ok': ok,
                'passed': 10 if ok else 9,
                'failed': 0 if ok else 1,
                'warned': 0,
            },
            'checks': [],
        })

    tools['audit_run'] = audit_second_fails
    session = AgentSession(tools, str(tmp_path), '1001:2002')
    session.start('帮我跑 2.5 公里')
    _wait_terminal(session)

    res = session.message('用第2条')
    assert res['state'] == 'wait_confirm'
    assert res['selected'] == 'lr_cand0002'
    assert res['audit']['summary']['ok'] is False
    assert any('未自动替换' in entry['text'] for entry in res['log'])
    apply_ids = [c[1]['template_id'] for c in calls if c[0] == 'apply']
    assert apply_ids == ['lr_cand0001', 'lr_cand0002']
    assert session.confirm()['ok'] is False


def test_agent_stats_count_tool_and_llm_cache_hits(tmp_path):
    tools, _ = _make_fake_tools(templates=2)
    real_suggest = tools['route_memory_suggest']
    real_apply = tools['route_memory_apply']

    def cached_suggest(**kwargs):
        res = real_suggest(**kwargs)
        res['data']['cached'] = True
        return res

    def uncached_apply(template_id, km=2.5, points=None):
        res = real_apply(template_id, km=km, points=points)
        res['data']['cached'] = False
        return res

    tools['route_memory_suggest'] = cached_suggest
    tools['route_memory_apply'] = uncached_apply
    tools['llm_choose'] = lambda cands, target_m: {
        'template_id': 'lr_cand0001',
        'model': 'fake-model',
        'error': '',
        'cached': True,
    }

    session = AgentSession(tools, str(tmp_path), '1001:2002')
    session.start('帮我跑 2.5 公里')
    st = _wait_terminal(session)
    assert st['state'] == 'wait_confirm'
    assert st['stats']['cache_hits'] == 1
    assert st['stats']['cache_misses'] == 1
    assert st['stats']['llm_cache_hits'] == 1
    assert st['stats']['llm_calls'] == 0
    assert st['stats']['llm_cache_misses'] == 0


def test_llm_choice_cache_returns_the_same_decision_without_a_second_call(
        tmp_path, monkeypatch):
    api = ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'', str(tmp_path))
    llm_settings.save(str(tmp_path), {
        'enabled': True,
        'protocol': 'ollama',
        'base_url': 'http://127.0.0.1:11434/v1',
        'model': 'qwen2.5:7b',
    })
    calls = {'chat': 0}

    def fake_chat(settings, messages, proxies=None):
        calls['chat'] += 1
        return {
            'ok': True,
            'text': '2',
            'error': '',
            'latency_ms': 12,
            'status': 200,
            'error_kind': '',
        }

    monkeypatch.setattr(llm_client, 'chat', fake_chat)
    tools = api._agent_build_tools()
    cands = _make_candidates()

    first = tools['llm_choose'](cands, 2500.0)
    second = tools['llm_choose'](cands, 2500.0)
    assert first['template_id'] == 'lr_cand0002'
    assert first['cached'] is False
    assert second['template_id'] == 'lr_cand0002'
    assert second['cached'] is True
    assert calls['chat'] == 1
    assert first['prompt_sha256'] == second['prompt_sha256']

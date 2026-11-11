"""Static guards for the Agent route-planning page."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / 'frontend'
HTML = (FRONTEND / 'index.html').read_text(encoding='utf-8')
JS = (FRONTEND / 'js' / 'app.js').read_text(encoding='utf-8')
CSS = (FRONTEND / 'css' / 'app.css').read_text(encoding='utf-8')


def _js_function(name):
    m = re.search(r'(?:async\s+)?function\s+' + re.escape(name) + r'\s*\(', JS)
    assert m, f'missing function {name}'
    start = m.start()
    depth = 0
    for i in range(JS.index('{', start), len(JS)):
        if JS[i] == '{':
            depth += 1
        elif JS[i] == '}':
            depth -= 1
            if depth == 0:
                return JS[start:i + 1]
    raise AssertionError('unterminated function ' + name)


def test_agent_nav_position_and_page_order():
    nav = HTML.split('<nav class="nav">', 1)[1].split('</nav>', 1)[0]
    run_pos = nav.index('data-page="run"')
    agent_pos = nav.index('data-page="agent"')
    routes_pos = nav.index('data-page="routes"')
    assert run_pos < agent_pos < routes_pos

    agent_nav = re.search(
        r'<button\b[^>]*data-page="agent"[^>]*>.*?</button>', nav, re.S,
    )
    assert agent_nav
    assert '#bi-activity' in agent_nav.group(0)
    assert '>Agent<' in agent_nav.group(0)

    run_sec = HTML.index('id="page-run"')
    agent_sec = HTML.index('id="page-agent"')
    routes_sec = HTML.index('id="page-routes"')
    assert run_sec < agent_sec < routes_sec


def test_agent_page_required_ids_chips_and_defaults():
    page = HTML.split('id="page-agent"', 1)[1].split('id="page-routes"', 1)[0]

    for cid in (
        'agentStateChip', 'agentGoalBar', 'agentGoalKm', 'agentGoalPoints',
        'agentGoalCampus', 'agentLlmChip', 'agentTranscript', 'agentCandidates',
        'agentPreview', 'agentComposer', 'agentInput', 'agentSend',
        'agentConfirm', 'agentCancel', 'agentHint',
    ):
        assert f'id="{cid}"' in page, cid

    transcript = re.search(r'<div\b[^>]*\bid="agentTranscript"[^>]*>', page)
    assert transcript and 'aria-live="polite"' in transcript.group(0)

    confirm_btn = re.search(
        r'<button\b[^>]*\bid="agentConfirm"[^>]*>.*?</button>', page, re.S,
    )
    assert confirm_btn
    assert 'disabled' in confirm_btn.group(0)
    assert '确认，去跑步页预览' in confirm_btn.group(0)

    cancel_btn = re.search(
        r'<button\b[^>]*\bid="agentCancel"[^>]*>.*?</button>', page, re.S,
    )
    assert cancel_btn and '取消' in cancel_btn.group(0)

    for say in (
        '帮我跑 2.5 公里',
        '用我跑过的路线',
        '先学习我的路线',
        '换一条',
        '反向跑',
    ):
        assert f'data-agent-say="{say}"' in page, say

    assert 'data-goto="settings"' in page
    assert '本地规则' in page
    assert 'real_submit' not in page


def test_agent_js_functions_bridge_methods_and_polling():
    assert "/* agent page */" in JS
    for fn in (
        'loadAgentStatus', 'startAgent', 'sendAgentMessage',
        'pollAgentStatus', 'renderAgentStatus', 'confirmAgent',
        'cancelAgent', 'appendAgentBubble', 'renderAgentCandidates',
        'renderAgentPreview',
    ):
        _js_function(fn)

    assert "liveApi('real_agent_status')" in _js_function('loadAgentStatus')
    assert "liveApi('real_agent_start'" in _js_function('startAgent')
    assert "liveApi('real_agent_message'" in _js_function('sendAgentMessage')
    assert "liveApi('real_agent_status')" in _js_function('pollAgentStatus')
    assert "liveApi('real_agent_confirm'" in _js_function('confirmAgent')
    assert "liveApi('real_agent_cancel'" in _js_function('cancelAgent')
    assert 'setInterval(pollAgentStatus, 600)' in _js_function('startAgentPoll')


def test_agent_states_tool_cards_and_confirm_gating():
    agent_block = JS.split('/* agent page */', 1)[1].split(
        'function openRecordDataPreview(', 1,
    )[0]

    for label in (
        '收集中', '学习中', '推荐中', '适配中', '预览中',
        '检查中', '待确认', '已完成', '已取消', '出错',
    ):
        assert label in agent_block, label

    for tool_text in (
        '正在读取跑步记录…',
        '正在学习路线…',
        '正在推荐…',
        '正在生成预览…',
        '正在检查…',
    ):
        assert tool_text in agent_block, tool_text

    render_fn = _js_function('renderAgentStatus')
    assert "state === 'wait_confirm'" in render_fn
    assert 'audit.summary.ok === true' in render_fn
    assert "disabled = !canConfirm" in render_fn

    goal_fn = _js_function('renderAgentGoalBar')
    assert '本地规则' in goal_fn
    assert 'data-goto="settings"' in goal_fn
    assert '配置模型（可选）' in goal_fn

    apply_fn = _js_function('applyAgentRouteResult')
    assert 'setCloseUI(' in apply_fn
    assert '#inKm' in apply_fn
    assert '#inPace' in apply_fn
    assert '#inCad' in apply_fn
    assert 'LIVE.pointsDistance = want' in apply_fn
    assert "scheduleRouteFinalize(300, 'agent')" in apply_fn
    assert "go('run')" in apply_fn
    assert "switchRunTab('route')" in apply_fn

    confirm_fn = _js_function('confirmAgent')
    assert 'applyAgentRouteResult(' in confirm_fn

    # Agent code must never submit a run automatically.
    assert 'real_submit' not in agent_block


def test_agent_routing_empty_reset_wiring_and_css():
    pages_line = re.search(r'const\s+PAGES\s*=\s*\[[^\]]+\]', JS)
    assert pages_line and "'agent'" in pages_line.group(0)

    go_fn = _js_function('go')
    assert "page === 'agent'" in go_fn
    assert 'loadAgentStatus()' in go_fn
    assert '#agentInput' in go_fn
    assert 'stopAgentPoll()' in go_fn
    assert 'startAgent(' not in go_fn

    empty_fn = _js_function('renderEmptyData')
    assert 'stopAgentPoll()' in empty_fn
    assert 'LIVE.agentInit = false' in empty_fn
    assert 'LIVE.agentStatus = null' in empty_fn

    wire_fn = _js_function('featureWire')
    for sel in (
        '#agentSend', '#agentInput', '[data-agent-say]',
        '#agentCandidates', '#agentConfirm', '#agentCancel',
    ):
        assert sel in wire_fn, sel

    for text in (HTML, JS, CSS):
        assert 'AI 生成' not in text

    for cls in (
        '.agent-shell', '.agent-bubble.user', '.agent-bubble.bot',
        '.agent-tool', '.agent-card', '.agent-state', '.agent-composer',
    ):
        assert cls in CSS, cls
    assert '@media (max-width:1100px)' in CSS

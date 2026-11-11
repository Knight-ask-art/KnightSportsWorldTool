"""Static guards for route-memory UI in the route library and auto mode."""
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


def test_route_library_has_route_memory_box_and_learned_filter():
    routes_page = HTML.split('id="page-routes"', 1)[1].split('id="page-records"', 1)[0]
    rm_pos = routes_page.index('id="routeMemoryBox"')
    filter_pos = routes_page.index('class="filter-row"')
    layout_pos = routes_page.index('class="route-layout"')
    assert rm_pos < filter_pos < layout_pos

    for cid in (
        'routeMemoryBox', 'rmStatus', 'rmLearn', 'rmRefresh',
        'rmProgress', 'rmBar', 'rmProgressText', 'rmTemplates', 'rmHint',
    ):
        assert f'id="{cid}"' in routes_page, cid

    assert 'data-route-filter="learned"' in routes_page


def test_run_route_panel_auto_and_reverse_controls():
    rsec = HTML.split('id="rsec-route"', 1)[1].split('id="rsec-params"', 1)[0]
    auto_tab = re.search(r'<button\b[^>]*data-mode="auto"[^>]*>.*?</button>', rsec, re.S)
    assert auto_tab
    assert '开发中' not in auto_tab.group(0)
    assert '自动功能仍在开发中' not in rsec

    cand_pos = rsec.index('id="autoCandidates"')
    metrics_pos = rsec.index('class="route-metrics"')
    assert cand_pos < metrics_pos
    assert 'id="reverseRouteBtn"' in rsec


def test_route_memory_bridge_methods_and_polling():
    load_fn = _js_function('loadRouteMemory')
    assert "liveApi('real_route_memory_status')" in load_fn
    assert 'bridgeMissing(j)' in load_fn
    assert 'box.hidden = true' in load_fn

    learn_fn = _js_function('startRouteMemoryLearn')
    assert "liveApi('real_route_memory_learn', {limit: 50, force: true, wait: false}, 20000)" in learn_fn
    assert 'setInterval(pollRouteMemoryStatus, 800)' in learn_fn

    poll_fn = _js_function('pollRouteMemoryStatus')
    assert "liveApi('real_route_memory_status')" in poll_fn
    assert 'stopRouteMemoryPoll()' in poll_fn
    assert 'loadRouteMemory()' in poll_fn

    apply_fn = _js_function('applyRouteMemoryTemplate')
    assert "liveApi('real_route_memory_apply'" in apply_fn
    assert "scheduleRouteFinalize(300, 'learned')" in apply_fn
    assert "go('run')" in apply_fn

    save_fn = _js_function('saveRouteMemoryTemplate')
    assert "liveApi('real_route_memory_save'" in save_fn
    assert 'normaliseRoutes(data.routes)' in save_fn
    assert 'renderRouteList()' in save_fn


def test_auto_generate_queries_suggest_before_nearest_neighbor_fallback():
    auto_fn = _js_function('autoGenerate')
    suggest_pos = auto_fn.index("liveApi('real_route_memory_suggest'")
    nn_pos = auto_fn.index('left.shift()')
    assert suggest_pos < nn_pos
    assert 'renderAutoCandidates(' in auto_fn
    assert 'points: LIVE.points' in auto_fn
    assert 'fence: LIVE.fences' in auto_fn
    assert 'chosenRunAreaId(LIVE.session)' in auto_fn

    cand_fn = _js_function('renderAutoCandidates')
    assert '用这条' in cand_fn
    assert '换一条' in cand_fn
    assert 'data-cand-index' in cand_fn
    assert 'autoRotateCandidate' in cand_fn

    use_fn = _js_function('applyAutoCandidate')
    assert 'pushRouteHistory(' in use_fn
    assert "scheduleRouteFinalize(300, 'learned')" in use_fn


def test_learned_type_go_wiring_empty_reset_and_copy_guards():
    assert "type === 'learned'" in _js_function('routeTypeLabel')
    assert "'learned'" in _js_function('normaliseRoutes')

    go_fn = _js_function('go')
    assert 'loadRouteMemory()' in go_fn
    assert 'stopRouteMemoryPoll()' in go_fn

    empty_fn = _js_function('renderEmptyData')
    assert 'LIVE.rmLoaded = false' in empty_fn
    assert 'LIVE.rmCandidates = []' in empty_fn
    assert 'hideAutoCandidates()' in empty_fn

    wire_fn = _js_function('featureWire')
    for sel in ('#rmLearn', '#rmRefresh', '#rmTemplates', '#autoCandidates', '#reverseRouteBtn'):
        assert sel in wire_fn, sel

    for text in (HTML, JS, CSS):
        assert 'AI 生成' not in text

    assert '自动还在开发中' not in JS
    for cls in ('.rm-box', '.rm-card', '.auto-candidates', '.auto-candidate'):
        assert cls in CSS, cls


def test_learned_apply_preserves_requested_distance():
    lock_fn = _js_function('lockLearnedTargetKm')
    assert 'distanceManual = true' in lock_fn
    assert "el.value" in lock_fn

    for name in ('applyAutoCandidate', 'applyRouteMemoryTemplate'):
        fn = _js_function(name)
        assert 'lockLearnedTargetKm(' in fn, name
        assert fn.index('lockLearnedTargetKm(') < fn.index('redraw()'), name


def test_route_memory_poll_and_button_recover_after_tab_switch():
    render_fn = _js_function('renderRouteMemory')
    assert "#rmLearn" in render_fn
    assert "learnBtn.disabled = false" in render_fn
    assert "setInterval(pollRouteMemoryStatus, 800)" in render_fn

    start_fn = _js_function('startRouteMemoryLearn')
    assert "d.progress && d.progress.running" in start_fn
    assert "setInterval(pollRouteMemoryStatus, 800)" in start_fn

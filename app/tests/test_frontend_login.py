"""Static guards for the login gate and frontend asset split.

The full interaction test is Playwright-based (mocked bridge), but these checks
make sure a future edit cannot silently remove the gate, re-introduce a
blocking external font request, or break the external CSS/JS links.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / 'frontend'
HTML = (FRONTEND / 'index.html').read_text(encoding='utf-8')
JS = (FRONTEND / 'js' / 'app.js').read_text(encoding='utf-8')
SOURCE = HTML + '\n' + JS


def test_frontend_assets_are_external_and_present():
    assert '<link rel="stylesheet" href="css/app.css">' in HTML
    assert '<script src="js/app.js"></script>' in HTML
    assert '<style>' not in HTML
    assert (FRONTEND / 'css' / 'app.css').is_file()
    assert (FRONTEND / 'js' / 'app.js').is_file()


def test_login_gate_locks_app_until_session_is_ready():
    assert '<body class="login-locked">' in HTML
    assert 'id="loginGate"' in HTML
    assert 'id="loginForm"' in HTML
    assert 'id="loginUser"' in HTML
    assert 'id="loginPass"' in HTML
    assert 'id="loginBtn"' in HTML
    assert 'id="appShell" aria-hidden="true" inert' in HTML


def test_login_gate_uses_the_real_bridge_and_auto_keepalive():
    assert "liveApi('real_login'" in SOURCE
    assert 'async function ensureKeepalive()' in SOURCE
    # “随登随退”: local logout does not wait for the server proof.
    assert "liveApi('real_logout', {local_only: true})" in SOURCE
    assert "showLoginGate(verified ? '已退出登录" in SOURCE
    assert '已退出本机登录，请重新登录。' in SOURCE


def test_partial_school_rules_and_issue_records_are_visible():
    assert '留空 = 学校没有这项要求' in SOURCE
    assert 'data-rec-filter="issue"' in SOURCE
    assert 'id="netProxy"' in SOURCE
    assert 'id="netAmap"' in SOURCE
    assert 'logo-128.png' in SOURCE


def test_no_blocking_external_font_request():
    assert 'cdn.jsdelivr.net' not in SOURCE

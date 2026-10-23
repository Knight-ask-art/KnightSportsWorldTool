"""Static guards for the login gate.

The full interaction test is Playwright-based (mocked bridge), but these checks
make sure a future edit cannot silently remove the gate or re-introduce a
blocking external font request.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / 'frontend' / 'index.html').read_text(encoding='utf-8')


def test_login_gate_locks_app_until_session_is_ready():
    assert '<body class="login-locked">' in HTML
    assert 'id="loginGate"' in HTML
    assert 'id="loginForm"' in HTML
    assert 'id="loginUser"' in HTML
    assert 'id="loginPass"' in HTML
    assert 'id="loginBtn"' in HTML
    assert 'id="appShell" aria-hidden="true" inert' in HTML


def test_login_gate_uses_the_real_bridge_and_auto_keepalive():
    assert "liveApi('real_login'" in HTML
    assert 'async function ensureKeepalive()' in HTML
    # “随登随退”: local logout does not wait for the server proof.
    assert "liveApi('real_logout', {local_only: true})" in HTML
    assert "showLoginGate(verified ? '已退出登录" in HTML
    assert '已退出本机登录，请重新登录。' in HTML


def test_partial_school_rules_and_issue_records_are_visible():
    assert '留空 = 学校没有这项要求' in HTML
    assert 'data-rec-filter="issue"' in HTML
    assert 'id="netProxy"' in HTML
    assert 'id="netAmap"' in HTML
    assert 'logo-128.png' in HTML


def test_no_blocking_external_font_request():
    assert 'cdn.jsdelivr.net' not in HTML

"""Static guards for the boot splash.

The animation is decorative, so these checks focus on the parts that would
break the app if someone deleted them: the external asset links, the two
access controls, the skip path, the reduced-motion opt-out and the failsafe.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / 'frontend'
HTML = (FRONTEND / 'index.html').read_text(encoding='utf-8')
CSS = (FRONTEND / 'css' / 'splash.css').read_text(encoding='utf-8')
JS = (FRONTEND / 'js' / 'splash.js').read_text(encoding='utf-8')
APP_CSS = (FRONTEND / 'css' / 'app.css').read_text(encoding='utf-8')
APP_JS = (FRONTEND / 'js' / 'app.js').read_text(encoding='utf-8')


def _splash_block():
    start = HTML.index('<!-- boot splash')
    end = HTML.index('</div>', HTML.index('id="bootSkip"')) + len('</div>')
    return HTML[start:end]


def test_splash_assets_are_external_and_wired_before_app_js():
    assert '<link rel="stylesheet" href="css/splash.css">' in HTML
    assert '<script src="js/splash.js"></script>' in HTML
    assert HTML.index('js/splash.js') < HTML.index('js/app.js')
    assert (FRONTEND / 'css' / 'splash.css').is_file()
    assert (FRONTEND / 'js' / 'splash.js').is_file()


def test_splash_is_english_only():
    block = _splash_block()
    assert not re.search(r'[\u4e00-\u9fff]', block)
    assert 'HAOXIANGPAO' in block
    assert 'LOCAL WORKBENCH' in block


def test_splash_has_the_access_step_and_a_skip_path():
    assert 'id="bootAction"' in HTML
    assert 'GRANT ACCESS' in HTML
    assert 'CONFIRM IDENTITY' in JS
    assert 'id="bootSkip"' in HTML
    assert "event.key === 'Escape'" in JS
    assert 'sessionStorage' in JS


def test_splash_respects_reduced_motion_and_has_a_failsafe():
    assert 'prefers-reduced-motion' in CSS
    assert 'boot-failsafe' in CSS
    assert 'animation: boot-failsafe' in CSS


def test_splash_uses_the_packaged_logo():
    assert 'href="logo.png"' in HTML
    assert (FRONTEND / 'logo.png').is_file()


def test_first_run_guide_waits_below_the_splash():
    """A saved session restores while the splash plays; the guide must wait."""
    def block(css, selector):
        match = re.search(re.escape(selector) + r'\s*\{([^}]*)\}', css)
        assert match, selector
        return match.group(1)

    def z_index(css, selector):
        match = re.search(r'z-index:\s*(-?\d+)', block(css, selector))
        assert match, selector
        return int(match.group(1))

    assert z_index(APP_CSS, '.onboard') < z_index(CSS, '.boot')
    start = APP_JS.index('function openOnboard(')
    body = APP_JS[start:APP_JS.index('function closeOnboard(', start)]
    assert '#bootSplash' in body
    assert 'onboardWait' in body


def test_splash_owns_focus_and_releases_the_login_gate():
    """The gate is inert while the splash is up, and the splash traps Tab."""
    assert 'loginGate.inert = true' in JS
    assert 'loginGate.inert = false' in JS
    assert 'hxp:boot-done' in JS
    assert "event.key === 'Tab'" in JS

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


def test_splash_leads_with_chinese_and_keeps_english_as_decoration():
    block = _splash_block()
    assert '\u597d\u60f3\u8dd1 \u00b7 \u672c\u5730\u5de5\u4f5c\u53f0' in block
    assert '\u8fdb\u5165' in block
    assert '\u8df3\u8fc7' in block
    # English stays as small brand decoration, not as the instruction.
    assert 'HAOXIANGPAO' in block
    assert 'LOCAL WORKBENCH' in block


def test_splash_has_one_confirmation_and_a_skip_path():
    assert 'id="bootAction"' in HTML
    assert "btn: '\u8fdb\u5165'" in JS
    # The old two-step access/identity wall is gone: one click gets in.
    assert JS.count('wait: true') == 1
    assert 'CONFIRM IDENTITY' not in JS
    assert 'GRANT ACCESS' not in JS
    assert 'id="bootSkip"' in HTML
    assert '\u8df3\u8fc7' in HTML
    assert "event.key === 'Escape'" in JS
    assert 'sessionStorage' in JS


def test_splash_skips_when_a_local_session_already_exists():
    # A saved session should not be asked to authorise anything: the intro
    # probes the same bridge the page is served from and gets out of the way.
    assert "fetch('/bridge/real_get_session'" in JS
    assert 'token_present' in JS
    assert 'if (found && !done) finish();' in JS
    # The old global was never consumed and was undefined on the early-exit
    # paths; guard against re-introducing it.
    assert 'window.HXPSplash' not in JS


def test_splash_dialog_is_named_by_the_chinese_title():
    line = next(l for l in HTML.splitlines() if 'id="bootSplash"' in l)
    assert 'role="dialog"' in line
    assert 'aria-modal="true"' in line
    assert 'aria-labelledby="bootTitle"' in line
    # aria-label would override the Chinese title for screen readers.
    assert 'aria-label=' not in line


def test_splash_bottom_rail_does_not_overlap_at_390px():
    # At 390px the centered read-out ("READY WHEN YOU ARE") used to share a
    # baseline with "[ 01 / 05 ] 准备": 121px vs 135px, ~14px overlap.  The
    # narrow query lifts the read-out above the rail and stacks the rail.
    narrow = CSS[CSS.index('@media (max-width: 480px)'):]
    assert 'flex-direction: column' in narrow
    assert '.acc-logwrap { bottom: 66px;' in narrow
    # The wide layout must stay untouched by this query.
    wide = CSS[:CSS.index('@media (max-width: 480px)')]
    assert '.acc-logwrap' in wide
    assert 'bottom: 42px' in wide


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


def test_splash_hides_the_login_gate_until_it_drops():
    """A slow bridge boot used to flash the login form for ~1.2 s."""
    hidden_at = JS.index("loginGate.style.visibility = 'hidden'")
    # Early exits (?boot=0, reduced motion, already played) must not hide or
    # inert a gate they never owned.
    early = JS[JS.index('if (off || reduced'):JS.index('/* The login gate sits behind')]
    assert 'visibility' not in early
    assert 'loginGate.inert = true' not in early
    assert JS.index('if (off || reduced') < hidden_at < JS.index('function showPanel()')
    drop = JS[JS.index('function drop()'):JS.index('function remember()')]
    assert "loginGate.style.visibility = ''" in drop
    assert 'loginGate.inert = false' in drop

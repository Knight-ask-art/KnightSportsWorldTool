"""Build-time frontend minification (B of the frontend hardening).

The source tree stays readable; the packaging step runs terser on the staged
copy.  These tests execute the real script against a tiny file and check the
two properties the app depends on: top-level names survive and locals do not.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts' / 'minify_frontend.py'
BUILD = ROOT / 'app' / 'packaging' / 'build.ps1'
MARKER = '/* build artifact: minified; do not edit */'

SAMPLE = """\
/* readable source */
function publicName(value){
  const someVeryLongLocalName = value + 1;
  return someVeryLongLocalName;
}
"""


@pytest.fixture(scope='session')
def node_tools():
    """Skip when the terser CLI cannot be resolved (offline CI, no node)."""
    node = shutil.which('node')
    npx = shutil.which('npx')
    if not node or not npx:
        pytest.skip('node/npx is not installed; minifier behaviour test skipped')
    try:
        probe = subprocess.run(
            [npx, '--yes', 'terser@5.36.0', '--version'],
            check=False, capture_output=True, text=True, encoding='utf-8',
            timeout=90)
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f'terser is not available: {exc}')
    if probe.returncode != 0:
        pytest.skip('terser is not available (offline and not cached)')
    return node


@pytest.fixture()
def frontend(tmp_path):
    js = tmp_path / 'frontend' / 'js'
    js.mkdir(parents=True)
    (js / 'sample.js').write_text(SAMPLE, encoding='utf-8')
    return tmp_path / 'frontend'


def test_minifier_keeps_top_level_names_and_mangles_locals(frontend, node_tools):
    node = node_tools
    proc = subprocess.run([sys.executable, str(SCRIPT), str(frontend)],
                          check=False, capture_output=True, text=True, encoding='utf-8')
    assert proc.returncode == 0, proc.stderr
    assert 'minified 1/1 file(s)' in proc.stdout

    out = (frontend / 'js' / 'sample.js').read_text(encoding='utf-8')
    assert out.startswith(MARKER)
    # charts.js / announcements.js globals must survive for app.js.
    assert 'publicName' in out
    assert 'someVeryLongLocalName' not in out
    check = subprocess.run([node, '--check', str(frontend / 'js' / 'sample.js')],
                           check=False, capture_output=True, text=True, encoding='utf-8')
    assert check.returncode == 0, check.stderr

    # A second run is a no-op, so a rebuilt stage cannot prepend the marker
    # twice or re-mangle already mangled code.
    again = subprocess.run([sys.executable, str(SCRIPT), str(frontend)],
                           check=False, capture_output=True, text=True, encoding='utf-8')
    assert again.returncode == 0, again.stderr
    assert 'already minified' in again.stdout
    assert (frontend / 'js' / 'sample.js').read_text(encoding='utf-8') == out


def test_minifier_leaves_the_source_untouched_on_syntax_error(frontend, node_tools):
    broken = frontend / 'js' / 'zbroken.js'
    broken.write_text('function ( {', encoding='utf-8')
    sample = frontend / 'js' / 'sample.js'
    sample_before = sample.read_text(encoding='utf-8')
    before = broken.read_text(encoding='utf-8')
    proc = subprocess.run([sys.executable, str(SCRIPT), str(frontend)],
                          check=False, capture_output=True, text=True, encoding='utf-8')
    assert proc.returncode != 0
    assert broken.read_text(encoding='utf-8') == before
    # sample.js was minified into a temp file first; it must not be replaced
    # when a later file fails.
    assert sample.read_text(encoding='utf-8') == sample_before
    assert not list((frontend / 'js').glob('.minify-*.js'))


def test_packaging_runs_the_minifier_before_the_smoke_test():
    text = BUILD.read_text(encoding='utf-8')
    assert 'scripts\\minify_frontend.py' in text
    assert text.index('== 1b/5 Minify frontend ==') < text.index('== 2/5 Frozen import check ==')
    assert text.index('== 1b/5 Minify frontend ==') < text.index('== 4/5 Package ==')


def test_docs_describe_the_two_hardening_steps():
    hardening = (ROOT / 'docs' / 'hardening.md').read_text(encoding='utf-8')
    kb = (ROOT / 'docs' / 'kb' / '40-frontend.md').read_text(encoding='utf-8')
    assert 'Phase 4' in hardening
    assert 'data.failure_kind' in hardening
    assert 'minify_frontend.py' in hardening
    assert 'terser' in kb
    assert 'failure_kind' in kb

"""Static guards for the settings-page LLM configuration section."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / 'frontend'
HTML = (FRONTEND / 'index.html').read_text(encoding='utf-8')
JS = (FRONTEND / 'js' / 'app.js').read_text(encoding='utf-8')
CSS = (FRONTEND / 'css' / 'app.css').read_text(encoding='utf-8')


def _js_function(name):
    start = JS.index('function ' + name)
    depth = 0
    for i in range(JS.index('{', start), len(JS)):
        if JS[i] == '{':
            depth += 1
        elif JS[i] == '}':
            depth -= 1
            if depth == 0:
                return JS[start:i + 1]
    raise AssertionError('unterminated function ' + name)


def test_llm_section_position_and_controls():
    settings = HTML.split('id="page-settings"', 1)[1]
    net_pos = settings.index('id="networkSection"')
    llm_pos = settings.index('id="llmSection"')
    real_pos = settings.index('id="realismSection"')
    assert net_pos < llm_pos < real_pos

    llm_block = settings[llm_pos:real_pos]
    for cid in (
        'llmEnabled', 'llmProtocol', 'llmBase', 'llmModel', 'llmKey',
        'llmTemp', 'llmTimeout', 'llmSave', 'llmTest', 'llmClearKey',
        'llmStatus', 'llmHint',
    ):
        assert f'id="{cid}"' in llm_block, cid

    enabled_tag = re.search(r'<input\b[^>]*\bid="llmEnabled"[^>]*>', llm_block)
    assert enabled_tag
    assert 'type="checkbox"' in enabled_tag.group(0)
    assert 'checked' not in enabled_tag.group(0)

    proto_block = re.search(r'<select\b[^>]*\bid="llmProtocol"[^>]*>.*?</select>',
                            llm_block, re.S)
    assert proto_block
    assert 'disabled' in proto_block.group(0)
    for hardcoded in ('openai', 'deepseek', 'anthropic', 'gemini', 'ollama'):
        assert f'value="{hardcoded}"' not in proto_block.group(0)

    base_tag = re.search(r'<input\b[^>]*\bid="llmBase"[^>]*>', llm_block)
    assert base_tag and 'type="url"' in base_tag.group(0)

    model_tag = re.search(r'<input\b[^>]*\bid="llmModel"[^>]*>', llm_block)
    assert model_tag and 'type="text"' in model_tag.group(0)

    key_tag = re.search(r'<input\b[^>]*\bid="llmKey"[^>]*>', llm_block)
    assert key_tag
    assert 'type="password"' in key_tag.group(0)
    assert 'autocomplete="off"' in key_tag.group(0)

    temp_tag = re.search(r'<input\b[^>]*\bid="llmTemp"[^>]*>', llm_block)
    assert temp_tag
    for attr in ('type="number"', 'min="0"', 'max="1"', 'step="0.1"', 'value="0.2"'):
        assert attr in temp_tag.group(0)

    timeout_tag = re.search(r'<input\b[^>]*\bid="llmTimeout"[^>]*>', llm_block)
    assert timeout_tag
    for attr in ('type="number"', 'min="5"', 'max="120"', 'value="30"'):
        assert attr in timeout_tag.group(0)


def test_llm_section_states_dpapi_and_no_coordinate_privacy():
    settings = HTML.split('id="page-settings"', 1)[1]
    llm_block = settings.split('id="llmSection"', 1)[1].split(
        'id="realismSection"', 1)[0]
    assert 'Windows DPAPI' in llm_block
    assert '本机' in llm_block
    assert '路线坐标不会发给模型' in llm_block
    assert '只发候选统计' in llm_block


def test_llm_js_functions_and_bridge_methods():
    for fn in ('loadLlmSettings', 'saveLlmSettings',
               'testLlmConnection', 'clearLlmKey'):
        assert f'async function {fn}(' in JS, fn

    load_fn = _js_function('loadLlmSettings')
    assert "liveApi('real_llm_settings')" in load_fn
    assert '协议列表读取失败' in load_fn
    assert '已保存；留空=不修改' in load_fn
    assert 'toast(' in load_fn

    save_fn = _js_function('saveLlmSettings')
    assert "liveApi('real_llm_settings_save', payload, 20000)" in save_fn
    assert 'payload.api_key = key' in save_fn
    assert 'clear_key' not in save_fn
    assert 'LLM 设置已保存' in save_fn
    assert 'toast(' in save_fn

    test_fn = _js_function('testLlmConnection')
    assert "liveApi('real_llm_test', {}, 40000)" in test_fn
    assert '连接正常 · ' in test_fn
    assert '连接失败：' in test_fn
    assert 'toast(' in test_fn

    clear_fn = _js_function('clearLlmKey')
    assert 'confirm(' in clear_fn
    assert "liveApi('real_llm_settings_save', {clear_key: true})" in clear_fn
    assert 'toast(' in clear_fn


def test_llm_boot_wiring_and_copy_guards():
    assert "if($('#llmSection'))" in JS
    net_call = JS.index('loadNetworkConfig()')
    llm_call = JS.index('loadLlmSettings()', net_call)
    assert llm_call - net_call < 200

    for text in (HTML, JS, CSS):
        assert 'AI 生成' not in text

    assert '.llm-switch-row' in CSS
    assert '.llm-switch' in CSS
    assert '.llm-grid' in CSS


def test_no_plaintext_key_written_into_dom_or_storage():
    for script_body in re.findall(r'<script\b[^>]*>([\s\S]*?)</script>', HTML):
        assert 'llmKey' not in script_body
        assert 'api_key' not in script_body
    key_tag = re.search(r'<input\b[^>]*\bid="llmKey"[^>]*>', HTML)
    assert key_tag and 'value=' not in key_tag.group(0)

    assert '.api_key' not in _js_function('loadLlmSettings')
    for line in JS.splitlines():
        if '#llmKey' in line and '.value' in line and '=' in line:
            assert ".value = ''" in line or "$('#llmKey').value :" in line
        if 'localStorage' in line or 'sessionStorage' in line:
            assert 'llm' not in line.lower() and 'api_key' not in line.lower()

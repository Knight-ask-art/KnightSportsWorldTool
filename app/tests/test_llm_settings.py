"""LLM 设置、DPAPI 封存与选路 Prompt 脱敏单元测试。"""

from __future__ import annotations

import json
import os
import re

import pytest

from replica.adapters.bridge_api import ReplicaApi
from replica.application import credentials, llm_client, llm_settings


@pytest.fixture
def fake_dpapi(monkeypatch):
    """Cross-platform identity seal/unseal for testing key roundtrips."""
    monkeypatch.setattr(
        credentials,
        'seal',
        lambda text, entropy=b'': f'SEALED:{text}',
    )
    monkeypatch.setattr(
        credentials,
        'unseal',
        lambda value, entropy=b'': (
            value[len('SEALED:'):]
            if str(value).startswith('SEALED:')
            else (_ for _ in ()).throw(credentials.CredentialError('bad blob'))
        ),
    )


def test_presets_seven_protocols_and_defaults():
    items = llm_settings.presets()
    assert len(items) == 7
    ids = [p['id'] for p in items]
    assert len(set(ids)) == 7
    assert ids == [
        'openai', 'deepseek', 'openrouter',
        'anthropic', 'gemini', 'ollama', 'custom',
    ]
    by_id = {p['id']: p for p in items}
    assert by_id['openai']['base_url'] == 'https://api.openai.com/v1'
    assert by_id['deepseek']['base_url'] == 'https://api.deepseek.com/v1'
    assert by_id['openrouter']['base_url'] == 'https://openrouter.ai/api/v1'
    assert by_id['anthropic']['base_url'] == 'https://api.anthropic.com'
    assert by_id['gemini']['base_url'] == 'https://generativelanguage.googleapis.com/v1beta'
    assert by_id['ollama']['base_url'] == 'http://127.0.0.1:11434/v1'
    assert by_id['custom']['base_url'] == ''
    assert by_id['ollama']['needs_key'] is False
    assert by_id['custom']['needs_key'] is False
    assert by_id['openai']['needs_key'] is True
    assert by_id['openai']['default_model'] == 'gpt-4o-mini'
    assert by_id['deepseek']['default_model'] == 'deepseek-chat'
    assert by_id['anthropic']['default_model'] == 'claude-sonnet-4-5'
    assert all(p['label'] for p in items)


def test_default_load_and_roundtrip_without_key(tmp_path):
    store = str(tmp_path)
    default = llm_settings.load(store)
    assert default['version'] == llm_settings.SCHEMA_VERSION
    assert default['enabled'] is False
    assert default['protocol'] == 'openai'
    assert default['has_key'] is False
    assert 'api_key' not in default

    saved = llm_settings.save(store, {
        'enabled': True,
        'protocol': 'ollama',
        'base_url': 'http://127.0.0.1:11434/v1',
        'model': 'qwen2.5:7b',
        'temperature': 0.5,
        'timeout_s': 45,
    })
    assert saved['enabled'] is True
    assert saved['protocol'] == 'ollama'
    assert saved['base_url'] == 'http://127.0.0.1:11434/v1'
    assert saved['model'] == 'qwen2.5:7b'
    assert saved['temperature'] == 0.5
    assert saved['timeout_s'] == 45
    assert saved['has_key'] is False
    assert 'api_key' not in saved


def test_key_sealed_never_in_load_keep_empty_and_clear(tmp_path, fake_dpapi):
    store = str(tmp_path)
    secret = 'sk-test-secret-99887766'
    saved = llm_settings.save(store, {
        'enabled': True,
        'protocol': 'openai',
        'model': 'gpt-4o-mini',
        'api_key': secret,
    })
    assert saved['has_key'] is True
    assert 'api_key' not in saved
    assert secret not in json.dumps(saved)

    raw_text = (tmp_path / 'llm_settings.json').read_text(encoding='utf-8')
    assert secret not in raw_text or 'SEALED:' in raw_text

    # Empty api_key keeps the existing key.
    updated = llm_settings.save(store, {
        'protocol': 'deepseek',
        'model': 'deepseek-chat',
        'api_key': '',
    })
    assert updated['has_key'] is True
    assert 'api_key' not in updated
    eff = llm_settings.effective(store)
    assert eff is not None
    assert eff['api_key'] == secret
    assert eff['protocol'] == 'deepseek'

    # clear_key removes it.
    cleared_key = llm_settings.save(store, {'clear_key': True})
    assert cleared_key['has_key'] is False
    assert llm_settings.effective(store) is None

    assert llm_settings.clear(store) is True
    assert llm_settings.clear(store) is False


def test_non_windows_refuses_key_save_without_plaintext_fallback(tmp_path, monkeypatch):
    store = str(tmp_path)

    def fail_seal(*_a, **_kw):
        raise credentials.CredentialError('当前系统不支持 Windows DPAPI 本地加密保存')

    monkeypatch.setattr(credentials, 'seal', fail_seal)
    with pytest.raises(credentials.CredentialError):
        llm_settings.save(store, {'protocol': 'openai', 'api_key': 'sk-should-not-write'})

    assert not (tmp_path / 'llm_settings.json').exists()

    # Non-key fields can still be saved.
    saved = llm_settings.save(store, {
        'enabled': True,
        'protocol': 'ollama',
        'model': 'qwen2.5:7b',
    })
    assert saved['protocol'] == 'ollama'
    assert saved['has_key'] is False


@pytest.mark.windows_only
def test_windows_dpapi_real_seal_roundtrip(tmp_path):
    store = str(tmp_path)
    secret = 'sk-win-dpapi-real-123456'
    saved = llm_settings.save(store, {
        'enabled': True,
        'protocol': 'openai',
        'api_key': secret,
    })
    assert saved['has_key'] is True
    assert 'api_key' not in saved
    raw_disk = (tmp_path / 'llm_settings.json').read_text(encoding='utf-8')
    assert secret not in raw_disk
    eff = llm_settings.effective(store)
    assert eff is not None
    assert eff['api_key'] == secret


def test_corrupt_old_and_unknown_protocol_files_return_defaults(tmp_path):
    store = str(tmp_path)
    path = tmp_path / 'llm_settings.json'

    path.write_text('{not valid json', encoding='utf-8')
    assert llm_settings.load(store)['protocol'] == 'openai'

    path.write_text(json.dumps({'version': 99, 'protocol': 'openai'}), encoding='utf-8')
    assert llm_settings.load(store)['protocol'] == 'openai'

    path.write_text(json.dumps({'version': 1, 'protocol': 'bogus'}), encoding='utf-8')
    assert llm_settings.load(store)['protocol'] == 'openai'


def test_validation_rejects_bad_url_and_clamps_ranges(tmp_path):
    store = str(tmp_path)
    with pytest.raises(ValueError):
        llm_settings.save(store, {'protocol': 'bogus'})

    for bad_url in (
        'ftp://example.com/v1',
        'javascript:alert(1)',
        'http://',
        'https://bad host.example/v1',
        'https://example.com/' + 'a' * 500,
    ):
        with pytest.raises(ValueError):
            llm_settings.save(store, {'protocol': 'openai', 'base_url': bad_url})

    saved = llm_settings.save(store, {
        'protocol': 'openai',
        'temperature': 5.0,
        'timeout_s': 999,
        'model': 'm' * 250,
    })
    assert saved['temperature'] == 1.0
    assert saved['timeout_s'] == llm_settings.MAX_TIMEOUT_S
    assert len(saved['model']) == 200

    saved_low = llm_settings.save(store, {
        'protocol': 'openai',
        'temperature': -2.0,
        'timeout_s': 1,
    })
    assert saved_low['temperature'] == 0.0
    assert saved_low['timeout_s'] == llm_settings.MIN_TIMEOUT_S


def test_effective_requires_enabled_and_key_when_needed(tmp_path, fake_dpapi):
    store = str(tmp_path)
    llm_settings.save(store, {
        'enabled': False,
        'protocol': 'openai',
        'api_key': 'sk-disabled',
    })
    assert llm_settings.effective(store) is None

    llm_settings.save(store, {'enabled': True, 'clear_key': True})
    assert llm_settings.effective(store) is None

    llm_settings.save(store, {'enabled': True, 'api_key': 'sk-enabled'})
    eff = llm_settings.effective(store)
    assert eff is not None
    assert eff['api_key'] == 'sk-enabled'

    # Ollama works without a key when enabled.
    llm_settings.save(store, {
        'enabled': True,
        'protocol': 'ollama',
        'clear_key': True,
    })
    eff_ollama = llm_settings.effective(store)
    assert eff_ollama is not None
    assert eff_ollama['api_key'] == ''


def test_build_choice_prompt_strips_coords_and_secrets():
    candidates = [
        {
            'template_id': 'lr_11111111',
            'name': '操场',
            'score': 0.91,
            'line': [[22.410787, 112.940804], [22.411234, 112.941234]],
            'closed': True,
            'lap_m': 1420.5,
            'laps': 2,
            'samples': 8,
            'target_m': 2500.0,
            'missed': [],
            'fence_ok': True,
            'detour_m': 0.0,
            'token': 'secret-token-value',
            'uid': '15663170',
            'unid': '47203',
            'campus': '江门校区',
            'nickname': '测试同学',
        },
        {
            'template_id': 'lr_22222222',
            'line': [[22.420123, 112.950456]],
            'lap_m': 800.4,
            'laps': 3,
            'samples': 2,
            'missed': [{'lat': 22.41, 'lon': 112.94}],
            'fence_ok': False,
            'detour_m': 125.7,
        },
    ]
    messages = llm_settings.build_choice_prompt(candidates, 2500.4)
    assert len(messages) == 2
    assert messages[0]['role'] == 'system'
    assert messages[1]['role'] == 'user'

    serialized = json.dumps(messages, ensure_ascii=False)
    assert re.search(r'\d{2,3}\.\d{4,}', serialized) is None
    for forbidden in ('token', 'uid', 'unid', 'line', '15663170', '47203', '江门校区', '测试同学'):
        assert forbidden not in serialized

    assert '1420' in serialized or '1421' in serialized
    assert '2500' in serialized
    assert '8' in serialized
    assert '126' in serialized


def test_parse_choice_formats():
    candidates = [
        {'template_id': 'lr_aaa'},
        {'template_id': 'lr_bbb'},
        {'template_id': 'lr_ccc'},
    ]
    assert llm_settings.parse_choice('1', candidates) == 'lr_aaa'
    assert llm_settings.parse_choice('2', candidates) == 'lr_bbb'
    assert llm_settings.parse_choice('第2条', candidates) == 'lr_bbb'
    assert llm_settings.parse_choice('第二条', candidates) == 'lr_bbb'
    assert llm_settings.parse_choice('二', candidates) == 'lr_bbb'
    assert llm_settings.parse_choice('候选 2', candidates) == 'lr_bbb'
    assert llm_settings.parse_choice('候选三', candidates) == 'lr_ccc'
    assert llm_settings.parse_choice('{"choice": 2}', candidates) == 'lr_bbb'
    assert llm_settings.parse_choice('{"choice": "二"}', candidates) == 'lr_bbb'
    assert llm_settings.parse_choice('```json\n{"choice": 3}\n```', candidates) == 'lr_ccc'

    assert llm_settings.parse_choice('', candidates) is None
    assert llm_settings.parse_choice('0', candidates) is None
    assert llm_settings.parse_choice('4', candidates) is None
    assert llm_settings.parse_choice('-1', candidates) is None
    assert llm_settings.parse_choice('{"choice": 9}', candidates) is None
    assert llm_settings.parse_choice('无法决定', candidates) is None


def test_bridge_llm_methods(tmp_path, fake_dpapi, monkeypatch):
    api = ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'', str(tmp_path))

    res = api.real_llm_settings()
    assert res['code'] == 200
    assert len(res['data']['protocols']) == 7
    assert res['data']['settings']['has_key'] is False

    # Test with no key / disabled -> fail
    test_disabled = api.real_llm_test()
    assert test_disabled['code'] != 200

    # Save via kwargs
    saved = api.real_llm_settings_save(
        enabled=True,
        protocol='openai',
        model='gpt-4o-mini',
        api_key='sk-bridge-key',
    )
    assert saved['code'] == 200
    assert saved['data']['settings']['has_key'] is True
    assert 'sk-bridge-key' not in json.dumps(saved)

    # Test invalid URL via dict payload
    bad = api.real_llm_settings_save({'protocol': 'openai', 'base_url': 'ftp://bad'})
    assert bad['code'] != 200

    # Test real_llm_test passes JHP_PROXY and does not leak key
    captured = {}

    def fake_test_connection(settings, proxies=None):
        captured['settings'] = settings
        captured['proxies'] = proxies
        return {
            'ok': True,
            'latency_ms': 25,
            'model': settings['model'],
            'error': '',
            'error_kind': '',
        }

    monkeypatch.setenv('JHP_PROXY', 'http://127.0.0.1:7890')
    monkeypatch.setattr(llm_client, 'test_connection', fake_test_connection)

    tested = api.real_llm_test()
    assert tested['code'] == 200
    assert tested['data']['ok'] is True
    assert captured['proxies'] == {
        'http': 'http://127.0.0.1:7890',
        'https': 'http://127.0.0.1:7890',
    }
    assert 'sk-bridge-key' not in json.dumps(tested)

    cleared = api.real_llm_settings_clear()
    assert cleared['code'] == 200
    assert cleared['data']['cleared'] is True

"""多协议 LLM 客户端单元测试（全部走本地 mock，不发起网络请求）。"""

from __future__ import annotations

import pytest
import requests

from replica.application import llm_client


@pytest.fixture(autouse=True)
def _force_legacy_client(monkeypatch):
    """These tests guard the requests fallback, not the optional SDK path."""
    monkeypatch.setenv('JHP_LLM_SDK', '0')


class _FakeResponse:
    def __init__(self, status_code: int = 200, json_data=None, text: str = '',
                 content: bytes | None = None, json_raises: Exception | None = None):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self.content = content
        self._json_raises = json_raises

    def json(self):
        if self._json_raises is not None:
            raise self._json_raises
        return self._json_data


def test_openai_compatible_request_and_response(monkeypatch):
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        seen['url'] = url
        seen['headers'] = headers
        seen['json'] = json
        seen['timeout'] = timeout
        seen['kwargs'] = kwargs
        return _FakeResponse(200, {
            'choices': [{'message': {'content': '2'}}],
        })

    monkeypatch.setattr(requests, 'post', fake_post)
    res = llm_client.chat(
        {
            'protocol': 'openai',
            'base_url': 'https://api.openai.com/v1',
            'model': 'gpt-4o-mini',
            'api_key': 'sk-openai-123',
            'temperature': 0.2,
            'timeout_s': 25,
        },
        [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'pick one'},
        ],
    )
    assert res['ok'] is True
    assert res['text'] == '2'
    assert res['status'] == 200
    assert res['error_kind'] == ''
    assert seen['url'] == 'https://api.openai.com/v1/chat/completions'
    assert seen['headers']['Authorization'] == 'Bearer sk-openai-123'
    assert seen['json']['model'] == 'gpt-4o-mini'
    assert seen['json']['stream'] is False
    assert seen['json']['max_tokens'] == 256
    assert seen['timeout'] == 25


def test_ollama_omits_bearer_when_no_key(monkeypatch):
    seen = {}

    def fake_request(method, url, headers=None, json=None, timeout=None, **kwargs):
        seen['method'] = method
        seen['url'] = url
        seen['headers'] = headers
        return _FakeResponse(200, {
            'choices': [{'message': {'content': '1'}}],
        })

    monkeypatch.setattr(requests, 'request', fake_request)
    res = llm_client.chat(
        {
            'protocol': 'ollama',
            'base_url': 'http://127.0.0.1:11434/v1',
            'model': 'qwen2.5:7b',
            'api_key': '',
            'temperature': 0.2,
            'timeout_s': 30,
        },
        [{'role': 'user', 'content': 'hi'}],
    )
    assert res['ok'] is True
    assert seen['method'] == 'POST'
    assert seen['url'] == 'http://127.0.0.1:11434/v1/chat/completions'
    assert 'Authorization' not in seen['headers']


def test_anthropic_request_and_response(monkeypatch):
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        seen['url'] = url
        seen['headers'] = headers
        seen['json'] = json
        return _FakeResponse(200, {
            'content': [{'type': 'text', 'text': '1'}],
        })

    monkeypatch.setattr(requests, 'post', fake_post)
    res = llm_client.chat(
        {
            'protocol': 'anthropic',
            'base_url': 'https://api.anthropic.com',
            'model': 'claude-sonnet-4-5',
            'api_key': 'sk-ant-123',
            'temperature': 0.3,
            'timeout_s': 20,
        },
        [
            {'role': 'system', 'content': 'system rule'},
            {'role': 'user', 'content': 'choose'},
        ],
    )
    assert res['ok'] is True
    assert res['text'] == '1'
    assert seen['url'] == 'https://api.anthropic.com/v1/messages'
    assert seen['headers']['x-api-key'] == 'sk-ant-123'
    assert seen['headers']['anthropic-version'] == '2023-06-01'
    assert seen['json']['system'] == 'system rule'
    assert seen['json']['messages'] == [{'role': 'user', 'content': 'choose'}]
    assert seen['json']['max_tokens'] == 256


def test_gemini_request_and_response(monkeypatch):
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        seen['url'] = url
        seen['headers'] = headers
        seen['json'] = json
        return _FakeResponse(200, {
            'candidates': [{'content': {'parts': [{'text': '3'}]}}],
        })

    monkeypatch.setattr(requests, 'post', fake_post)
    res = llm_client.chat(
        {
            'protocol': 'gemini',
            'base_url': 'https://generativelanguage.googleapis.com/v1beta',
            'model': 'gemini-2.0-flash',
            'api_key': 'AIza-secret-key',
            'temperature': 0.1,
            'timeout_s': 15,
        },
        [{'role': 'user', 'content': 'hello'}],
    )
    assert res['ok'] is True
    assert res['text'] == '3'
    assert seen['url'] == (
        'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent'
    )
    assert seen['headers']['x-goog-api-key'] == 'AIza-secret-key'
    assert seen['json']['contents'] == [{'role': 'user', 'parts': [{'text': 'hello'}]}]


def test_http_error_kinds_and_key_redaction(monkeypatch):
    secret = 'sk-super-secret-leak-999'

    for code, expected_kind in (
        (401, 'auth'),
        (403, 'auth'),
        (404, 'not_found'),
        (429, 'rate_limit'),
        (502, 'server'),
    ):
        monkeypatch.setattr(
            requests,
            'post',
            lambda *a, _c=code, **kw: _FakeResponse(
                _c,
                {'error': {'message': f'bad key {secret}'}},
            ),
        )
        res = llm_client.chat(
            {
                'protocol': 'openai',
                'base_url': 'https://api.openai.com/v1',
                'model': 'gpt-4o-mini',
                'api_key': secret,
                'timeout_s': 10,
            },
            [{'role': 'user', 'content': 'hi'}],
        )
        assert res['ok'] is False
        assert res['error_kind'] == expected_kind
        assert res['status'] == code
        assert secret not in res['error']
        assert '***' in res['error']


def test_timeout_network_bad_json_oversize_and_config(monkeypatch):
    secret = 'sk-timeout-secret'
    base_settings = {
        'protocol': 'openai',
        'base_url': 'https://api.openai.com/v1',
        'model': 'gpt-4o-mini',
        'api_key': secret,
        'timeout_s': 10,
    }

    # Timeout
    def raise_timeout(*_a, **_kw):
        raise requests.Timeout(f'timeout with {secret}')

    monkeypatch.setattr(requests, 'post', raise_timeout)
    res_to = llm_client.chat(base_settings, [{'role': 'user', 'content': 'hi'}])
    assert res_to['ok'] is False
    assert res_to['error_kind'] == 'timeout'
    assert secret not in res_to['error']

    # Network error
    def raise_conn(*_a, **_kw):
        raise requests.ConnectionError(f'dns fail {secret}')

    monkeypatch.setattr(requests, 'post', raise_conn)
    res_net = llm_client.chat(base_settings, [{'role': 'user', 'content': 'hi'}])
    assert res_net['ok'] is False
    assert res_net['error_kind'] == 'network'
    assert secret not in res_net['error']

    # Invalid JSON
    monkeypatch.setattr(
        requests,
        'post',
        lambda *a, **kw: _FakeResponse(200, json_raises=ValueError('bad json')),
    )
    res_json = llm_client.chat(base_settings, [{'role': 'user', 'content': 'hi'}])
    assert res_json['ok'] is False
    assert res_json['error_kind'] == 'bad_response'

    # Oversized response (> 256 KB)
    monkeypatch.setattr(
        requests,
        'post',
        lambda *a, **kw: _FakeResponse(
            200,
            {'choices': [{'message': {'content': 'ok'}}]},
            content=b'x' * (256 * 1024 + 1),
        ),
    )
    res_big = llm_client.chat(base_settings, [{'role': 'user', 'content': 'hi'}])
    assert res_big['ok'] is False
    assert res_big['error_kind'] == 'bad_response'

    # Missing config (e.g. missing key for openai)
    res_cfg = llm_client.chat(
        {
            'protocol': 'openai',
            'base_url': 'https://api.openai.com/v1',
            'model': 'gpt-4o-mini',
            'api_key': '',
        },
        [{'role': 'user', 'content': 'hi'}],
    )
    assert res_cfg['ok'] is False
    assert res_cfg['error_kind'] == 'config'


def test_test_connection_returns_ok_and_latency(monkeypatch):
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        seen['messages'] = json['messages']
        seen['proxies'] = kwargs.get('proxies')
        return _FakeResponse(200, {'choices': [{'message': {'content': 'OK'}}]})

    monkeypatch.setattr(requests, 'post', fake_post)
    res = llm_client.test_connection(
        {
            'protocol': 'deepseek',
            'base_url': 'https://api.deepseek.com/v1',
            'model': 'deepseek-chat',
            'api_key': 'sk-ds-123',
            'timeout_s': 15,
        },
        proxies={'http': 'http://127.0.0.1:7890', 'https': 'http://127.0.0.1:7890'},
    )
    assert res['ok'] is True
    assert res['model'] == 'deepseek-chat'
    assert isinstance(res['latency_ms'], int) and res['latency_ms'] >= 0
    assert res['error'] == ''
    assert res['error_kind'] == ''
    assert seen['messages'] == [{'role': 'user', 'content': '只回复 OK'}]
    assert seen['proxies'] == {
        'http': 'http://127.0.0.1:7890',
        'https': 'http://127.0.0.1:7890',
    }

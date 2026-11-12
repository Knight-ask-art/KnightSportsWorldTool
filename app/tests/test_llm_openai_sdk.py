"""OpenAI SDK adapter tests; no network and no real API key."""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

import requests

from replica.application import llm_client, llm_openai_sdk

SETTINGS = {
    'protocol': 'openai',
    'base_url': 'https://api.openai.com/v1',
    'model': 'gpt-4o-mini',
    'api_key': 'sk-test-secret-123',
    'temperature': 0.2,
    'timeout_s': 25,
}


class _FakeCompletions:
    def __init__(self, response=None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.seen: dict = {}

    def create(self, **kwargs):
        self.seen.update(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class _FakeClient:
    def __init__(self, response=None, error: Exception | None = None):
        self.completions = _FakeCompletions(response, error)
        self.chat = SimpleNamespace(completions=self.completions)
        self.closed = False

    def close(self):
        self.closed = True


def _allow_sdk(monkeypatch):
    monkeypatch.setenv('JHP_LLM_SDK', '1')
    monkeypatch.setattr(llm_openai_sdk, 'available', lambda: True)


def _response(text: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def test_openai_compatible_chat_uses_the_sdk(monkeypatch):
    _allow_sdk(monkeypatch)
    fake = _FakeClient(response=_response(' 2 '))
    factory = {}

    def fake_make_client(settings, timeout_s, proxies):
        factory.update({
            'settings': settings,
            'timeout_s': timeout_s,
            'proxies': proxies,
        })
        return fake

    monkeypatch.setattr(llm_openai_sdk, '_make_client', fake_make_client)
    res = llm_client.chat(
        SETTINGS,
        [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': '选一个'},
        ],
    )

    assert res['ok'] is True
    assert res['text'] == '2'
    assert res['status'] == 200
    assert res['error_kind'] == ''
    assert fake.completions.seen['model'] == 'gpt-4o-mini'
    assert fake.completions.seen['temperature'] == 0.2
    assert fake.completions.seen['max_tokens'] == 256
    assert fake.completions.seen['stream'] is False
    assert fake.completions.seen['messages'][1]['content'] == '选一个'
    assert factory['timeout_s'] == 25
    assert factory['proxies'] is None
    assert fake.closed is True


def test_sdk_uses_placeholder_key_for_ollama_and_keeps_proxy(monkeypatch):
    import httpx
    import openai

    seen = {}

    class FakeHTTPClient:
        def __init__(self, **kwargs):
            seen['http'] = kwargs

    def fake_openai(**kwargs):
        seen['openai'] = kwargs
        return object()

    monkeypatch.setattr(httpx, 'Client', FakeHTTPClient)
    monkeypatch.setattr(openai, 'OpenAI', fake_openai)

    llm_openai_sdk._make_client(
        {
            'protocol': 'ollama',
            'base_url': 'http://127.0.0.1:11434/v1',
            'model': 'qwen2.5:7b',
            'api_key': '',
        },
        30,
        {'http': 'http://127.0.0.1:7890', 'https': 'http://127.0.0.1:7890'},
    )

    assert seen['openai']['api_key'] == 'not-needed'
    assert seen['openai']['base_url'] == 'http://127.0.0.1:11434/v1'
    assert seen['openai']['timeout'] == 30
    assert seen['openai']['max_retries'] == 0
    assert seen['http']['proxy'] == 'http://127.0.0.1:7890'
    assert 'http_client' in seen['openai']


def test_sdk_status_error_maps_kind_and_redacts_key(monkeypatch):
    secret = 'sk-sdk-secret-leak-999'
    settings = dict(SETTINGS, api_key=secret)

    class RateLimited(Exception):
        status_code = 429
        body: ClassVar[dict] = {'error': {'message': f'rate limited for {secret}'}}

    _allow_sdk(monkeypatch)
    fake = _FakeClient(error=RateLimited('too many requests'))
    monkeypatch.setattr(llm_openai_sdk, '_make_client',
                        lambda settings, timeout_s, proxies: fake)

    res = llm_openai_sdk.chat(settings, [{'role': 'user', 'content': 'hi'}])
    assert res['ok'] is False
    assert res['error_kind'] == 'rate_limit'
    assert res['status'] == 429
    assert secret not in res['error']
    assert '***' in res['error']
    assert fake.closed is True


def test_sdk_timeout_and_connection_classification(monkeypatch):
    class APITimeoutError(Exception):
        pass

    class APIConnectionError(Exception):
        pass

    assert llm_openai_sdk._classify_exception(APITimeoutError()) == ('timeout', 0)
    assert llm_openai_sdk._classify_exception(APIConnectionError()) == ('network', 0)

    _allow_sdk(monkeypatch)
    fake = _FakeClient(error=APITimeoutError('timed out'))
    monkeypatch.setattr(llm_openai_sdk, '_make_client',
                        lambda settings, timeout_s, proxies: fake)
    res = llm_openai_sdk.chat(SETTINGS, [{'role': 'user', 'content': 'hi'}])
    assert res['ok'] is False
    assert res['error_kind'] == 'timeout'


def test_sdk_empty_choices_and_oversized_text(monkeypatch):
    _allow_sdk(monkeypatch)
    fake = _FakeClient(response=SimpleNamespace(choices=[]))
    monkeypatch.setattr(llm_openai_sdk, '_make_client',
                        lambda settings, timeout_s, proxies: fake)
    res = llm_openai_sdk.chat(SETTINGS, [{'role': 'user', 'content': 'hi'}])
    assert res['ok'] is False
    assert res['error_kind'] == 'bad_response'

    fake = _FakeClient(response=_response('x' * (256 * 1024 + 1)))
    monkeypatch.setattr(llm_openai_sdk, '_make_client',
                        lambda settings, timeout_s, proxies: fake)
    res_big = llm_openai_sdk.chat(SETTINGS, [{'role': 'user', 'content': 'hi'}])
    assert res_big['ok'] is False
    assert res_big['error_kind'] == 'bad_response'


def test_sdk_reads_content_parts_from_models_or_dicts(monkeypatch):
    _allow_sdk(monkeypatch)
    fake = _FakeClient(response={
        'choices': [{
            'message': {
                'content': [
                    {'type': 'text', 'text': '1'},
                    SimpleNamespace(type='text', text=' 2'),
                ],
            },
        }],
    })
    monkeypatch.setattr(llm_openai_sdk, '_make_client',
                        lambda settings, timeout_s, proxies: fake)
    res = llm_openai_sdk.chat(SETTINGS, [{'role': 'user', 'content': 'hi'}])
    assert res['ok'] is True
    assert res['text'] == '1 2'


def test_sdk_missing_key_is_a_config_error(monkeypatch):
    _allow_sdk(monkeypatch)
    monkeypatch.setattr(
        llm_openai_sdk, '_make_client',
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError('must not build')),
    )
    res = llm_openai_sdk.chat(
        dict(SETTINGS, api_key=''),
        [{'role': 'user', 'content': 'hi'}],
    )
    assert res['ok'] is False
    assert res['error_kind'] == 'config'


def test_sdk_is_not_used_for_anthropic(monkeypatch):
    monkeypatch.setattr(
        llm_openai_sdk, '_make_client',
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError('SDK must not run')),
    )

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        return SimpleNamespace(
            status_code=200,
            content=b'',
            text='',
            json=lambda: {'content': [{'type': 'text', 'text': '1'}]},
        )

    monkeypatch.setattr(requests, 'post', fake_post)
    res = llm_client.chat(
        {
            'protocol': 'anthropic',
            'base_url': 'https://api.anthropic.com',
            'model': 'claude-sonnet-4-5',
            'api_key': 'sk-ant-123',
        },
        [{'role': 'user', 'content': 'hi'}],
    )
    assert res['ok'] is True
    assert res['text'] == '1'


def test_sdk_env_switch_falls_back_to_requests(monkeypatch):
    monkeypatch.setenv('JHP_LLM_SDK', '0')
    monkeypatch.setattr(
        llm_openai_sdk, '_make_client',
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError('SDK must not run')),
    )

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        return SimpleNamespace(
            status_code=200,
            content=b'',
            text='',
            json=lambda: {'choices': [{'message': {'content': '1'}}]},
        )

    monkeypatch.setattr(requests, 'post', fake_post)
    res = llm_client.chat(SETTINGS, [{'role': 'user', 'content': 'hi'}])
    assert res['ok'] is True
    assert res['text'] == '1'


def test_sdk_unavailable_falls_back_without_duplicating(monkeypatch):
    monkeypatch.setenv('JHP_LLM_SDK', '1')
    monkeypatch.setattr(llm_openai_sdk, 'available', lambda: False)

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        return SimpleNamespace(
            status_code=200,
            content=b'',
            text='',
            json=lambda: {'choices': [{'message': {'content': '2'}}]},
        )

    monkeypatch.setattr(requests, 'post', fake_post)
    res = llm_client.chat(SETTINGS, [{'role': 'user', 'content': 'hi'}])
    assert res['ok'] is True
    assert res['text'] == '2'


def test_sdk_adapter_crash_does_not_retry_over_http(monkeypatch):
    _allow_sdk(monkeypatch)
    monkeypatch.setattr(
        llm_openai_sdk, 'chat',
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('adapter bug')),
    )
    monkeypatch.setattr(
        requests, 'post',
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError('must not fall back')),
    )
    res = llm_client.chat(SETTINGS, [{'role': 'user', 'content': 'hi'}])
    assert res['ok'] is False
    assert res['error_kind'] == 'config'
    assert '适配器异常' in res['error']

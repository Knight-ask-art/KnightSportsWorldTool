"""多协议 LLM 客户端。

OpenAI 兼容接口优先走官方 ``openai`` SDK 薄适配层（``llm_openai_sdk``）；
SDK 未安装或 ``JHP_LLM_SDK=0`` 时自动回退到仅依赖 ``requests`` 的实现。
Anthropic Messages API 与 Google Gemini generateContent API 仍使用
``requests`` 实现。不读取磁盘配置，不打印请求头，错误信息始终做 Key 脱敏。
"""

from __future__ import annotations

import os
import time
from urllib.parse import urlparse

import requests
from requests import api as _requests_api

from . import llm_settings

MAX_RESPONSE_BYTES = 256 * 1024


def _sdk_allowed() -> bool:
    """Allow an explicit opt-out for debugging or minimal installs."""
    raw = str(os.environ.get('JHP_LLM_SDK', '1')).strip().lower()
    return raw not in ('0', 'false', 'off', 'no')


def _redact(text: str, api_key: str) -> str:
    if not text:
        return ''
    out = str(text)
    if api_key:
        out = out.replace(api_key, '***')
    return out


def _elapsed_ms(start: float) -> int:
    return max(0, int(round((time.monotonic() - start) * 1000)))


def _fail_result(error: str, error_kind: str, *, latency_ms: int = 0,
                 status: int = 0, api_key: str = '') -> dict:
    return {
        'ok': False,
        'text': '',
        'error': _redact(error, api_key),
        'latency_ms': latency_ms,
        'status': status,
        'error_kind': error_kind,
    }


def _send_post(url: str, *, headers: dict, json_body: dict, timeout_s: int,
               proxies: dict | None):
    req_kwargs: dict = {
        'headers': headers,
        'json': json_body,
        'timeout': timeout_s,
    }
    if proxies:
        req_kwargs['proxies'] = proxies

    if requests.post is not _requests_api.post:
        return requests.post(url, **req_kwargs)
    if requests.request is not _requests_api.request:
        return requests.request('POST', url, **req_kwargs)
    return requests.post(url, **req_kwargs)


def _build_request(settings: dict, messages: list[dict]) -> tuple[str, dict, dict, str]:
    protocol = str(settings.get('protocol') or '').strip()
    info = llm_settings.PROTOCOLS[protocol]
    kind = info['kind']
    base_url = str(settings.get('base_url') or '').strip().rstrip('/')
    model = str(settings.get('model') or '').strip()
    api_key = str(settings.get('api_key') or '').strip()
    temperature = float(
        settings.get('temperature')
        if settings.get('temperature') is not None
        else llm_settings.DEFAULT_TEMPERATURE
    )

    if kind == 'openai':
        url = f'{base_url}/chat/completions'
        headers = {'Content-Type': 'application/json'}
        if api_key:
            headers['Authorization'] = f'Bearer {api_key}'
        body = {
            'model': model,
            'messages': list(messages),
            'temperature': temperature,
            'max_tokens': 256,
            'stream': False,
        }
        return url, headers, body, kind

    if kind == 'anthropic':
        url = f'{base_url}/v1/messages'
        headers = {
            'Content-Type': 'application/json',
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01',
        }
        system_parts = []
        chat_messages = []
        for msg in messages:
            role = str(msg.get('role') or 'user')
            content = str(msg.get('content') or '')
            if role == 'system':
                if content:
                    system_parts.append(content)
            else:
                chat_messages.append({'role': role, 'content': content})
        body: dict = {
            'model': model,
            'max_tokens': 256,
            'temperature': temperature,
            'messages': chat_messages,
        }
        if system_parts:
            body['system'] = '\n\n'.join(system_parts)
        return url, headers, body, kind

    # Gemini
    url = f'{base_url}/models/{model}:generateContent'
    headers = {
        'Content-Type': 'application/json',
        'x-goog-api-key': api_key,
    }
    text_parts = []
    for msg in messages:
        content = str(msg.get('content') or '').strip()
        if content:
            text_parts.append(content)
    body = {
        'contents': [
            {
                'role': 'user',
                'parts': [{'text': '\n\n'.join(text_parts)}],
            }
        ],
        'generationConfig': {
            'temperature': temperature,
            'maxOutputTokens': 256,
        },
    }
    return url, headers, body, kind


def _classify_http_error(status: int) -> str:
    if status in (401, 403):
        return 'auth'
    if status == 404:
        return 'not_found'
    if status == 429:
        return 'rate_limit'
    if 500 <= status < 600:
        return 'server'
    return 'bad_response'


def _extract_http_error_message(resp) -> str:
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        data = None
    if isinstance(data, dict):
        err = data.get('error')
        if isinstance(err, dict) and err.get('message'):
            return str(err['message'])[:300]
        if isinstance(err, str) and err:
            return err[:300]
        if data.get('message'):
            return str(data['message'])[:300]
    text = getattr(resp, 'text', None)
    if isinstance(text, str) and text.strip():
        return text.strip()[:200]
    return ''


def _extract_text(kind: str, data: dict) -> str:
    if kind == 'openai':
        choices = data.get('choices')
        if not isinstance(choices, list) or not choices:
            raise ValueError('响应缺少 choices')
        msg = choices[0].get('message') if isinstance(choices[0], dict) else None
        if not isinstance(msg, dict) or not isinstance(msg.get('content'), str):
            raise ValueError('响应缺少 message.content')
        return msg['content'].strip()

    if kind == 'anthropic':
        blocks = data.get('content')
        if not isinstance(blocks, list) or not blocks:
            raise ValueError('响应缺少 content')
        parts = []
        for block in blocks:
            if isinstance(block, dict) and isinstance(block.get('text'), str):
                parts.append(block['text'])
        text = ''.join(parts).strip()
        if not text:
            raise ValueError('响应 content 无文本')
        return text

    # Gemini
    candidates = data.get('candidates')
    if not isinstance(candidates, list) or not candidates:
        raise ValueError('响应缺少 candidates')
    content = candidates[0].get('content') if isinstance(candidates[0], dict) else None
    parts = content.get('parts') if isinstance(content, dict) else None
    if not isinstance(parts, list) or not parts:
        raise ValueError('响应缺少 content.parts')
    texts = [p['text'] for p in parts if isinstance(p, dict) and isinstance(p.get('text'), str)]
    text = ''.join(texts).strip()
    if not text:
        raise ValueError('响应 parts 无文本')
    return text


def _check_response_size(resp) -> bool:
    content = getattr(resp, 'content', None)
    if isinstance(content, (bytes, bytearray)):
        return len(content) <= MAX_RESPONSE_BYTES
    text = getattr(resp, 'text', None)
    if isinstance(text, str):
        return len(text.encode('utf-8', errors='replace')) <= MAX_RESPONSE_BYTES
    return True


def chat(settings: dict, messages: list[dict], proxies: dict | None = None) -> dict:
    """发送非流式对话请求并返回统一结果字典。"""
    if not isinstance(settings, dict):
        return _fail_result('LLM 配置无效', 'config')

    api_key = str(settings.get('api_key') or '').strip()
    protocol = str(settings.get('protocol') or '').strip()
    if protocol not in llm_settings.PROTOCOLS:
        return _fail_result(f'不支持的协议: {protocol}', 'config', api_key=api_key)

    base_url = str(settings.get('base_url') or '').strip()
    if not base_url:
        return _fail_result('未配置 Base URL', 'config', api_key=api_key)
    parsed = urlparse(base_url)
    if parsed.scheme.lower() not in ('http', 'https') or not parsed.hostname:
        return _fail_result('Base URL 格式无效', 'config', api_key=api_key)

    model = str(settings.get('model') or '').strip()
    if not model:
        return _fail_result('未配置模型名称', 'config', api_key=api_key)

    if llm_settings.PROTOCOLS[protocol]['needs_key'] and not api_key:
        return _fail_result('未配置 API Key', 'config', api_key=api_key)

    try:
        timeout_s = int(
            settings.get('timeout_s')
            if settings.get('timeout_s') is not None
            else llm_settings.DEFAULT_TIMEOUT_S
        )
    except (TypeError, ValueError):
        timeout_s = llm_settings.DEFAULT_TIMEOUT_S
    timeout_s = max(llm_settings.MIN_TIMEOUT_S, min(llm_settings.MAX_TIMEOUT_S, timeout_s))

    kind = llm_settings.PROTOCOLS[protocol]['kind']
    if kind == 'openai' and _sdk_allowed():
        try:
            from . import llm_openai_sdk
        except Exception:  # noqa: BLE001 - a broken optional import must not block
            llm_openai_sdk = None
        if llm_openai_sdk is not None and llm_openai_sdk.available():
            try:
                return llm_openai_sdk.chat(settings, messages, proxies=proxies)
            except Exception as exc:  # noqa: BLE001 - never duplicate the request
                return _fail_result(
                    f'OpenAI SDK 适配器异常: {exc}',
                    'config',
                    api_key=api_key,
                )

    url, headers, body, kind = _build_request(settings, messages)
    start = time.monotonic()

    try:
        resp = _send_post(
            url, headers=headers, json_body=body, timeout_s=timeout_s, proxies=proxies
        )
    except (requests.Timeout, TimeoutError) as exc:
        return _fail_result(
            f'请求超时: {exc}', 'timeout', latency_ms=_elapsed_ms(start), api_key=api_key
        )
    except requests.RequestException as exc:
        return _fail_result(
            f'网络错误: {exc}', 'network', latency_ms=_elapsed_ms(start), api_key=api_key
        )
    except OSError as exc:
        return _fail_result(
            f'连接失败: {exc}', 'network', latency_ms=_elapsed_ms(start), api_key=api_key
        )

    latency_ms = _elapsed_ms(start)
    status = int(getattr(resp, 'status_code', 0) or 0)

    if not _check_response_size(resp):
        return _fail_result(
            '响应体超过 256 KB 上限',
            'bad_response',
            latency_ms=latency_ms,
            status=status,
            api_key=api_key,
        )

    if status < 200 or status >= 300:
        detail = _extract_http_error_message(resp)
        err_msg = f'HTTP {status}: {detail}' if detail else f'HTTP {status}'
        return _fail_result(
            err_msg,
            _classify_http_error(status),
            latency_ms=latency_ms,
            status=status,
            api_key=api_key,
        )

    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return _fail_result(
            f'响应 JSON 解析失败: {exc}',
            'bad_response',
            latency_ms=latency_ms,
            status=status,
            api_key=api_key,
        )

    if not isinstance(data, dict):
        return _fail_result(
            '响应 JSON 不是对象',
            'bad_response',
            latency_ms=latency_ms,
            status=status,
            api_key=api_key,
        )

    try:
        text = _extract_text(kind, data)
    except ValueError as exc:
        return _fail_result(
            str(exc),
            'bad_response',
            latency_ms=latency_ms,
            status=status,
            api_key=api_key,
        )

    return {
        'ok': True,
        'text': text,
        'error': '',
        'latency_ms': latency_ms,
        'status': status,
        'error_kind': '',
    }


def test_connection(settings: dict, proxies: dict | None = None) -> dict:
    """发送一条最小探活消息，返回连通性与延迟摘要。"""
    model = str((settings or {}).get('model') or '').strip() if isinstance(settings, dict) else ''
    res = chat(
        settings,
        [{'role': 'user', 'content': '只回复 OK'}],
        proxies=proxies,
    )
    return {
        'ok': res['ok'],
        'latency_ms': res['latency_ms'],
        'model': model,
        'error': res['error'],
        'error_kind': res['error_kind'],
    }

"""Thin adapter around the official OpenAI Python SDK.

Only OpenAI-compatible protocols (``kind == 'openai'``) are routed here:
OpenAI, DeepSeek, OpenRouter, Ollama and custom OpenAI-compatible endpoints.
Anthropic and Gemini keep the dependency-free ``requests`` implementation.

The adapter is optional.  If the SDK is not installed, ``available()`` returns
False and ``llm_client.chat`` falls back to the existing HTTP client.  Set
``JHP_LLM_SDK=0`` to force the fallback even when the SDK is installed.

No configuration is read from disk here and no API key is ever logged or
returned in an error message.
"""

from __future__ import annotations

import contextlib
import time

from . import llm_settings

MAX_RESPONSE_BYTES = llm_settings.MAX_RESPONSE_BYTES


def available() -> bool:
    """Return True when the official SDK can be imported."""
    try:
        import openai  # noqa: F401
        return True
    except Exception:  # noqa: BLE001 - any import failure means "not available"
        return False


def _redact(text: str, api_key: str) -> str:
    out = str(text or '')
    if api_key:
        out = out.replace(api_key, '***')
    return out


def _fail(error: str, error_kind: str, *, latency_ms: int = 0,
          status: int = 0, api_key: str = '') -> dict:
    return {
        'ok': False,
        'text': '',
        'error': _redact(error, api_key),
        'latency_ms': max(0, int(latency_ms)),
        'status': int(status or 0),
        'error_kind': error_kind,
    }


def _classify_exception(exc: Exception) -> tuple[str, int]:
    """Map SDK exceptions without importing SDK classes.

    Class-name matching keeps this adapter working across openai-python
    versions while still using ``status_code`` when the SDK exposes it.
    """
    status = 0
    try:
        status = int(getattr(exc, 'status_code', 0) or 0)
    except (TypeError, ValueError):
        status = 0
    name = type(exc).__name__.lower()
    if status:
        return llm_settings.classify_status(status), status
    if 'timeout' in name:
        return 'timeout', 0
    if 'connection' in name or 'connecterror' in name:
        return 'network', 0
    return 'bad_response', 0


def _error_detail(exc: Exception) -> str:
    body = getattr(exc, 'body', None)
    if isinstance(body, dict):
        err = body.get('error')
        if isinstance(err, dict) and err.get('message'):
            return str(err['message'])[:300]
        if isinstance(err, str) and err:
            return err[:300]
        if body.get('message'):
            return str(body['message'])[:300]
    return str(exc or '')[:300]


def _make_client(settings: dict, timeout_s: int, proxies: dict | None):
    """Build an OpenAI client; kept separate so tests can replace it."""
    import httpx
    from openai import OpenAI

    api_key = str(settings.get('api_key') or '').strip() or 'not-needed'
    kwargs: dict = {
        'api_key': api_key,
        'base_url': str(settings.get('base_url') or '').strip().rstrip('/'),
        'timeout': timeout_s,
        # The Agent/UI owns retries.  Hidden SDK retries would triple a slow
        # request and make the latency/cache metrics lie.
        'max_retries': 0,
    }
    proxy = ''
    if isinstance(proxies, dict):
        proxy = str(proxies.get('https') or proxies.get('http') or '').strip()
    if proxy:
        try:
            http_client = httpx.Client(proxy=proxy, timeout=timeout_s)
        except TypeError:  # httpx < 0.26 used ``proxies=``
            http_client = httpx.Client(proxies=proxy, timeout=timeout_s)
        kwargs['http_client'] = http_client
    return OpenAI(**kwargs)


def _extract_message_text(resp) -> str:
    """Read ``choices[0].message.content`` from models or plain dicts."""
    choices = resp.get('choices') if isinstance(resp, dict) else getattr(resp, 'choices', None)
    if not isinstance(choices, list) or not choices:
        return ''
    choice = choices[0]
    message = choice.get('message') if isinstance(choice, dict) else getattr(choice, 'message', None)
    if isinstance(message, dict):
        content = message.get('content')
    else:
        content = getattr(message, 'content', None)
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get('text') or ''))
            else:
                parts.append(str(getattr(part, 'text', '') or ''))
        return ''.join(parts).strip()
    return str(content or '').strip()


def chat(settings: dict, messages: list[dict],
         proxies: dict | None = None) -> dict:
    """Send one non-streaming chat request through the official SDK."""
    if not isinstance(settings, dict):
        return _fail('LLM 配置无效', 'config')

    protocol = str(settings.get('protocol') or '').strip()
    info = llm_settings.PROTOCOLS.get(protocol)
    if not info or info.get('kind') != 'openai':
        return _fail(f'OpenAI SDK 不支持协议: {protocol}', 'config')

    base_url = str(settings.get('base_url') or '').strip()
    model = str(settings.get('model') or '').strip()
    api_key = str(settings.get('api_key') or '').strip()
    if not base_url:
        return _fail('未配置 Base URL', 'config', api_key=api_key)
    if not model:
        return _fail('未配置模型名称', 'config', api_key=api_key)
    if info.get('needs_key') and not api_key:
        return _fail('未配置 API Key', 'config', api_key=api_key)

    timeout_s = llm_settings.clamp_timeout(settings.get('timeout_s'))

    temperature = llm_settings.clamp_temperature(settings.get('temperature'))

    start = time.monotonic()
    client = None
    try:
        client = _make_client(settings, timeout_s, proxies)
        resp = client.chat.completions.create(
            model=model,
            messages=list(messages),
            temperature=temperature,
            max_tokens=256,
            stream=False,
        )
        latency_ms = round((time.monotonic() - start) * 1000)

        choices = resp.get('choices') if isinstance(resp, dict) else getattr(resp, 'choices', None)
        if not isinstance(choices, list) or not choices:
            return _fail('响应缺少 choices', 'bad_response',
                         latency_ms=latency_ms, api_key=api_key)
        text = _extract_message_text(resp)
        if not text:
            return _fail('响应缺少 message.content', 'bad_response',
                         latency_ms=latency_ms, api_key=api_key)
        if len(text.encode('utf-8', errors='replace')) > MAX_RESPONSE_BYTES:
            return _fail('响应体超过 256 KB 上限', 'bad_response',
                         latency_ms=latency_ms, api_key=api_key)
        return {
            'ok': True,
            'text': text,
            'error': '',
            'latency_ms': latency_ms,
            'status': 200,
            'error_kind': '',
        }
    except Exception as exc:  # noqa: BLE001 - convert every SDK failure
        latency_ms = round((time.monotonic() - start) * 1000)
        kind, status = _classify_exception(exc)
        detail = _error_detail(exc)
        message = f'HTTP {status}: {detail}' if status else (detail or 'OpenAI SDK 调用失败')
        return _fail(message, kind, latency_ms=latency_ms,
                     status=status, api_key=api_key)
    finally:
        if client is not None:
            # Closing must never mask the result we already built.
            with contextlib.suppress(Exception):
                client.close()

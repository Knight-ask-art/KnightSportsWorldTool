"""LLM 配置存储、校验与候选脱敏 Prompt 构造。

API Key 使用 Windows DPAPI（``credentials.seal``）封存，绝不落盘明文，
也不在 ``load()`` 或前端接口中回传。非 Windows 平台保存 Key 直接失败，
不降级为明文；不带 Key 的常规字段仍可正常保存。
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
from urllib.parse import urlparse

from . import credentials

SCHEMA_VERSION = 1
FILENAME = 'llm_settings.json'
_KEY_ENTROPY = b'haoxiangpao.llm.v1'

DEFAULT_TIMEOUT_S = 30
MIN_TIMEOUT_S = 5
MAX_TIMEOUT_S = 120
DEFAULT_TEMPERATURE = 0.2
MAX_URL_LEN = 500
MAX_MODEL_LEN = 200

PROTOCOLS: dict[str, dict] = {
    'openai': {
        'label': 'OpenAI 兼容',
        'base_url': 'https://api.openai.com/v1',
        'auth': 'bearer',
        'kind': 'openai',
        'needs_key': True,
        'default_model': 'gpt-4o-mini',
    },
    'deepseek': {
        'label': 'DeepSeek',
        'base_url': 'https://api.deepseek.com/v1',
        'auth': 'bearer',
        'kind': 'openai',
        'needs_key': True,
        'default_model': 'deepseek-chat',
    },
    'openrouter': {
        'label': 'OpenRouter',
        'base_url': 'https://openrouter.ai/api/v1',
        'auth': 'bearer',
        'kind': 'openai',
        'needs_key': True,
        'default_model': 'openai/gpt-4o-mini',
    },
    'anthropic': {
        'label': 'Anthropic',
        'base_url': 'https://api.anthropic.com',
        'auth': 'x-api-key',
        'kind': 'anthropic',
        'needs_key': True,
        'default_model': 'claude-sonnet-4-5',
    },
    'gemini': {
        'label': 'Google Gemini',
        'base_url': 'https://generativelanguage.googleapis.com/v1beta',
        'auth': 'x-goog-api-key',
        'kind': 'gemini',
        'needs_key': True,
        'default_model': 'gemini-2.0-flash',
    },
    'ollama': {
        'label': 'Ollama 本地',
        'base_url': 'http://127.0.0.1:11434/v1',
        'auth': 'none',
        'kind': 'openai',
        'needs_key': False,
        'default_model': 'qwen2.5:7b',
    },
    'custom': {
        'label': '自定义（OpenAI 兼容）',
        'base_url': '',
        'auth': 'bearer',
        'kind': 'openai',
        'needs_key': False,
        'default_model': '',
    },
}


def presets() -> list[dict]:
    """返回前端渲染协议下拉列表所需的单一来源预设。"""
    return [
        {
            'id': pid,
            'label': info['label'],
            'base_url': info['base_url'],
            'default_model': info.get('default_model', ''),
            'needs_key': bool(info['needs_key']),
        }
        for pid, info in PROTOCOLS.items()
    ]


def _path(store_dir: str) -> str:
    return os.path.join(os.path.abspath(store_dir), FILENAME)


def _defaults() -> dict:
    return {
        'version': SCHEMA_VERSION,
        'enabled': False,
        'protocol': 'openai',
        'base_url': PROTOCOLS['openai']['base_url'],
        'model': PROTOCOLS['openai']['default_model'],
        'has_key': False,
        'temperature': DEFAULT_TEMPERATURE,
        'timeout_s': DEFAULT_TIMEOUT_S,
        'updated_at_ms': 0,
        'last_test': None,
    }


def _clamp_temperature(value, *, strict: bool = False) -> float:
    if value is None:
        return DEFAULT_TEMPERATURE
    if isinstance(value, bool):
        if strict:
            raise ValueError('temperature 必须是 0 到 1 之间的数字')
        return DEFAULT_TEMPERATURE
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        if strict:
            raise ValueError('temperature 必须是 0 到 1 之间的数字') from exc
        return DEFAULT_TEMPERATURE
    if math.isnan(num) or math.isinf(num):
        if strict:
            raise ValueError('temperature 必须是有效数字')
        return DEFAULT_TEMPERATURE
    return max(0.0, min(1.0, num))


def _clamp_timeout(value, *, strict: bool = False) -> int:
    if value is None:
        return DEFAULT_TIMEOUT_S
    if isinstance(value, bool):
        if strict:
            raise ValueError('timeout_s 必须是整数')
        return DEFAULT_TIMEOUT_S
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        if strict:
            raise ValueError('timeout_s 必须是数字') from exc
        return DEFAULT_TIMEOUT_S
    if math.isnan(num) or math.isinf(num):
        if strict:
            raise ValueError('timeout_s 必须是有效数字')
        return DEFAULT_TIMEOUT_S
    return max(MIN_TIMEOUT_S, min(MAX_TIMEOUT_S, int(round(num))))


def _validate_base_url(value: object, protocol: str) -> str:
    if value is None:
        return PROTOCOLS[protocol]['base_url']
    text = str(value)
    if any(ch.isspace() for ch in text):
        raise ValueError('Base URL 不能包含空白字符')
    if len(text) > MAX_URL_LEN:
        raise ValueError(f'Base URL 长度不能超过 {MAX_URL_LEN} 字符')
    if text == '':
        if protocol == 'custom':
            return ''
        return PROTOCOLS[protocol]['base_url']
    parsed = urlparse(text)
    if parsed.scheme.lower() not in ('http', 'https') or not parsed.hostname:
        raise ValueError('Base URL 必须是以 http 或 https 开头的有效地址')
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError('Base URL 端口号无效') from exc
    return text.rstrip('/')


def _sanitise_last_test(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    return {
        'ok': bool(raw.get('ok')),
        'at_ms': int(raw.get('at_ms') or 0),
        'latency_ms': int(raw.get('latency_ms') or 0),
        'error': str(raw.get('error') or '')[:300],
    }


def _read_raw(store_dir: str) -> dict | None:
    if not store_dir:
        return None
    try:
        with open(_path(store_dir), encoding='utf-8') as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    if doc.get('version') != SCHEMA_VERSION:
        return None
    if doc.get('protocol') not in PROTOCOLS:
        return None
    return doc


def load(store_dir: str) -> dict:
    """读取 LLM 设置（含 ``has_key``，绝不含 ``api_key``）；损坏时返回默认值。"""
    doc = _read_raw(store_dir)
    if doc is None:
        return _defaults()

    protocol = doc['protocol']
    try:
        base_url = _validate_base_url(doc.get('base_url'), protocol)
    except ValueError:
        base_url = PROTOCOLS[protocol]['base_url']

    model_raw = doc.get('model')
    if model_raw is None:
        model = PROTOCOLS[protocol]['default_model']
    else:
        model = str(model_raw).strip()[:MAX_MODEL_LEN]

    sealed = str(doc.get('api_key') or '').strip()
    try:
        updated_at_ms = int(doc.get('updated_at_ms') or 0)
    except (TypeError, ValueError):
        updated_at_ms = 0

    return {
        'version': SCHEMA_VERSION,
        'enabled': bool(doc.get('enabled', False)),
        'protocol': protocol,
        'base_url': base_url,
        'model': model,
        'has_key': bool(sealed),
        'temperature': _clamp_temperature(doc.get('temperature')),
        'timeout_s': _clamp_timeout(doc.get('timeout_s')),
        'updated_at_ms': updated_at_ms,
        'last_test': _sanitise_last_test(doc.get('last_test')),
    }


def save(store_dir: str, payload: dict) -> dict:
    """校验并原子保存 LLM 设置；返回 ``load(store_dir)``。"""
    if not store_dir:
        raise ValueError('store_dir 不能为空')
    if not isinstance(payload, dict):
        raise ValueError('设置参数必须是对象')

    existing_raw = _read_raw(store_dir)
    existing = load(store_dir)

    if 'protocol' in payload:
        protocol = str(payload.get('protocol') or '').strip()
        if protocol not in PROTOCOLS:
            raise ValueError(f'不支持的 LLM 协议: {protocol}')
    else:
        protocol = existing['protocol']

    protocol_changed = protocol != existing['protocol']

    if 'base_url' in payload:
        base_url = _validate_base_url(payload.get('base_url'), protocol)
    elif protocol_changed:
        base_url = PROTOCOLS[protocol]['base_url']
    else:
        base_url = existing['base_url']

    if 'model' in payload:
        model_val = payload.get('model')
        model = '' if model_val is None else str(model_val).strip()[:MAX_MODEL_LEN]
    elif protocol_changed:
        model = PROTOCOLS[protocol]['default_model']
    else:
        model = existing['model']

    if 'enabled' in payload:
        enabled = bool(payload.get('enabled'))
    else:
        enabled = existing['enabled']

    if 'temperature' in payload:
        temperature = _clamp_temperature(payload.get('temperature'), strict=True)
    else:
        temperature = existing['temperature']

    if 'timeout_s' in payload:
        timeout_s = _clamp_timeout(payload.get('timeout_s'), strict=True)
    else:
        timeout_s = existing['timeout_s']

    clear_key = bool(payload.get('clear_key'))
    existing_sealed = (
        str(existing_raw.get('api_key') or '').strip() if existing_raw else ''
    )

    if clear_key:
        sealed_key = ''
    else:
        raw_key = payload.get('api_key')
        new_key = '' if raw_key is None else str(raw_key).strip()
        if new_key:
            sealed_key = credentials.seal(new_key, entropy=_KEY_ENTROPY)
        else:
            sealed_key = existing_sealed

    last_test = (
        _sanitise_last_test(payload.get('last_test'))
        if 'last_test' in payload
        else existing['last_test']
    )

    doc = {
        'version': SCHEMA_VERSION,
        'enabled': enabled,
        'protocol': protocol,
        'base_url': base_url,
        'model': model,
        'api_key': sealed_key,
        'has_key': bool(sealed_key),
        'temperature': temperature,
        'timeout_s': timeout_s,
        'updated_at_ms': int(time.time() * 1000),
        'last_test': last_test,
    }

    folder = os.path.abspath(store_dir)
    os.makedirs(folder, exist_ok=True)
    target = _path(folder)
    fd, tmp = tempfile.mkstemp(prefix='.llm_settings-', suffix='.tmp', dir=folder)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(doc, f, ensure_ascii=False, separators=(',', ':'))
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass

    return load(store_dir)


def clear(store_dir: str) -> bool:
    """删除本地 LLM 设置文件与密钥。"""
    if not store_dir:
        return False
    try:
        os.unlink(_path(store_dir))
        return True
    except FileNotFoundError:
        return False


def effective(store_dir: str) -> dict | None:
    """返回含明文 ``api_key`` 的运行时配置；未启用或缺少必需 Key 时返回 ``None``。"""
    doc = _read_raw(store_dir)
    if doc is None or not bool(doc.get('enabled', False)):
        return None

    loaded = load(store_dir)
    protocol = loaded['protocol']
    needs_key = bool(PROTOCOLS[protocol]['needs_key'])
    sealed = str(doc.get('api_key') or '').strip()

    if sealed:
        try:
            api_key = credentials.unseal(sealed, entropy=_KEY_ENTROPY)
        except (credentials.CredentialError, ValueError):
            return None
        if not api_key:
            if needs_key:
                return None
            api_key = ''
    else:
        if needs_key:
            return None
        api_key = ''

    loaded['api_key'] = api_key
    return loaded


def build_choice_prompt(candidates: list[dict], target_m: float) -> list[dict]:
    """构造仅含聚合统计指标的脱敏选路 Prompt（不含任何坐标或账号信息）。"""
    target_rounded = round(float(target_m or 0))
    system_content = (
        '你是跑步路线选择助手。请根据各候选路线的统计指标选出最合适的一条。'
        '只回复一个候选序号数字（例如 1）；若所有候选都不合适，回复 0。'
        '不要输出解释、单位或多余文字。'
    )

    lines = [f'目标距离：{target_rounded} 米', '候选路线列表：']
    for idx, cand in enumerate(candidates or [], 1):
        if not isinstance(cand, dict):
            continue
        lap_m = round(float(cand.get('lap_m') or 0))
        laps = int(cand.get('laps') or 0)
        samples = int(cand.get('samples') or 0)
        missed_raw = cand.get('missed')
        if isinstance(missed_raw, (list, tuple, set)):
            missed_n = len(missed_raw)
        elif isinstance(missed_raw, (int, float)) and not isinstance(missed_raw, bool):
            missed_n = int(missed_raw)
        else:
            missed_n = 0
        fence_ok = bool(cand.get('fence_ok', True))
        detour_m = round(float(cand.get('detour_m') or 0))
        score = max(0.0, min(1.0, float(cand.get('score') or 0.0)))
        coverage = max(0.0, min(1.0, float(cand.get('coverage') or 0.0)))
        fence_label = '围栏内' if fence_ok else '超围栏'
        lines.append(
            f'{idx}. 单圈 {lap_m} 米，需 {laps} 圈，样本 {samples} 次，'
            f'漏点 {missed_n} 个，{fence_label}，绕行 {detour_m} 米，'
            f'点位覆盖 {coverage * 100:.0f}%，综合评分 {score:.2f}'
        )
    lines.append('请仅回复最佳候选的序号数字（如 1）；没有合适路线回复 0。')

    return [
        {'role': 'system', 'content': system_content},
        {'role': 'user', 'content': '\n'.join(lines)},
    ]


_EXPLICIT_CHOICE_RE = re.compile(
    r'(?:第\s*(?<!-)([一二两三四五六七八九十\d]+)\s*[条个]?|'
    r'(?:候选(?:路线)?|选项|选择|choice|option)\s*[:#：]?\s*'
    r'(?<!-)([一二两三四五六七八九十\d]+))',
    re.IGNORECASE,
)
_BARE_INT_RE = re.compile(r'^\s*([一二两三四五六七八九十]+|\d+)\s*$')
_LEADING_INT_RE = re.compile(
    r'^\s*([一二两三四五六七八九十]+|\d+)\s*[.、:：\s]')
_ANY_NUMBER_RE = re.compile(r'(?<![\w.-])(\d+)(?!\.\d)')

_CN_CHOICE_DIGITS = {
    '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
    '五': 5, '六': 6, '七': 7, '八': 8, '九': 9,
}


def _choice_index(token: str) -> int | None:
    token = str(token or '').strip()
    if not token:
        return None
    if token.isdigit():
        return int(token)
    if token in _CN_CHOICE_DIGITS:
        return _CN_CHOICE_DIGITS[token]
    return None


def _pick_candidate_id(idx: int, candidates: list[dict]) -> str | None:
    if 1 <= idx <= len(candidates):
        cand = candidates[idx - 1]
        if isinstance(cand, dict):
            tid = cand.get('template_id')
            return str(tid) if tid else None
    return None


def _parse_json_choice(text: str) -> int | None:
    stripped = text.strip()
    fenced = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', stripped, re.DOTALL | re.I)
    raw_json = fenced.group(1) if fenced else None
    if raw_json is None:
        brace = re.search(r'\{.*\}', stripped, re.DOTALL)
        if brace:
            raw_json = brace.group(0)
    if raw_json is None:
        return None
    try:
        obj = json.loads(raw_json)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    for key in ('choice', 'index', 'candidate', 'selected', 'number'):
        if key in obj:
            val = obj[key]
            if isinstance(val, bool):
                return -1
            idx = _choice_index(str(val))
            return idx if idx is not None else -1
    return None


def parse_choice(text: str, candidates: list[dict]) -> str | None:
    """从模型文本回复中解析候选序号并返回对应的 ``template_id``。"""
    if not text or not candidates:
        return None
    s = str(text).strip()
    if not s:
        return None

    json_idx = _parse_json_choice(s)
    if json_idx is not None:
        return _pick_candidate_id(json_idx, candidates)

    m = _BARE_INT_RE.match(s)
    if m:
        idx = _choice_index(m.group(1))
        return _pick_candidate_id(idx, candidates) if idx is not None else None

    m = _EXPLICIT_CHOICE_RE.search(s)
    if m:
        idx = _choice_index(m.group(1) or m.group(2))
        return _pick_candidate_id(idx, candidates) if idx is not None else None

    m = _LEADING_INT_RE.match(s)
    if m:
        idx = _choice_index(m.group(1))
        return _pick_candidate_id(idx, candidates) if idx is not None else None

    nums = _ANY_NUMBER_RE.findall(s)
    if len(nums) == 1 and '-' not in s:
        return _pick_candidate_id(int(nums[0]), candidates)

    return None

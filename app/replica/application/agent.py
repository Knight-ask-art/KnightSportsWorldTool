"""Agent 跑步规划状态机与会话编排（纯本地确定性逻辑 + 线程编排）。

不直接发 HTTP 请求、不 import bridge_api、永不调用 submit。
所有状态变更以 RLock 和 generation 计数器保护，旧线程取消后立即停止写入。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
import uuid
from typing import Any, Callable

from ..domain import safety as S

STATES = (
    'idle',
    'collect',
    'learn',
    'suggest',
    'adapt',
    'preview',
    'audit',
    'wait_confirm',
    'confirmed',
    'cancelled',
    'error',
)

TRANSIENT_STATES = ('collect', 'learn', 'suggest', 'adapt', 'preview', 'audit')

REQUIRED_TOOLS = (
    'route_memory_status',
    'route_memory_learn',
    'route_memory_suggest',
    'route_memory_apply',
    'preview_route',
    'audit_run',
)

MAX_LOG_ENTRIES = 200
MIN_KM = 0.5
MAX_KM = 20.0

_CN_DIGITS = {
    '零': 0,
    '〇': 0,
    '一': 1,
    '二': 2,
    '两': 2,
    '三': 3,
    '四': 4,
    '五': 5,
    '六': 6,
    '七': 7,
    '八': 8,
    '九': 9,
}

_WHITELIST_INT_KEYS = frozenset({
    'count', 'samples', 'usable', 'seen', 'skipped', 'templates',
    'candidates', 'points', 'waypoints', 'laps', 'missed', 'passed',
    'failed', 'warned', 'index', 'latency_ms', 'run_area_id',
    'tool_calls', 'tool_retries', 'cache_hits', 'cache_misses',
    'llm_calls', 'llm_cache_hits', 'llm_cache_misses',
    'attempts', 'preferred',
})
_WHITELIST_METRE_KEYS = frozenset({
    'lap_m', 'target_m', 'detour_m', 'avg_distance_m',
})
_WHITELIST_FLOAT_KEYS = frozenset({'km', 'score', 'coverage'})
_WHITELIST_BOOL_KEYS = frozenset({
    'ok', 'closed', 'cached', 'used', 'audit_ok', 'fence_ok', 'reversed', 'force',
    'retried',
})
_WHITELIST_STR_KEYS = frozenset({
    'template_id', 'selected', 'status', 'phase', 'verdict', 'model',
    'fallback', 'prompt_sha256', 'error', 'reason',
})

_COORD_DECIMAL_RE = re.compile(r'-?\d{1,3}\.\d{4,}')
_KV_SECRET_RE = re.compile(
    r'(?i)(uid|unid|nickname|account|api_key|apikey|key|token|secret)\s*([=:])\s*([^\s&,;"\']+)'
)

_DIST_ARABIC_KM_RE = re.compile(
    # Do not use \b here: Python treats CJK characters as word characters, so
    # "3km路线" would fail to match.  Only reject a following digit/dot so the
    # number itself stays unambiguous.
    r'(?<!\d)(\d+(?:\.\d+)?)\s*(?:公里|千米|km(?![A-Za-z0-9]))',
    re.IGNORECASE,
)
_DIST_ARABIC_M_RE = re.compile(
    r'(?<!\d)(\d+(?:\.\d+)?)\s*(?:米|m(?![A-Za-z0-9]))',
    re.IGNORECASE,
)
_DIST_BARE_RUN_RE = re.compile(r'跑\s*(\d+(?:\.\d+)?)\s*$')
_DIST_CN_HALF_RE = re.compile(r'(?:跑\s*)?半\s*(?:公里|千米|km)', re.IGNORECASE)
_DIST_CN_WITH_HALF_RE = re.compile(
    r'([一二两三四五六七八九十]{1,3}|[1-9]\d?)\s*(?:个?半\s*(?:公里|千米|km)|(?:公里|千米|km)\s*半)',
    re.IGNORECASE,
)
_DIST_CN_DECIMAL_RE = re.compile(
    r'([一二两三四五六七八九十]{1,3})点([零〇一二三四五六七八九五\d])\s*(?:公里|千米|km)',
    re.IGNORECASE,
)
_DIST_CN_INT_RE = re.compile(
    r'([一二两三四五六七八九十]{1,3})\s*(?:公里|千米|km)',
    re.IGNORECASE,
)
_SELECT_N_RE = re.compile(
    r'(?:用|选|选择)?\s*第\s*([一二两三四五六七八九十\d]+)\s*[条个]'
)

# A phrase only counts as an intent when it is not negated.  Chinese negation
# is short and usually sits directly in front of the verb ("不要取消").
_NEGATION_PREFIXES = (
    '不', '别', '不要', '不用', '不想', '先不', '先别', '别再',
    '不要再', '不用再', '不太想', '先不要',
)


def _intent_present(text: str, *phrases: str) -> bool:
    """Return True when at least one phrase occurs without a negation prefix."""
    msg = str(text or '')
    for phrase in phrases:
        start = 0
        while True:
            idx = msg.find(phrase, start)
            if idx < 0:
                break
            prefix = msg[max(0, idx - 3):idx]
            if not any(prefix.endswith(neg) for neg in _NEGATION_PREFIXES):
                return True
            start = idx + len(phrase)
    return False


# "算了" is also a substring of "打算了解"; only a standalone filler counts.
_CANCEL_FILLER_RE = re.compile(r'(?:^|[，,。！!\s])算了(?:$|[，,。！!\s])')
_CANCEL_ACTION_PHRASES = (
    '换一条', '下一条', '换一个', '反向', '反着跑',
    '重新学习', '再学一次', '确认', '就这条',
)


def _wants_cancel(text: str, new_km: float | None) -> bool:
    """Cancel is explicit, but "算了，跑3公里" is a new target, not a cancel."""
    if _intent_present(text, '取消'):
        return True
    if not _CANCEL_FILLER_RE.search(str(text or '')):
        return False
    if new_km is not None:
        return False
    if any(_intent_present(text, phrase) for phrase in _CANCEL_ACTION_PHRASES):
        return False
    return True


class AgentError(RuntimeError):
    """Agent 会话或工具契约错误。"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _haversine_m(a: list[float] | tuple[float, float],
                 b: list[float] | tuple[float, float]) -> float:
    r = 6371000.0
    lat1, lon1 = math.radians(float(a[0])), math.radians(float(a[1]))
    lat2, lon2 = math.radians(float(b[0])), math.radians(float(b[1]))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = (math.sin(dlat / 2.0) ** 2
         + math.cos(lat1) * math.cos(lat2) * (math.sin(dlon / 2.0) ** 2))
    return 2.0 * r * math.asin(min(1.0, math.sqrt(max(0.0, h))))


def _cn_to_int(token: str) -> int | None:
    token = (token or '').strip()
    if not token:
        return None
    if token.isdigit():
        return int(token)
    if token in _CN_DIGITS:
        return _CN_DIGITS[token]
    if token == '十':
        return 10
    if token.startswith('十') and len(token) == 2 and token[1] in _CN_DIGITS:
        return 10 + _CN_DIGITS[token[1]]
    if len(token) == 2 and token[0] in _CN_DIGITS and token[1] == '十':
        return _CN_DIGITS[token[0]] * 10
    if (len(token) == 3 and token[0] in _CN_DIGITS
            and token[1] == '十' and token[2] in _CN_DIGITS):
        return _CN_DIGITS[token[0]] * 10 + _CN_DIGITS[token[2]]
    return None


def _valid_km(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    if value < MIN_KM or value > MAX_KM:
        return None
    return round(float(value), 2)


def _as_int(value: Any, default: int = 0) -> int:
    """Tool payloads are untrusted; a malformed count must not kill the thread."""
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool) or value is None:
        return default
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return num if math.isfinite(num) else default


def parse_distance_km(text: str) -> float | None:
    """从中文/阿拉伯数字输入解析目标公里数（0.5-20 km），解析失败返回 None。"""
    s = str(text or '').strip()
    if not s:
        return None
    # 不要把“换一条”“用第2条”“半马”误判为公里数
    if '半马' in s:
        return None

    m = _DIST_ARABIC_KM_RE.search(s)
    if m:
        try:
            return _valid_km(float(m.group(1)))
        except ValueError:
            return None

    m = _DIST_CN_DECIMAL_RE.search(s)
    if m:
        whole = _cn_to_int(m.group(1))
        frac_ch = m.group(2)
        frac = int(frac_ch) if frac_ch.isdigit() else _CN_DIGITS.get(frac_ch)
        if whole is not None and frac is not None:
            return _valid_km(whole + frac * 0.1)

    m = _DIST_CN_WITH_HALF_RE.search(s)
    if m:
        whole = _cn_to_int(m.group(1))
        if whole is not None:
            return _valid_km(whole + 0.5)

    if _DIST_CN_HALF_RE.search(s):
        return 0.5

    m = _DIST_CN_INT_RE.search(s)
    if m:
        whole = _cn_to_int(m.group(1))
        if whole is not None:
            return _valid_km(float(whole))

    m = _DIST_ARABIC_M_RE.search(s)
    if m:
        try:
            metres = float(m.group(1))
            if metres >= 500.0:
                return _valid_km(metres / 1000.0)
        except ValueError:
            return None

    m = _DIST_BARE_RUN_RE.search(s)
    if m:
        try:
            return _valid_km(float(m.group(1)))
        except ValueError:
            return None

    # 用户只回了一个数字（例如在“想跑多少公里？”之后回“2.5”或“3”）。
    m = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*', s)
    if m:
        try:
            return _valid_km(float(m.group(1)))
        except ValueError:
            return None
    if s == '半':
        return 0.5

    return None


def _normalise_points(points: Any) -> list[dict]:
    """统一打卡点结构，兼容 lat/lng 与 lat/lon，供 suggest/apply/preview 共用。"""
    out: list[dict] = []
    if not isinstance(points, (list, tuple)):
        return out
    for p in points:
        if not isinstance(p, dict):
            continue
        lat = next((p[k] for k in ('lat', 'glat', 'gLat', 'latitude')
                    if p.get(k) is not None), None)
        lon = next((p[k] for k in ('lon', 'lng', 'glon', 'gLng', 'longitude')
                    if p.get(k) is not None), None)
        if lat is None or lon is None:
            continue
        try:
            flat, flon = float(lat), float(lon)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(flat) and math.isfinite(flon)):
            continue
        if not (-90.0 <= flat <= 90.0 and -180.0 <= flon <= 180.0):
            continue
        if abs(flat) < 1e-9 and abs(flon) < 1e-9:
            continue
        item: dict[str, Any] = {
            'lat': flat,
            'lon': flon,
            'name': str(p.get('name') or p.get('n') or p.get('pointName') or ''),
            'isMustPoint': bool(
                p.get('isMustPoint') if 'isMustPoint' in p else p.get('must')
            ),
        }
        for extra_k in ('isFixed', 'position', 'seq'):
            if extra_k in p and p[extra_k] is not None:
                item[extra_k] = p[extra_k]
        out.append(item)
    return out


def _sanitize_text(text: Any, owner: str = '') -> str:
    s = S.redact_text(str(text or '')).strip()
    if not s:
        return ''
    s = _KV_SECRET_RE.sub(lambda m: f'{m.group(1)}{m.group(2)}***', s)
    if owner:
        if owner in s:
            s = s.replace(owner, '***')
        for part in str(owner).split(':'):
            part = part.strip()
            if part and (len(part) >= 4 or not part.isdigit()):
                s = s.replace(part, '***')
    s = _COORD_DECIMAL_RE.sub('<coord>', s)
    return s[:300]


def _whitelist_data(data: dict | None, owner: str = '') -> dict:
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in data.items():
        if k in _WHITELIST_INT_KEYS:
            if isinstance(v, bool) or v is None:
                continue
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                continue
        elif k in _WHITELIST_METRE_KEYS:
            if isinstance(v, bool) or v is None:
                continue
            try:
                num = float(v)
                if math.isfinite(num):
                    out[k] = int(round(num))
            except (TypeError, ValueError):
                continue
        elif k in _WHITELIST_FLOAT_KEYS:
            if isinstance(v, bool) or v is None:
                continue
            try:
                num = float(v)
                if math.isfinite(num):
                    out[k] = round(num, 2)
            except (TypeError, ValueError):
                continue
        elif k in _WHITELIST_BOOL_KEYS:
            out[k] = bool(v)
        elif k in _WHITELIST_STR_KEYS and v is not None:
            out[k] = _sanitize_text(v, owner=owner)
    return out


def _looks_like_token_error(msg: str) -> bool:
    low = str(msg or '').lower()
    return any(k in low for k in (
        'token', 'unauthorized', '401', '403', '未登录', '重新登录', '失效',
    ))


def _format_tool_error(msg: str, owner: str = '') -> str:
    clean = _sanitize_text(msg, owner=owner) or '工具执行失败'
    if _looks_like_token_error(clean):
        if 'token' not in clean.lower():
            clean = f'token 失效，请重新登录（{clean}）'
        elif '重新登录' not in clean:
            clean = f'{clean}，请重新登录'
    return clean


def _empty_stats() -> dict:
    return {
        'tool_calls': 0,
        'tool_retries': 0,
        'cache_hits': 0,
        'cache_misses': 0,
        'llm_calls': 0,
        'llm_cache_hits': 0,
        'llm_cache_misses': 0,
        'candidates_tried': 0,
    }


def _idle_status(session_id: str = '') -> dict:
    return {
        'session_id': session_id,
        'state': 'idle',
        'goal': {'km': None, 'points': 0, 'run_area_id': None},
        'log': [],
        'candidates': [],
        'selected': '',
        'preview': None,
        'audit': None,
        'llm': {'used': False, 'model': '', 'error': ''},
        'stats': _empty_stats(),
        'error': '',
        'fallback': '',
        'updated_at_ms': 0,
    }


class AgentSession:
    """单个 Agent 规划会话，封装状态机、线程执行、工具编排与脱敏追踪。"""

    def __init__(self, tools: dict, store_dir: str, owner: str):
        if not isinstance(tools, dict):
            raise AgentError('tools 必须是字典')
        for key in REQUIRED_TOOLS:
            fn = tools.get(key)
            if not callable(fn):
                raise AgentError(f'缺少必需工具: {key}')
        self._tools: dict[str, Callable[..., Any]] = dict(tools)
        self.store_dir = str(store_dir or '')
        self.owner = str(owner or '')

        self._lock = threading.RLock()
        self._generation = 0
        self._cancel_event: threading.Event | None = None
        self._thread: threading.Thread | None = None

        self._session_id = 'ag_' + uuid.uuid4().hex[:12]
        self._state = 'idle'
        self._text = ''
        self._raw_km: float | None = None
        self._points: list[dict] = []
        self._fence: Any = None
        self._run_area_id: Any = None
        self._force_learn = False
        self._prefer_index: int | None = None

        self._goal: dict[str, Any] = {'km': None, 'points': 0, 'run_area_id': None}
        self._log: list[dict[str, Any]] = []
        self._candidates: list[dict[str, Any]] = []
        self._selected = ''
        self._waypoints: list[list[float]] = []
        self._closed = True
        self._preview: dict | None = None
        self._audit: dict | None = None
        self._audit_ok = False
        self._preview_args: dict | None = None
        self._llm: dict[str, Any] = {'used': False, 'model': '', 'error': ''}
        self._stats: dict[str, int] = _empty_stats()
        self._error = ''
        self._fallback = ''
        self._updated_at_ms = 0

    def _is_stale(self, gen: int, cancel_event: threading.Event | None) -> bool:
        if gen != self._generation:
            return True
        if cancel_event is not None and cancel_event.is_set():
            return True
        return False

    def _append_log(
        self,
        level: str,
        text: str,
        tool: str = '',
        data: dict | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            'at_ms': _now_ms(),
            'level': str(level or 'info'),
            'text': _sanitize_text(text, owner=self.owner),
            'tool': str(tool or ''),
            'data': _whitelist_data(data, owner=self.owner),
        }
        with self._lock:
            self._log.append(entry)
            if len(self._log) > MAX_LOG_ENTRIES:
                self._log = self._log[-MAX_LOG_ENTRIES:]

    def _bump_stats(self, **fields: int) -> None:
        with self._lock:
            for key, value in fields.items():
                if key in self._stats:
                    self._stats[key] += int(value)

    def _write_trace(
        self,
        state: str,
        tool: str,
        ok: bool,
        detail: dict | None = None,
        prompt_sha256: str | None = None,
    ) -> None:
        if not self.store_dir or not self._session_id:
            return
        folder = os.path.join(self.store_dir, 'agent_runs')
        path = os.path.join(folder, f'{self._session_id}.jsonl')
        record: dict[str, Any] = {
            'at_ms': _now_ms(),
            'state': str(state),
            'tool': str(tool),
            'ok': bool(ok),
            'detail': _whitelist_data(detail, owner=self.owner),
        }
        if prompt_sha256:
            record['prompt_sha256'] = str(prompt_sha256)[:64]
        try:
            os.makedirs(folder, exist_ok=True)
            with open(path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(record, ensure_ascii=False) + '\n')
                f.flush()
        except OSError:
            pass

    def _set_error(
        self,
        gen: int,
        cancel_event: threading.Event | None,
        message: str,
        tool: str = '',
        fallback: str = '',
        detail: dict | None = None,
    ) -> None:
        clean = _format_tool_error(message, owner=self.owner)
        with self._lock:
            if self._is_stale(gen, cancel_event):
                return
            self._state = 'error'
            self._error = clean
            self._fallback = str(fallback or '')
            self._updated_at_ms = _now_ms()
            log_data = dict(detail or {})
            if fallback:
                log_data['fallback'] = fallback
            self._append_log('error', clean, tool=tool, data=log_data)
            trace_detail = dict(log_data)
            trace_detail['error'] = clean
            self._write_trace('error', tool or 'error', False, trace_detail)

    @staticmethod
    def _is_transient_error(res: dict) -> bool:
        """Only retry errors that are plausibly temporary.

        Token/authorization failures are terminal: retrying them just burns
        time and produces a second identical error.
        """
        msg = str(res.get('message') or res.get('error') or '').lower()
        if any(k in msg for k in ('token', 'unauthorized', '401', '403',
                                  '未登录', '重新登录', '失效')):
            return False
        return any(k in msg for k in (
            '超时', 'timeout', 'timed out', '网络', 'network', '连接',
            'connection', '503', '502', '504', '429', '繁忙', 'busy',
            'temporarily', '稍后重试',
        ))

    def _call_tool_once(self, name: str, *args, **kwargs) -> dict:
        fn = self._tools.get(name)
        if not callable(fn):
            return {'code': 500, 'message': f'工具不可用: {name}', 'data': None}
        try:
            res = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            return {'code': 500, 'message': str(exc) or f'{name} 调用异常', 'data': None}
        if not isinstance(res, dict):
            return {'code': 500, 'message': f'{name} 返回格式无效', 'data': None}
        return res

    def _call_tool(self, name: str, *args, **kwargs) -> dict:
        """Call one bridge tool, retrying a single transient failure."""
        for attempt in (1, 2):
            res = self._call_tool_once(name, *args, **kwargs)
            self._bump_stats(tool_calls=1)
            if res.get('code') == 200:
                data = res.get('data')
                if isinstance(data, dict) and 'cached' in data:
                    if data.get('cached'):
                        self._bump_stats(cache_hits=1)
                    else:
                        self._bump_stats(cache_misses=1)
                return res
            if attempt == 1 and self._is_transient_error(res):
                self._bump_stats(tool_retries=1)
                self._append_log(
                    'warn',
                    f'{name} 暂时失败，正在自动重试',
                    tool=name,
                    data={'retried': True},
                )
                time.sleep(0.15)
                continue
            return res
        return {'code': 500, 'message': f'{name} 调用失败', 'data': None}

    def _safe_llm_model(self) -> str:
        if not self.store_dir:
            return ''
        try:
            from . import llm_settings
            cfg = llm_settings.load(self.store_dir)
            return str(cfg.get('model') or '')
        except Exception:  # noqa: BLE001
            return ''

    def _safe_prompt_sha256(self, candidates: list[dict], target_m: float) -> str:
        try:
            from . import llm_settings
            msgs = llm_settings.build_choice_prompt(candidates, target_m)
            raw = json.dumps(msgs, ensure_ascii=False).encode('utf-8')
            return hashlib.sha256(raw).hexdigest()
        except Exception:  # noqa: BLE001
            return ''

    def status(self) -> dict:
        with self._lock:
            return {
                'session_id': self._session_id,
                'state': self._state,
                'goal': dict(self._goal),
                'log': [dict(item) for item in self._log],
                'candidates': [dict(c) for c in self._candidates],
                'selected': self._selected,
                'preview': dict(self._preview) if isinstance(self._preview, dict) else self._preview,
                'audit': dict(self._audit) if isinstance(self._audit, dict) else self._audit,
                'llm': dict(self._llm),
                'stats': dict(self._stats),
                'error': self._error,
                'fallback': self._fallback,
                'updated_at_ms': self._updated_at_ms,
            }

    def start(
        self,
        text: str = '',
        km: float | None = None,
        points: list[dict] | None = None,
        fence: list[list[tuple]] | None = None,
        run_area_id=None,
    ) -> dict:
        with self._lock:
            if self._cancel_event is not None:
                self._cancel_event.set()
            self._generation += 1
            gen = self._generation
            cancel_event = threading.Event()
            self._cancel_event = cancel_event

            self._session_id = 'ag_' + uuid.uuid4().hex[:12]
            self._state = 'collect'
            self._text = str(text or '')
            self._raw_km = km
            self._points = _normalise_points(points)
            self._fence = fence
            self._run_area_id = run_area_id
            self._force_learn = ('重新学习' in self._text or '再学一次' in self._text
                                 or '先学习' in self._text)
            self._prefer_index = None

            self._goal = {
                'km': None,
                'points': len(self._points),
                'run_area_id': run_area_id,
            }
            self._log = []
            self._candidates = []
            self._selected = ''
            self._waypoints = []
            self._closed = True
            self._preview = None
            self._audit = None
            self._audit_ok = False
            self._preview_args = None
            self._llm = {'used': False, 'model': '', 'error': ''}
            self._stats = _empty_stats()
            self._error = ''
            self._fallback = ''
            self._updated_at_ms = _now_ms()

            self._append_log(
                'info',
                '开始规划跑步路线',
                tool='collect',
                data={'points': len(self._points)},
            )
            snap = self.status()

            t = threading.Thread(
                target=self._run_pipeline,
                args=(gen, cancel_event),
                daemon=True,
            )
            self._thread = t
            t.start()
            return snap

    def _run_pipeline(
        self,
        gen: int | None = None,
        cancel_event: threading.Event | None = None,
        *,
        skip_learn: bool = False,
        force_learn_override: bool | None = None,
    ) -> None:
        with self._lock:
            if gen is None:
                gen = self._generation
            if cancel_event is None:
                cancel_event = self._cancel_event
            if self._is_stale(gen, cancel_event):
                return
            raw_km = self._raw_km
            text = self._text
            points = list(self._points)
            fence = self._fence
            run_area_id = self._run_area_id
            force_learn = (
                self._force_learn
                if force_learn_override is None
                else bool(force_learn_override)
            )
            prefer_index = self._prefer_index
            self._prefer_index = None

        # 1. collect: parse km
        if raw_km is not None:
            try:
                km_val = float(raw_km)
            except (TypeError, ValueError):
                self._set_error(gen, cancel_event, '目标距离无效，请输入数字', tool='collect')
                return
            if not math.isfinite(km_val) or km_val < MIN_KM or km_val > MAX_KM:
                self._set_error(
                    gen,
                    cancel_event,
                    f'目标距离需在 {MIN_KM} 到 {MAX_KM} 公里之间',
                    tool='collect',
                )
                return
            km_val = round(km_val, 2)
        else:
            parsed = parse_distance_km(text)
            km_val = parsed if parsed is not None else 2.5

        with self._lock:
            if self._is_stale(gen, cancel_event):
                return
            self._error = ''
            self._fallback = ''
            self._goal['km'] = km_val
            self._raw_km = km_val
            self._updated_at_ms = _now_ms()
            self._append_log(
                'info',
                f'已收集目标：{km_val:g} 公里，打卡点 {len(points)} 个',
                tool='collect',
                data={'km': km_val, 'points': len(points)},
            )
            self._write_trace('collect', 'collect', True, {'km': km_val, 'points': len(points)})

        if self._is_stale(gen, cancel_event):
            return

        # 2. learn
        if not skip_learn:
            with self._lock:
                if self._is_stale(gen, cancel_event):
                    return
                self._state = 'learn'
                self._updated_at_ms = _now_ms()

            status_env = self._call_tool('route_memory_status')
            if self._is_stale(gen, cancel_event):
                return
            if status_env.get('code') != 200:
                self._set_error(
                    gen,
                    cancel_event,
                    status_env.get('message') or '读取路线记忆状态失败',
                    tool='route_memory_status',
                )
                return

            status_data = status_env.get('data')
            if (not isinstance(status_data, dict)
                    or not isinstance(status_data.get('summary'), dict)):
                self._set_error(
                    gen,
                    cancel_event,
                    '路线状态返回格式异常，请稍后重试',
                    tool='route_memory_status',
                )
                return
            summary = status_data['summary']
            tpl_count = _as_int(
                summary.get('templates')
                if summary.get('templates') is not None
                else len(status_data.get('templates') or [])
            )

            with self._lock:
                if self._is_stale(gen, cancel_event):
                    return
                self._append_log(
                    'info',
                    f'已读取学习路线状态：{tpl_count} 条模板',
                    tool='route_memory_status',
                    data={'templates': tpl_count, 'status': summary.get('status', '')},
                )
                self._write_trace(
                    'learn',
                    'route_memory_status',
                    True,
                    {'templates': tpl_count, 'status': summary.get('status', '')},
                )

            if self._is_stale(gen, cancel_event):
                return

            if force_learn or tpl_count == 0:
                with self._lock:
                    if self._is_stale(gen, cancel_event):
                        return
                    self._append_log(
                        'info',
                        '正在读取历史跑步记录并学习路线...',
                        tool='route_memory_learn',
                        data={'force': force_learn},
                    )
                learn_env = self._call_tool('route_memory_learn', limit=50, force=force_learn)
                if self._is_stale(gen, cancel_event):
                    return
                if learn_env.get('code') != 200:
                    self._set_error(
                        gen,
                        cancel_event,
                        learn_env.get('message') or '学习历史路线失败',
                        tool='route_memory_learn',
                    )
                    return
                learn_data = learn_env.get('data')
                if (not isinstance(learn_data, dict)
                        or not isinstance(learn_data.get('summary'), dict)):
                    self._set_error(
                        gen,
                        cancel_event,
                        '路线学习结果格式异常，请稍后重试',
                        tool='route_memory_learn',
                    )
                    return
                summary = learn_data['summary']
                tpl_count = _as_int(
                    summary.get('templates')
                    if summary.get('templates') is not None
                    else len(learn_data.get('templates') or [])
                )
                with self._lock:
                    if self._is_stale(gen, cancel_event):
                        return
                    self._append_log(
                        'info',
                        f'路线学习完成：可用 {summary.get("usable", 0)} 条，模板 {tpl_count} 条',
                        tool='route_memory_learn',
                        data={
                            'templates': tpl_count,
                            'usable': summary.get('usable', 0),
                            'seen': summary.get('seen', 0),
                            'status': summary.get('status', ''),
                            'cached': bool(learn_data.get('cached', False)),
                        },
                    )
                    self._write_trace(
                        'learn',
                        'route_memory_learn',
                        True,
                        {
                            'templates': tpl_count,
                            'usable': summary.get('usable', 0),
                            'seen': summary.get('seen', 0),
                            'status': summary.get('status', ''),
                            'force': force_learn,
                        },
                    )

            rm_status = str(summary.get('status') or ('ok' if tpl_count > 0 else 'empty'))
            if tpl_count == 0 or rm_status == 'empty':
                self._set_error(
                    gen,
                    cancel_event,
                    '还没有学到可用路线（记录太少或无有效轨迹），先用自动或手绘路线',
                    tool='route_memory_learn',
                    fallback='auto',
                    detail={'templates': tpl_count, 'status': rm_status},
                )
                return
            if rm_status == 'weak':
                self._set_error(
                    gen,
                    cancel_event,
                    '历史跑步记录还不够稳定，先用自动或手绘路线',
                    tool='route_memory_learn',
                    fallback='auto',
                    detail={'templates': tpl_count, 'usable': summary.get('usable', 0), 'status': 'weak'},
                )
                return

        if self._is_stale(gen, cancel_event):
            return

        # 3. suggest
        with self._lock:
            if self._is_stale(gen, cancel_event):
                return
            self._state = 'suggest'
            self._updated_at_ms = _now_ms()

        suggest_env = self._call_tool(
            'route_memory_suggest',
            km=km_val,
            points=points,
            fence=fence,
            run_area_id=run_area_id,
        )
        if self._is_stale(gen, cancel_event):
            return
        if suggest_env.get('code') != 200:
            self._set_error(
                gen,
                cancel_event,
                suggest_env.get('message') or '推荐路线失败',
                tool='route_memory_suggest',
            )
            return

        suggest_data = suggest_env.get('data')
        if not isinstance(suggest_data, dict):
            self._set_error(
                gen,
                cancel_event,
                '推荐结果格式异常，请稍后重试',
                tool='route_memory_suggest',
            )
            return
        raw_candidates = suggest_data.get('candidates')
        if raw_candidates is None:
            raw_candidates = []
        if not isinstance(raw_candidates, list):
            self._set_error(
                gen,
                cancel_event,
                '推荐结果缺少候选列表，请稍后重试',
                tool='route_memory_suggest',
            )
            return
        candidates = [c for c in raw_candidates if isinstance(c, dict)]
        if len(candidates) != len(raw_candidates):
            self._append_log(
                'warn',
                '推荐结果里有无效候选，已忽略',
                tool='route_memory_suggest',
                data={'candidates': len(candidates), 'count': len(raw_candidates)},
            )
        s_status = str(suggest_data.get('status') or 'ok')

        if s_status == 'weak' or not candidates:
            err_msg = (
                suggest_data.get('message')
                or '没有匹配当前围栏与点位的学习路线，先用自动或手绘路线'
            )
            self._set_error(
                gen,
                cancel_event,
                err_msg,
                tool='route_memory_suggest',
                fallback='auto',
                detail={'candidates': len(candidates), 'status': s_status},
            )
            return

        with self._lock:
            if self._is_stale(gen, cancel_event):
                return
            self._candidates = candidates
            self._append_log(
                'info',
                f'已推荐 {len(candidates)} 条候选路线',
                tool='route_memory_suggest',
                data={'candidates': len(candidates), 'target_m': round(km_val * 1000.0)},
            )
            self._write_trace(
                'suggest',
                'route_memory_suggest',
                True,
                {'candidates': len(candidates), 'target_m': round(km_val * 1000.0)},
            )

        if self._is_stale(gen, cancel_event):
            return

        # 4. pick (optional llm_choose, fallback to deterministic top 1)
        valid_ids = [
            str(c.get('template_id'))
            for c in candidates
            if isinstance(c, dict) and c.get('template_id')
        ]
        if not valid_ids:
            self._set_error(
                gen,
                cancel_event,
                '候选路线缺少模板 ID',
                tool='route_memory_suggest',
                fallback='auto',
            )
            return

        selected_id = valid_ids[0]
        llm_info: dict[str, Any] = {'used': False, 'model': '', 'error': ''}
        target_m = round(km_val * 1000.0, 1)

        if prefer_index is not None:
            if 1 <= prefer_index <= len(valid_ids):
                selected_id = valid_ids[prefer_index - 1]
                with self._lock:
                    if self._is_stale(gen, cancel_event):
                        return
                    self._append_log(
                        'info',
                        f'按你的选择使用第 {prefer_index} 条候选路线',
                        tool='message',
                        data={'index': prefer_index, 'template_id': selected_id},
                    )
            else:
                with self._lock:
                    if self._is_stale(gen, cancel_event):
                        return
                    self._append_log(
                        'warn',
                        f'新候选只有 {len(valid_ids)} 条，第 {prefer_index} 条无效，已按评分排序',
                        tool='message',
                        data={'index': prefer_index, 'candidates': len(valid_ids)},
                    )

        pinned = prefer_index is not None and 1 <= prefer_index <= len(valid_ids)
        llm_fn = self._tools.get('llm_choose')

        # An explicit "用第 N 条" is a user decision; do not let the model
        # override it.  The LLM is only a tie-breaker for the normal flow.
        if pinned:
            llm_fn = None

        if callable(llm_fn):
            prompt_sha = ''
            try:
                picked = llm_fn(candidates, target_m)
            except Exception as exc:  # noqa: BLE001
                err_txt = _sanitize_text(str(exc), owner=self.owner) or 'LLM 调用失败'
                llm_info = {'used': False, 'model': '', 'error': err_txt}
                self._bump_stats(llm_calls=1, llm_cache_misses=1)
                with self._lock:
                    if self._is_stale(gen, cancel_event):
                        return
                    self._append_log(
                        'warn',
                        '模型选路失败，已回退到本地推荐第 1 条',
                        tool='llm_choose',
                        data={'used': False, 'error': err_txt},
                    )
                    self._write_trace(
                        'suggest',
                        'llm_choose',
                        False,
                        {'used': False, 'selected': selected_id, 'error': err_txt},
                    )
            else:
                if self._is_stale(gen, cancel_event):
                    return
                if isinstance(picked, dict):
                    cand_id = str(picked.get('template_id') or '').strip()
                    model = str(picked.get('model') or '')
                    err_txt = _sanitize_text(picked.get('error') or '', owner=self.owner)
                    prompt_sha = str(picked.get('prompt_sha256') or '')
                    cached = bool(picked.get('cached'))
                else:
                    cand_id = str(picked or '').strip()
                    model = ''
                    err_txt = ''
                    cached = False

                if cached:
                    self._bump_stats(llm_cache_hits=1)
                else:
                    self._bump_stats(llm_calls=1, llm_cache_misses=1)

                if cand_id and cand_id in valid_ids:
                    # The local score already encodes coverage, distance fit,
                    # freshness and detour.  If the model wanders far below the
                    # top candidate, treat it as a bad tool result rather than
                    # silently loading a much worse route.
                    chosen_score = next(
                        (_as_float(c.get('score')) for c in candidates
                         if str(c.get('template_id') or '') == cand_id),
                        0.0,
                    )
                    top_score = _as_float(candidates[0].get('score'))
                    gap = top_score - chosen_score
                    if gap > 0.15:
                        selected_id = valid_ids[0]
                        llm_info = {
                            'used': False,
                            'model': model,
                            'error': f'模型选择与本地评分差距过大（{gap:.2f}），已回退',
                        }
                        with self._lock:
                            if self._is_stale(gen, cancel_event):
                                return
                            self._append_log(
                                'warn',
                                '模型选择与本地评分差距过大，已回退到本地推荐第 1 条',
                                tool='llm_choose',
                                data={'used': False, 'selected': selected_id,
                                      'cached': cached, 'error': llm_info['error']},
                            )
                            self._write_trace(
                                'suggest',
                                'llm_choose',
                                False,
                                {'used': False, 'selected': selected_id,
                                 'cached': cached, 'error': llm_info['error']},
                            )
                    else:
                        selected_id = cand_id
                        if not model:
                            model = self._safe_llm_model()
                        if not prompt_sha:
                            prompt_sha = self._safe_prompt_sha256(candidates, target_m)
                        llm_info = {'used': True, 'model': model, 'error': ''}
                        with self._lock:
                            if self._is_stale(gen, cancel_event):
                                return
                            self._append_log(
                                'info',
                                f'已选定候选路线 {selected_id}',
                                tool='llm_choose',
                                data={'used': True, 'template_id': selected_id,
                                      'model': model, 'cached': cached},
                            )
                            self._write_trace(
                                'suggest',
                                'llm_choose',
                                True,
                                {'used': True, 'template_id': selected_id,
                                 'model': model, 'cached': cached},
                                prompt_sha256=prompt_sha or None,
                            )
                elif cand_id:
                    llm_info = {
                        'used': False,
                        'model': model,
                        'error': err_txt or '模型返回了无效的候选 ID',
                    }
                    with self._lock:
                        if self._is_stale(gen, cancel_event):
                            return
                        self._append_log(
                            'warn',
                            '模型返回候选不在列表中，已回退到本地推荐第 1 条',
                            tool='llm_choose',
                            data={'used': False, 'selected': selected_id},
                        )
                        self._write_trace(
                            'suggest',
                            'llm_choose',
                            False,
                            {'used': False, 'selected': selected_id, 'error': llm_info['error']},
                        )
                else:
                    llm_info = {'used': False, 'model': model, 'error': err_txt}
                    if err_txt:
                        with self._lock:
                            if self._is_stale(gen, cancel_event):
                                return
                            self._append_log(
                                'warn',
                                '模型未给出有效候选，已回退到本地推荐第 1 条',
                                tool='llm_choose',
                                data={'used': False, 'selected': selected_id,
                                      'error': err_txt},
                            )
                            self._write_trace(
                                'suggest',
                                'llm_choose',
                                False,
                                {'used': False, 'selected': selected_id,
                                 'error': err_txt},
                            )

        with self._lock:
            if self._is_stale(gen, cancel_event):
                return
            self._selected = selected_id
            self._llm = llm_info

        # 5-8. adapt -> preview -> audit -> wait_confirm
        self._adapt_preview_audit(
            gen, cancel_event, selected_id, km_val, points,
            allow_rotation=not pinned,
        )

    def _adapt_preview_audit(
        self,
        gen: int,
        cancel_event: threading.Event | None,
        template_id: str,
        km_val: float,
        points: list[dict],
        reversed_override: tuple[list[list[float]], bool] | None = None,
        allow_rotation: bool = True,
    ) -> None:
        """Try the chosen candidate, then the other suggestions, until audit passes.

        A route can fail the local audit for reasons the scorer cannot see
        (e.g. one segment crosses a gate).  Silently stopping on the first
        failure wastes a good second candidate, so the Agent rotates through
        at most three candidates and keeps the first audited result.  An
        explicit "用第 N 条" is a user decision, so it opts out of rotation.
        """
        if self._is_stale(gen, cancel_event):
            return

        if reversed_override is not None or not allow_rotation:
            plan = [template_id]
        else:
            plan = [template_id]
            for cand in self._candidates:
                tid = str(cand.get('template_id') or '')
                if tid and tid not in plan:
                    plan.append(tid)
            plan = plan[:3]

        last_error = ''
        last_audited_id = ''
        attempts = 0

        for idx, tid in enumerate(plan):
            if idx > 0:
                failed_id = plan[idx - 1]
                failed_ord = next(
                    (i + 1 for i, c in enumerate(self._candidates)
                     if str(c.get('template_id') or '') == failed_id),
                    0,
                )
                label = f'第 {failed_ord} 条候选' if failed_ord else '当前候选'
                with self._lock:
                    if self._is_stale(gen, cancel_event):
                        return
                    self._append_log(
                        'warn',
                        f'{label}不可用，正在自动尝试下一条',
                        tool='audit_run',
                        data={'attempts': idx, 'template_id': tid},
                    )
                    # The model's original pick did not survive the local
                    # checks.  Do not keep claiming the model chose the final
                    # route; the transcript already explains the rotation.
                    if self._llm.get('used'):
                        self._llm = {
                            'used': False,
                            'model': self._llm.get('model', ''),
                            'error': '模型选的候选未通过本地检查，已自动换一条',
                        }
            status, message = self._attempt_candidate(
                gen,
                cancel_event,
                tid,
                km_val,
                points,
                reversed_override=reversed_override if idx == 0 else None,
            )
            if status in ('stale', 'error'):
                return
            attempts += 1
            if status == 'candidate_failed':
                last_error = message
                continue
            last_audited_id = tid
            if status == 'ok':
                break

        self._bump_stats(candidates_tried=attempts)

        if not last_audited_id:
            self._set_error(
                gen,
                cancel_event,
                last_error or '所有候选路线都适配失败，请换一个目标距离',
                tool='preview_route',
                fallback='auto',
            )
            return

        self._finalize_wait_confirm(
            gen, cancel_event, last_audited_id, km_val, points, attempts,
            pinned=not allow_rotation,
        )

    def _finalize_wait_confirm(
        self,
        gen: int,
        cancel_event: threading.Event | None,
        template_id: str,
        km_val: float,
        points: list[dict],
        attempts: int,
        pinned: bool = False,
    ) -> None:
        with self._lock:
            if self._is_stale(gen, cancel_event):
                return
            waypoints = [list(p) for p in self._waypoints]
            closed = bool(self._closed)
            self._selected = template_id
            self._preview_args = {
                'km': km_val,
                'pace': 6.0,
                'cadence': 150,
                'waypoints': waypoints,
                'closed': closed,
                'checkpoints': list(points) if points else [],
            }
            self._state = 'wait_confirm'
            self._error = ''
            self._fallback = ''
            self._updated_at_ms = _now_ms()
            if self._audit_ok:
                text = '已准备好路线，请确认后前往跑步页预览'
                level = 'info'
            elif pinned:
                text = '你选择的候选未通过本地检查；未自动替换，请换一条或调整目标距离'
                level = 'warn'
            else:
                text = f'已尝试 {attempts} 条候选，均未通过本地检查；请调整目标距离或点位'
                level = 'warn'
            self._append_log(
                level,
                text,
                tool='wait_confirm',
                data={
                    'km': km_val,
                    'closed': closed,
                    'waypoints': len(waypoints),
                    'attempts': attempts,
                    'template_id': template_id,
                },
            )
            self._write_trace(
                'wait_confirm',
                'wait_confirm',
                True,
                {
                    'km': km_val,
                    'closed': closed,
                    'waypoints': len(waypoints),
                    'attempts': attempts,
                    'audit_ok': self._audit_ok,
                },
            )

    def _attempt_candidate(
        self,
        gen: int,
        cancel_event: threading.Event | None,
        template_id: str,
        km_val: float,
        points: list[dict],
        reversed_override: tuple[list[list[float]], bool] | None = None,
    ) -> tuple[str, str]:
        if self._is_stale(gen, cancel_event):
            return 'stale', ''

        # 5. adapt
        with self._lock:
            if self._is_stale(gen, cancel_event):
                return 'stale', ''
            self._state = 'adapt'
            self._selected = template_id
            self._updated_at_ms = _now_ms()

        if reversed_override is None:
            apply_env = self._call_tool(
                'route_memory_apply',
                template_id,
                km=km_val,
                points=points,
            )
            if self._is_stale(gen, cancel_event):
                return 'stale', ''
            if apply_env.get('code') != 200:
                return 'candidate_failed', apply_env.get('message') or '适配路线失败'
            applied = apply_env.get('data')
            if not isinstance(applied, dict):
                return 'candidate_failed', '适配结果格式异常'
            if applied.get('ok') is False:
                return 'candidate_failed', applied.get('error') or '适配路线失败'
            raw_waypoints = applied.get('waypoints')
            if not isinstance(raw_waypoints, list):
                return 'candidate_failed', '适配结果缺少控制点'
            waypoints = [list(p) for p in raw_waypoints]
            if len(waypoints) < 2:
                return 'candidate_failed', '适配结果控制点不足'
            closed = bool(applied.get('closed', True))
            with self._lock:
                if self._is_stale(gen, cancel_event):
                    return 'stale', ''
                self._append_log(
                    'info',
                    f'已适配路线（{len(waypoints)} 个控制点）',
                    tool='route_memory_apply',
                    data={
                        'template_id': template_id,
                        'waypoints': len(waypoints),
                        'closed': closed,
                        'lap_m': applied.get('lap_m', 0),
                        'laps': applied.get('laps', 1),
                        'cached': applied.get('cached', False),
                    },
                )
                self._write_trace(
                    'adapt',
                    'route_memory_apply',
                    True,
                    {
                        'template_id': template_id,
                        'waypoints': len(waypoints),
                        'closed': closed,
                        'lap_m': applied.get('lap_m', 0),
                        'laps': applied.get('laps', 1),
                    },
                )
        else:
            waypoints, closed = reversed_override
            if len(waypoints) < 2:
                return 'candidate_failed', '反向路线控制点不足'
            with self._lock:
                if self._is_stale(gen, cancel_event):
                    return 'stale', ''
                self._append_log(
                    'info',
                    '已将闭环路线反向，正在重新生成预览',
                    tool='reverse',
                    data={
                        'template_id': template_id,
                        'waypoints': len(waypoints),
                        'closed': bool(closed),
                        'reversed': True,
                    },
                )
                self._write_trace(
                    'adapt',
                    'reverse',
                    True,
                    {
                        'template_id': template_id,
                        'waypoints': len(waypoints),
                        'closed': bool(closed),
                        'reversed': True,
                    },
                )

        if self._is_stale(gen, cancel_event):
            return 'stale', ''

        # 6. preview
        with self._lock:
            if self._is_stale(gen, cancel_event):
                return 'stale', ''
            self._state = 'preview'
            self._updated_at_ms = _now_ms()

        preview_env = self._call_tool(
            'preview_route',
            km=km_val,
            pace=6.0,
            cadence=150,
            waypoints=waypoints,
            closed=closed,
            checkpoints=points,
        )
        if self._is_stale(gen, cancel_event):
            return 'stale', ''
        if preview_env.get('code') != 200:
            return 'candidate_failed', preview_env.get('message') or '生成预览失败'

        preview_data = preview_env.get('data')
        if not isinstance(preview_data, dict):
            return 'candidate_failed', '预览结果格式异常'
        model_info = preview_data.get('model')
        verdict = model_info.get('verdict', '') if isinstance(model_info, dict) else ''
        with self._lock:
            if self._is_stale(gen, cancel_event):
                return 'stale', ''
            # Only publish the candidate after the preview succeeded, so a
            # failed retry cannot leave the UI showing a half-applied route.
            self._waypoints = [list(p) for p in waypoints]
            self._closed = bool(closed)
            self._preview = preview_data
            self._append_log(
                'info',
                '已生成跑步预览',
                tool='preview_route',
                data={'km': km_val, 'closed': closed, 'verdict': verdict},
            )
            self._write_trace(
                'preview',
                'preview_route',
                True,
                {'km': km_val, 'closed': closed, 'verdict': verdict},
            )

        if self._is_stale(gen, cancel_event):
            return 'stale', ''

        # 7. audit (must be called with NO arguments so it audits _last_run)
        with self._lock:
            if self._is_stale(gen, cancel_event):
                return 'stale', ''
            self._state = 'audit'
            self._updated_at_ms = _now_ms()

        audit_env = self._call_tool('audit_run')
        if self._is_stale(gen, cancel_event):
            return 'stale', ''
        if audit_env.get('code') != 200:
            self._set_error(
                gen,
                cancel_event,
                audit_env.get('message') or '路线审计失败',
                tool='audit_run',
            )
            return 'error', ''

        audit_data = audit_env.get('data')
        audit_summary = audit_data.get('summary') if isinstance(audit_data, dict) else None
        # A missing summary used to be treated as "passed"; that is unsafe.
        # Refuse to hand an unaudited route to the confirmation gate.
        if (not isinstance(audit_summary, dict)
                or not isinstance(audit_summary.get('ok'), bool)):
            self._set_error(
                gen,
                cancel_event,
                '审计结果格式异常，不能确认路线',
                tool='audit_run',
            )
            return 'error', ''
        audit_ok = bool(audit_summary.get('ok'))
        passed = _as_int(audit_summary.get('passed'))
        failed = _as_int(audit_summary.get('failed'))

        with self._lock:
            if self._is_stale(gen, cancel_event):
                return 'stale', ''
            self._audit = audit_data
            self._audit_ok = audit_ok
            self._append_log(
                'info' if audit_ok else 'warn',
                f'本地审计完成（通过 {passed} 项，失败 {failed} 项）',
                tool='audit_run',
                data={'audit_ok': audit_ok, 'passed': passed, 'failed': failed},
            )
            self._write_trace(
                'audit',
                'audit_run',
                True,
                {'audit_ok': audit_ok, 'passed': passed, 'failed': failed},
            )

        return ('ok' if audit_ok else 'audit_failed'), ''

    def message(self, text: str) -> dict:
        msg = str(text or '').strip()
        new_km = parse_distance_km(msg)
        m_sel = _SELECT_N_RE.search(msg)
        sel_idx = _cn_to_int(m_sel.group(1)) if m_sel else None

        # Parse every intent before acting.  Substring checks on their own made
        # "不要取消" cancel and "换一条3公里" ignore the new distance; explicit
        # flags keep the tool call aligned with what the user actually asked.
        wants_cancel = _wants_cancel(msg, new_km)
        wants_confirm = _intent_present(
            msg, '确认', '就这条', '就用这条', '可以了', '就这样')
        wants_rotate = _intent_present(msg, '换一条', '下一条', '换一个')
        wants_reverse = _intent_present(msg, '反向', '反着跑')
        wants_relearn = _intent_present(msg, '重新学习', '再学一次')

        m_mode = ''
        tpl_id = ''
        km_val = 0.0
        points: list[dict] = []
        gen = 0
        cancel_event = None
        rev_pair = None
        skip_learn_flag = False

        with self._lock:
            state = self._state
            # Cancel wins unless it was negated ("不要取消") or "算了" is only
            # filler before a new target ("算了，跑3公里").
            if wants_cancel:
                return self.cancel()

            if state in ('idle', 'cancelled', 'confirmed'):
                self._append_log(
                    'warn',
                    '当前会话已结束，请重新发送目标开始新的规划',
                    tool='message',
                )
                self._updated_at_ms = _now_ms()
                return self.status()

            if state in TRANSIENT_STATES:
                self._append_log('info', '还在处理上一条请求，请稍候', tool='message')
                self._updated_at_ms = _now_ms()
                return self.status()

            # A distance change and a confirmation cannot both be the primary
            # intent; the distance wins so "确认3公里" does not confirm a stale
            # 2.5 km plan.
            if wants_confirm and new_km is None and not m_sel:
                res = self.confirm()
                if res.get('ok'):
                    snap = self.status()
                    snap.update(res)
                    return snap
                return res

            if new_km is not None:
                gen = self._generation
                cancel_event = self._cancel_event
                self._raw_km = new_km
                self._goal['km'] = new_km
                self._state = 'suggest'
                # "跑3公里，用第2条": remember the ordinal and apply it after
                # the fresh suggestion list is available.  Out-of-range values
                # fall back to the normal ranking with a warning.
                self._prefer_index = (
                    sel_idx if sel_idx is not None and 1 <= sel_idx <= 3 else None
                )
                self._candidates = []
                self._selected = ''
                self._waypoints = []
                self._preview = None
                self._audit = None
                self._audit_ok = False
                self._preview_args = None
                if wants_relearn:
                    m_mode = 'relearn'
                    self._force_learn = True
                else:
                    m_mode = 'distance'
                    skip_learn_flag = state == 'wait_confirm'
                self._append_log(
                    'info',
                    f'收到新目标：{new_km:g} 公里',
                    tool='collect',
                    data={'km': new_km, 'preferred': self._prefer_index or 0},
                )
                if wants_rotate or wants_reverse:
                    self._append_log(
                        'info',
                        '已按新目标重新推荐；换一条/反向会在新候选上重新计算',
                        tool='message',
                    )
            elif m_sel:
                if state != 'wait_confirm':
                    self._append_log('warn', '当前不在等待确认阶段，无法切换候选路线', tool='message')
                    self._updated_at_ms = _now_ms()
                    return self.status()
                if sel_idx is None or sel_idx < 1 or sel_idx > len(self._candidates):
                    self._append_log(
                        'warn',
                        f'候选序号无效，目前有 {len(self._candidates)} 条候选',
                        tool='message',
                    )
                    self._updated_at_ms = _now_ms()
                    return self.status()
                tpl_id = str(self._candidates[sel_idx - 1].get('template_id') or '')
                km_val = float(self._goal.get('km') or 2.5)
                points = list(self._points)
                gen = self._generation
                cancel_event = self._cancel_event
                m_mode = 'select'
                self._append_log(
                    'info',
                    f'切换到第 {sel_idx} 条候选路线',
                    tool='message',
                    data={'index': sel_idx, 'template_id': tpl_id},
                )
                self._state = 'adapt'
                self._preview = None
                self._audit = None
                self._audit_ok = False
                self._preview_args = None
            elif wants_rotate:
                if state != 'wait_confirm':
                    self._append_log('warn', '当前不在等待确认阶段，不能换一条', tool='message')
                    self._updated_at_ms = _now_ms()
                    return self.status()
                if len(self._candidates) <= 1:
                    self._append_log('info', '当前只有 1 条候选路线，没有更多可换', tool='message')
                    self._updated_at_ms = _now_ms()
                    return self.status()
                cur_idx = next(
                    (i for i, c in enumerate(self._candidates)
                     if c.get('template_id') == self._selected),
                    0,
                )
                nxt_idx = (cur_idx + 1) % len(self._candidates)
                tpl_id = str(self._candidates[nxt_idx].get('template_id') or '')
                km_val = float(self._goal.get('km') or 2.5)
                points = list(self._points)
                gen = self._generation
                cancel_event = self._cancel_event
                m_mode = 'rotate'
                self._append_log(
                    'info',
                    f'换用第 {nxt_idx + 1} 条候选路线',
                    tool='message',
                    data={'index': nxt_idx + 1, 'template_id': tpl_id},
                )
                self._state = 'adapt'
                self._preview = None
                self._audit = None
                self._audit_ok = False
                self._preview_args = None
            elif wants_reverse:
                if state != 'wait_confirm':
                    self._append_log('warn', '当前不在等待确认阶段，不能反向', tool='message')
                    self._updated_at_ms = _now_ms()
                    return self.status()
                if not self._closed:
                    self._append_log('warn', '当前路线是开放路线，不支持反向', tool='reverse')
                    self._updated_at_ms = _now_ms()
                    return self.status()
                ring = [list(p) for p in self._waypoints]
                if len(ring) < 2:
                    self._append_log('warn', '路线点数不足，无法反向', tool='reverse')
                    self._updated_at_ms = _now_ms()
                    return self.status()
                if _haversine_m(ring[0], ring[-1]) <= 1e-3:
                    core = ring[:-1]
                else:
                    core = ring
                if len(core) < 2:
                    self._append_log('warn', '路线点数不足，无法反向', tool='reverse')
                    self._updated_at_ms = _now_ms()
                    return self.status()
                rev = list(reversed(core))
                rev.append(list(rev[0]))
                tpl_id = self._selected
                km_val = float(self._goal.get('km') or 2.5)
                points = list(self._points)
                gen = self._generation
                cancel_event = self._cancel_event
                m_mode = 'reverse'
                rev_pair = (rev, True)
                self._state = 'adapt'
                self._preview = None
                self._audit = None
                self._audit_ok = False
                self._preview_args = None
            elif wants_relearn:
                gen = self._generation
                cancel_event = self._cancel_event
                self._force_learn = True
                if self._goal.get('km') is None:
                    self._raw_km = 2.5
                m_mode = 'relearn'
                self._state = 'learn'
                self._candidates = []
                self._selected = ''
                self._waypoints = []
                self._preview = None
                self._audit = None
                self._audit_ok = False
                self._preview_args = None
            else:
                self._append_log(
                    'info',
                    '没听懂你的意思，想跑多少公里？（支持如“2.5公里”“换一条”“反向”“重新学习”“确认”“取消”）',
                    tool='message',
                )
                self._updated_at_ms = _now_ms()
                return self.status()

        # Execute selected synchronous step outside lock
        if m_mode in ('select', 'rotate'):
            self._adapt_preview_audit(
                gen, cancel_event, tpl_id, km_val, points,
                allow_rotation=(m_mode != 'select'),
            )
            return self.status()

        if m_mode == 'reverse':
            self._adapt_preview_audit(
                gen,
                cancel_event,
                tpl_id,
                km_val,
                points,
                reversed_override=rev_pair,
                allow_rotation=False,
            )
            return self.status()

        if m_mode == 'relearn':
            self._run_pipeline(gen, cancel_event, skip_learn=False, force_learn_override=True)
            return self.status()

        if m_mode == 'distance':
            self._run_pipeline(
                gen,
                cancel_event,
                skip_learn=skip_learn_flag,
                force_learn_override=False,
            )
            return self.status()

        return self.status()

    def confirm(self) -> dict:
        with self._lock:
            if self._state != 'wait_confirm' or not isinstance(self._preview_args, dict):
                err = f'当前状态（{self._state}）不可确认，请等待路线预览完成'
                self._append_log('error', err, tool='confirm')
                self._write_trace(self._state, 'confirm', False, {'error': err})
                self._updated_at_ms = _now_ms()
                snap = self.status()
                snap['ok'] = False
                snap['error'] = err
                return snap

            if not self._audit_ok:
                err = '本地审计未通过，不能确认；请换一条或重新规划'
                self._append_log('error', err, tool='confirm')
                self._write_trace(self._state, 'confirm', False, {'error': err})
                self._updated_at_ms = _now_ms()
                snap = self.status()
                snap['ok'] = False
                snap['error'] = err
                return snap

            preview_args = {
                'km': self._preview_args['km'],
                'pace': self._preview_args['pace'],
                'cadence': self._preview_args['cadence'],
                'waypoints': [list(p) for p in self._preview_args['waypoints']],
                'closed': bool(self._preview_args['closed']),
                'checkpoints': list(self._preview_args.get('checkpoints') or []),
            }
            route = [list(p) for p in preview_args['waypoints']]
            km_val = preview_args['km']

            self._state = 'confirmed'
            self._updated_at_ms = _now_ms()
            self._append_log(
                'info',
                '已确认路线，请前往跑步页预览并由你决定是否提交',
                tool='confirm',
                data={'km': km_val, 'waypoints': len(route), 'closed': preview_args['closed']},
            )
            self._write_trace(
                'confirmed',
                'confirm',
                True,
                {'km': km_val, 'waypoints': len(route), 'closed': preview_args['closed']},
            )
            return {
                'ok': True,
                'state': 'confirmed',
                'preview_args': preview_args,
                'route': route,
                'km': km_val,
            }

    def cancel(self) -> dict:
        with self._lock:
            if self._cancel_event is not None:
                self._cancel_event.set()
            self._generation += 1
            self._state = 'cancelled'
            self._candidates = []
            self._selected = ''
            self._waypoints = []
            self._preview = None
            self._audit = None
            self._audit_ok = False
            self._preview_args = None
            self._error = ''
            self._fallback = ''
            self._updated_at_ms = _now_ms()
            self._append_log('info', '已取消本次规划', tool='cancel')
            self._write_trace('cancelled', 'cancel', True, {})
            return self.status()

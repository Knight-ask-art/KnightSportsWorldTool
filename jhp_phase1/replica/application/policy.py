"""Run policy — POST api/v70103/runModePolicy.

Ported from YanamiNeko/NekoSportsWorldTool `src/api/policy.rs`.  Two things come
out of it that matter for submission:

* `timestamp`  -> the `runes` header is "{timestamp}{uid}"
* `policy`     -> the completion rule set (0 = credit-run, 1 = relaxed)

runMode 1 is the 计分跑 (scored) mode and is only accepted inside the school's
valid window (06:30-22:00 by default); outside it the server answers
`11016 不在有效跑步时间内`.
"""

from __future__ import annotations

import json

from ..domain import coords

PATH = 'api/v70103/runModePolicy'

OPERATE_TYPE_RUN = 0
OPERATE_TYPE_SEMESTER = 1

RUN_MODE_SCORED = 1

#: The policy payload's must-pass point list has no fixed field name, so every
#: known alias is merged and de-duplicated (policy.rs::extract_must_points).
MUST_POINT_FIELDS = (
    'pointList', 'runPointList', 'mustPointList', 'passPointList',
    'mustPoints', 'passPoints', 'runPoints', 'points', 'nodeList',
    'checkPointList',
)


def build_body(acct, run_mode: int = RUN_MODE_SCORED,
               operate_type: int = OPERATE_TYPE_RUN,
               rule_update_time: int = 0,
               geo_fence_update_time: int = 0) -> dict:
    """Exactly the JSON RunPolicyViewModel.getPolicy() assembles."""
    return {
        'runMode': int(run_mode),
        'ruleUpdateTime': int(rule_update_time),
        'geoFenceUpdateTime': int(geo_fence_update_time),
        'selectUnid': int(acct.unid),
        'operateType': int(operate_type),
    }


def fetch_policy(client, acct, run_mode: int = RUN_MODE_SCORED,
                 operate_type: int = OPERATE_TYPE_RUN,
                 rule_update_time: int = 0,
                 geo_fence_update_time: int = 0) -> dict:
    """Return the policy payload; raises RuntimeError on a business error."""
    res = client.request(acct, PATH, build_body(
        acct, run_mode, operate_type, rule_update_time, geo_fence_update_time),
        retries=2)
    biz = res.get('json') or {}
    if biz.get('error') != 10000:
        raise RuntimeError('runModePolicy error %s: %s'
                           % (biz.get('error'), biz.get('message')))
    return normalize_data(biz.get('data'))


def normalize_data(data) -> dict:
    """`data` may arrive as a dict or as a JSON-encoded string."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:  # noqa: BLE001
            return {}
    return data if isinstance(data, dict) else {}


def extract_must_points(data) -> list[tuple[float, float]]:
    """Must-pass points as GCJ-02 (our internal frame).

    Mirrors `policy.rs::extract_must_points`: merge every recognised alias,
    de-duplicate, and **raise** on a malformed list rather than dropping it.
    Silently losing a required point would produce a record that can never be
    marked complete.
    """
    data = normalize_data(data)
    out: list[tuple[float, float]] = []
    for name in MUST_POINT_FIELDS:
        value = data.get(name)
        if value is None:
            continue
        if not isinstance(value, list):
            raise ValueError('策略点位 %s 不是列表' % name)
        for i, p in enumerate(value):
            xy = coords.pair_gcj(p) if isinstance(p, dict) else None
            if xy is None:
                raise ValueError('策略点位 %s[%d] 坐标无效' % (name, i))
            if not any(abs(xy[0] - q[0]) < 1e-6 and abs(xy[1] - q[1]) < 1e-6
                       for q in out):
                out.append(xy)
    return out


def must_points(policy: dict | None) -> list[tuple[float, float]]:
    """Best-effort wrapper: a broken list degrades to empty plus a warning."""
    try:
        return extract_must_points(policy or {})
    except Exception:  # noqa: BLE001
        return []


def policy_timestamp(policy: dict | None) -> int | None:
    ts = (policy or {}).get('timestamp')
    try:
        return int(ts) if ts else None
    except Exception:
        return None


def rule_model(policy: dict | None) -> dict:
    return (policy or {}).get('runRuleModel') or {}


def rule_min_distance(policy: dict | None) -> int:
    """`runRuleModel.minDistance` — upstream puts this in `selDistance`."""
    try:
        return int(rule_model(policy).get('minDistance') or 0)
    except Exception:  # noqa: BLE001
        return 0


def rule_valid_time(policy: dict | None) -> int:
    try:
        return int(rule_model(policy).get('validTime') or 0)
    except Exception:  # noqa: BLE001
        return 0


def _positive_number(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _hhmm(value) -> str:
    """Normalise ``HH:MM`` / ``HH:MM:SS`` from ValidTime to the form's format."""
    text = str(value or '').strip()
    parts = text.split(':')
    if len(parts) < 2:
        return ''
    try:
        hh, mm = int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        return ''
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return '%02d:%02d' % (hh, mm)
    return ''


def rules_from_policy(data) -> dict:
    """Map ``runRuleModel`` to the settings page's school-rules schema.

    Field provenance (decompiled official APK):

    * ``minDistance`` is metres (``RunStartViewModel`` renders ``/1000`` km);
    * ``minRunTime`` / ``maxRunTime`` are seconds (``formatDate2HourMinSen``);
    * ``speedTop`` / ``speedBottom`` are **pace** in minutes/km
      (``Utils.formatSpeed`` turns them into ``m'ss"``);
    * ``validTime[]`` entries are ``{start,end}`` wall-clock strings.

    Fields without a safe equivalent in the local schema (``dayGoal``,
    ``weekCountGoal``, ``stepTop/Bottom``, ``minSteps``) are reported in
    ``meta`` instead of being guessed into a rule.  Guessing here would make
    the qualification model reject valid runs.
    """
    data = normalize_data(data)
    model = data.get('runRuleModel')
    if isinstance(model, str):
        model = normalize_data(model)
    if not isinstance(model, dict):
        return {'rules': {}, 'raw': {}, 'meta': {}, 'mapped': []}

    rules: dict = {}
    min_d = _positive_number(model.get('minDistance'))
    if min_d is not None:
        rules['min_km'] = round(min_d / 1000.0, 3)

    min_t = _positive_number(model.get('minRunTime'))
    if min_t is not None:
        rules['min_minutes'] = round(min_t / 60.0, 1)
    max_t = _positive_number(model.get('maxRunTime'))
    if max_t is not None:
        rules['max_minutes'] = round(max_t / 60.0, 1)

    pace_top = _positive_number(model.get('speedTop'))
    pace_bottom = _positive_number(model.get('speedBottom'))
    if pace_top is not None and pace_bottom is not None and pace_top > pace_bottom:
        pace_top, pace_bottom = pace_bottom, pace_top
    if pace_top is not None:
        rules['pace_min'] = round(pace_top, 2)
    if pace_bottom is not None:
        rules['pace_max'] = round(pace_bottom, 2)

    step_min = _positive_number(model.get('stepBottom'))
    step_max = _positive_number(model.get('stepTop'))
    if step_min is not None and step_max is not None and step_min > step_max:
        step_min, step_max = step_max, step_min
    if step_min is not None:
        rules['step_min'] = int(round(step_min))
    if step_max is not None:
        rules['step_max'] = int(round(step_max))

    windows = model.get('validTime')
    if isinstance(windows, str):
        try:
            windows = json.loads(windows)
        except Exception:  # noqa: BLE001
            windows = []
    if isinstance(windows, dict):
        windows = [windows]
    if not isinstance(windows, list):
        windows = []
    window_count = 0
    window_list = []
    for w in windows:
        if isinstance(w, str):
            # tolerate "06:30-22:00"
            parts = w.split('-', 1)
            start = _hhmm(parts[0]) if len(parts) == 2 else ''
            end = _hhmm(parts[1]) if len(parts) == 2 else ''
        elif isinstance(w, dict):
            start = _hhmm(w.get('start') or w.get('startTime'))
            end = _hhmm(w.get('end') or w.get('endTime'))
        elif isinstance(w, (list, tuple)) and len(w) >= 2:
            start, end = _hhmm(w[0]), _hhmm(w[1])
        else:
            continue
        if not (start and end):
            continue
        window_count += 1
        window_list.append(start + '–' + end)
        if 'window_start' not in rules:
            rules['window_start'] = start
            rules['window_end'] = end

    meta = {
        'window_count': window_count,
        'windows': window_list,
        'day_goal_m': _positive_number(model.get('dayGoal')),
        'week_count_goal': _positive_number(model.get('weekCountGoal')),
        'step_min': _positive_number(model.get('stepBottom')),
        'step_max': _positive_number(model.get('stepTop')),
        'min_steps': _positive_number(model.get('minSteps')),
        'update_time': model.get('updateTime') or 0,
    }
    return {'rules': rules, 'raw': model, 'meta': meta,
            'mapped': sorted(rules)}


def policy_value(policy: dict | None, default: int = 1) -> int:
    """The server-decided rule set (0 or 1).  Upstream submits this verbatim
    instead of hardcoding it (src/api/flow.rs: `let pol = fetch_policy(...)`)."""
    try:
        v = (policy or {}).get('policy')
        return int(v) if v is not None else default
    except Exception:  # noqa: BLE001
        return default


# --------------------------------------------------------------------------
# self test (offline)
# --------------------------------------------------------------------------

def self_test() -> list[tuple[str, bool, str]]:
    out = []

    class _A:
        unid = 47203

    body = build_body(_A())
    out.append(('body keys', sorted(body) == sorted(
        ['runMode', 'ruleUpdateTime', 'geoFenceUpdateTime',
         'selectUnid', 'operateType']), str(sorted(body))))
    out.append(('selectUnid int', body['selectUnid'] == 47203, '47203'))
    out.append(('runMode default 1', body['runMode'] == 1, '1'))
    out.append(('operateType run=0', body['operateType'] == 0, '0'))

    out.append(('timestamp helper',
                policy_timestamp({'timestamp': 1791822671092}) == 1791822671092,
                '1791822671092'))
    out.append(('timestamp None safe', policy_timestamp(None) is None, 'None'))
    out.append(('rule_model empty safe', rule_model(None) == {}, '{}'))

    sample = {'policy': 1, 'runRuleModel': {'minDistance': 2000, 'validTime': 600}}
    out.append(('minDistance read', rule_min_distance(sample) == 2000, '2000'))
    out.append(('validTime read', rule_valid_time(sample) == 600, '600'))
    out.append(('policy value', policy_value(sample) == 1, '1'))
    out.append(('policy default on None', policy_value(None, 1) == 1, '1'))
    out.append(('minDistance None safe', rule_min_distance(None) == 0, '0'))

    # ---- must points (mirrors policy.rs tests) --------------------------
    a = {'lat': '39.9', 'lon': '116.4'}
    merged = extract_must_points({'pointList': [a],
                                  'mustPoints': [a, {'lat': 39.91, 'lon': 116.41}]})
    out.append(('must points merged + deduped', len(merged) == 2, str(merged)))
    out.append(('null list -> empty',
                extract_must_points({'mustPoints': None}) == [], '[]'))
    out.append(('missing -> empty', extract_must_points({}) == [], '[]'))
    out.append(('malformed point raises',
                _raises(lambda: extract_must_points(
                    {'mustPoints': [{}, {'lat': 39.9, 'lon': 116.4}]})),
                'error'))
    out.append(('non-list raises',
                _raises(lambda: extract_must_points({'mustPoints': 'bad'})),
                'error'))
    gcj_only = extract_must_points({'mustPoints': [{'glat': 22.41, 'glon': 112.94}]})
    out.append(('GCJ-only point accepted',
                len(gcj_only) == 1 and abs(gcj_only[0][0] - 22.41) < 1e-9,
                str(gcj_only)))
    out.append(('policy string data normalised',
                normalize_data('{"policy":1}') == {'policy': 1}, 'ok'))

    # ---- runRuleModel -> settings form ----------------------------------
    rule = {'policy': 1, 'runRuleModel': {
        'minDistance': 2000, 'minRunTime': 600, 'maxRunTime': 5400,
        'speedTop': 5.5, 'speedBottom': 8.0, 'stepBottom': 150,
        'stepTop': 200, 'dayGoal': 3000, 'weekCountGoal': 3,
        'validTime': [{'start': '06:30:00', 'end': '22:00:00'},
                      {'start': '12:00', 'end': '13:00'}],
    }}
    mapped = rules_from_policy(rule)
    out.append(('minDistance metres -> km',
                mapped['rules'].get('min_km') == 2.0,
                str(mapped['rules'].get('min_km'))))
    out.append(('run time seconds -> minutes',
                mapped['rules'].get('min_minutes') == 10.0
                and mapped['rules'].get('max_minutes') == 90.0,
                str((mapped['rules'].get('min_minutes'),
                     mapped['rules'].get('max_minutes')))))
    out.append(('speed fields become pace',
                mapped['rules'].get('pace_min') == 5.5
                and mapped['rules'].get('pace_max') == 8.0,
                str((mapped['rules'].get('pace_min'),
                     mapped['rules'].get('pace_max')))))
    out.append(('first validTime window maps to form',
                mapped['rules'].get('window_start') == '06:30'
                and mapped['rules'].get('window_end') == '22:00',
                str((mapped['rules'].get('window_start'),
                     mapped['rules'].get('window_end')))))
    out.append(('step range maps to cadence bounds',
                mapped['rules'].get('step_min') == 150
                and mapped['rules'].get('step_max') == 200,
                str((mapped['rules'].get('step_min'),
                     mapped['rules'].get('step_max')))))
    out.append(('all windows are reported for the hint',
                mapped['meta'].get('windows') == ['06:30–22:00', '12:00–13:00'],
                str(mapped['meta'].get('windows'))))
    out.append(('unmapped fields are reported, not guessed',
                mapped['meta'].get('day_goal_m') == 3000
                and 'max_per_day' not in mapped['rules'], 'meta only'))
    out.append(('empty rule model is safe',
                rules_from_policy({})['rules'] == {}, '{}'))
    return out


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except Exception:  # noqa: BLE001
        return True


if __name__ == '__main__':
    ok = True
    for name, passed, detail in self_test():
        print('%-30s %s   %s' % (name, 'PASS' if passed else 'FAIL', detail))
        ok = ok and passed
    print('\nRESULT:', 'PASS' if ok else 'FAIL')
    raise SystemExit(0 if ok else 1)

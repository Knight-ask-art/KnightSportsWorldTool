"""Read-only queries: violation self-check, semester progress, leaderboards.

Ported from YanamiNeko/NekoSportsWorldTool `src/api/cheat.rs`,
`semester.rs` and `rank.rs`.  None of these mutate anything.

    POST api/v78/cheat/cheatlist                违规自查
    POST api/v55/runnings/recordssummary/semester  学期完成度汇总
    POST api/v41/running/getPersonalSemesterInfo   个人完成度
    POST api/v41/rank                              主榜
    POST api/v41/historyRank                       历史榜
    POST api/v43/runnings/indoor/studentRank       室内榜
    POST api/v70230/runnings/records               跑步记录列表

Honest reporting rules (from upstream docs/violation-checks.md):
  * `self: null` means "no violation found", NOT "clean forever";
  * a failed query must never be rendered as "all good";
  * results older than five minutes are stale and should be re-queried.
"""

from __future__ import annotations

import base64
import datetime as _dt
import gzip
import json
import time

from ..domain import coords, safety

CHEAT_PATH = 'api/v78/cheat/cheatlist'
SEMESTER_SUMMARY_PATH = 'api/v55/runnings/recordssummary/semester'
SEMESTER_PERSONAL_PATH = 'api/v41/running/getPersonalSemesterInfo'
RANK_PATH = 'api/v41/rank'
HISTORY_RANK_PATH = 'api/v41/historyRank'
INDOOR_RANK_PATH = 'api/v43/runnings/indoor/studentRank'
RECORDS_PATH = 'api/v70230/runnings/records'

STALE_AFTER_S = 300


class QueryFailed(RuntimeError):
    pass


def _call(client, acct, path, payload=None):
    res = client.request(acct, path, payload or {}, retries=2)
    biz = res.get('json')
    if biz is None:
        raise QueryFailed('%s: %s (HTTP %s)'
                          % (path, res.get('error'), res.get('status')))
    if not isinstance(biz, dict):
        raise QueryFailed('%s: 返回不是对象（%s）'
                          % (path, type(biz).__name__))
    if biz.get('error') not in (None, 10000):
        raise QueryFailed('%s: error=%s %s'
                          % (path, biz.get('error'), biz.get('message')))
    # some endpoints answer {"status": false, "message": "未登录"} with no
    # `error` key at all; that is a failure, not an empty result
    if biz.get('status') is False:
        raise QueryFailed('%s: %s' % (path, biz.get('message') or 'status=false'))
    return biz.get('data')


# --------------------------------------------------------------------------
# violation self-check
# --------------------------------------------------------------------------

def check_violations(client, acct, page: int = 1, page_size: int = 20) -> dict:
    """Never returns a bare "clean" — states are explicit."""
    out = {'checked_at': int(time.time() * 1000), 'state': 'unknown',
           'message': '', 'records': []}
    try:
        data = _call(client, acct, CHEAT_PATH,
                     {'pageNum': page, 'pageSize': page_size})
    except Exception as e:  # noqa: BLE001
        out['state'] = 'failed'
        out['message'] = str(e)
        return out

    d = data if isinstance(data, dict) else {}
    me = d.get('self', '__missing__')
    if me is None or me == {} or me == []:
        out['state'] = 'none_found'
        out['message'] = '本次查询暂未发现违规记录'
    elif me == '__missing__':
        out['state'] = 'unknown'
        out['message'] = '响应中缺少个人违规状态字段，结果未知'
    else:
        out['state'] = 'violations'
        if isinstance(me, dict):
            out['message'] = str(me.get('message') or me.get('reason')
                                  or '存在违规记录')
        else:
            out['message'] = str(me)

    for rec in (d.get('list') or d.get('records') or []):
        if not isinstance(rec, dict):
            continue
        raw_rrid = rec.get('rrid')
        try:
            rrid = int(raw_rrid)
        except (TypeError, ValueError):
            rrid = None
        if rrid is not None and rrid <= 0:
            rrid = None
        out['records'].append({
            'id': rec.get('id'),
            'rrid': rrid,
            'reason': rec.get('reason') or rec.get('cheatReason'),
            'time': rec.get('createTime') or rec.get('time'),
        })
    if out['records']:
        # A non-empty list is authoritative even if `self` is missing or null:
        # do not label a page with rows as "none found".
        if out['state'] != 'violations':
            out['state'] = 'violations'
            out['message'] = '存在违规记录（服务器返回了列表）'
    out['count'] = len(out['records'])
    return out


def is_stale(result: dict, now_ms: int | None = None) -> bool:
    now_ms = now_ms or int(time.time() * 1000)
    return (now_ms - int(result.get('checked_at', 0))) > STALE_AFTER_S * 1000


# --------------------------------------------------------------------------
# semester progress
# --------------------------------------------------------------------------

def semester_summary(client, acct) -> dict:
    try:
        return {'ok': True, 'data': _call(client, acct, SEMESTER_SUMMARY_PATH)}
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'error': str(e)}


def semester_personal(client, acct, run_mode: int | None = None) -> dict:
    payload = {} if run_mode is None else {'runMode': run_mode}
    try:
        return {'ok': True, 'data': _call(client, acct, SEMESTER_PERSONAL_PATH,
                                          payload)}
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'error': str(e)}


# --------------------------------------------------------------------------
# recent records + weekly roll-up
# --------------------------------------------------------------------------
#
# There is no weekly endpoint to call.  The extracted APK only carries
# `api/v37/runnings/recordssummary/all` (an all-time/semester summary; see
# `extracted_constants/官方APK_API端点全集.txt`), and upstream
# (`src/api/semester.rs`) only models the semester summary.  Inventing a
# `?week=` parameter would be guesswork, so the weekly view is derived from
# the one list endpoint we *do* know, and every number below can be traced
# back to a record row.

def _as_list(data) -> list:
    """`data` is sometimes a JSON string, sometimes {list|records: []}."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:  # noqa: BLE001
            return []
    if isinstance(data, dict):
        for key in ('list', 'records', 'rows', 'data'):
            if isinstance(data.get(key), list):
                return data[key]
        return []
    return data if isinstance(data, list) else []


def _num(v, default: float = 0.0) -> float:
    if isinstance(v, bool) or v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _records_page(client, acct, page: int, page_size: int) -> list:
    res = client.request(acct, RECORDS_PATH,
                         {'pageNum': page, 'pageSize': page_size}, retries=2)
    biz = res.get('json')
    if biz is None:
        raise QueryFailed('%s: %s (HTTP %s)'
                          % (RECORDS_PATH, res.get('error'), res.get('status')))
    if not isinstance(biz, dict):
        raise QueryFailed('%s: 返回不是对象（%s）'
                          % (RECORDS_PATH, type(biz).__name__))
    if biz.get('error') not in (None, 10000):
        raise QueryFailed('%s: error=%s %s'
                          % (RECORDS_PATH, biz.get('error'), biz.get('message')))
    # Same trap as `_call`: some builds return {"status": false, "message":
    # "未登录"} with no error key.  Treating it as an empty page made the UI
    # say "no records today" and let the daily cap pass.
    if biz.get('status') is False:
        raise QueryFailed('%s: %s'
                          % (RECORDS_PATH, biz.get('message') or 'status=false'))
    rows = _as_list(biz.get('data'))
    if not rows:
        for key in ('list', 'records', 'rows'):
            rows = _as_list(biz.get(key))
            if rows:
                break
    return rows


def _start_ms(r: dict) -> int:
    ms = _num(r.get('startTime', r.get('start_time', r.get('startAt'))), 0.0)
    if 0 < ms < 1e11:            # seconds -> milliseconds
        ms *= 1000.0
    return int(ms)


def _record_state(complete: bool, normal_run, status, reasons: list,
                  status_code=None) -> str:
    """Classify a row honestly: ok / warn / issue.

    `complete=True` is not enough by itself: the server can accept a record but
    still list a failed reason such as “运动数据有问题”, return
    `normalRun=false`, or mark the list row with `status=2` (“可疑”).  Those
    must not be painted green as “已通过”.

    The list endpoint does not include `statusInfo`/`reasonList`; it only has
    the numeric `status` (observed 0 = normal, 2 = suspicious).  That code is
    therefore the only honest signal available before opening the detail.
    """
    text = str(status or '')
    normal_bad = (normal_run is False
                  or str(normal_run).strip().lower() in ('false', '0'))
    reason_bad = any(not bool(x.get('complete')) for x in (reasons or []))
    text_bad = any(word in text
                   for word in ('问题', '异常', '无效', '违规', '可疑', '失败'))
    code = None
    if status_code is not None and str(status_code).strip() != '':
        try:
            code = int(status_code)
        except (TypeError, ValueError):
            code = None
    code_bad = code == 2
    other_code = code not in (None, 0, 2)
    if normal_bad or reason_bad or text_bad or code_bad:
        return 'issue'
    if other_code:
        return 'warn'
    return 'ok' if complete else 'warn'


def normalise_record(r: dict) -> dict | None:
    """One list row -> the fields a table or chart actually needs.

    Distances are kept in metres by the API (`submit.rs` posts `totalDis` in
    metres) and converted once here, so callers never guess units.
    """
    if not isinstance(r, dict):
        return None
    start = _start_ms(r)
    dist_m = _num(r.get('totalDis') if r.get('totalDis') is not None
                  else r.get('juli') if r.get('juli') is not None
                  else r.get('distance'), 0.0)
    dur_s = _num(r.get('totalTime') if r.get('totalTime') is not None
                 else r.get('sportTime') if r.get('sportTime') is not None
                 else r.get('duration'), 0.0)
    dist_km = dist_m / 1000.0
    pace_s = (dur_s / dist_km) if dist_km > 0 and dur_s > 0 else 0.0

    valid_raw = r.get('validDis')
    reasons = []
    for item in (r.get('reasonList') or []):
        if isinstance(item, dict):
            reasons.append({'reason': item.get('reason') or '',
                            'complete': bool(item.get('complete'))})
        elif isinstance(item, str):
            reasons.append({'reason': item, 'complete': False})

    return {
        'rrid': r.get('rrid') or r.get('id') or r.get('runRecordId'),
        'uuid': r.get('uuid') or '',
        'start_ms': start,
        'date': safety.fmt_beijing(start, '%Y-%m-%d') if start else '',
        'distance_m': round(dist_m, 1),
        'distance_km': round(dist_km, 3),
        'duration_s': int(dur_s),
        'pace_s_per_km': round(pace_s, 1),
        'pace_min_per_km': round(pace_s / 60.0, 2) if pace_s else 0.0,
        'steps': int(_num(r.get('totalSteps') or r.get('stepNumber'))),
        'calorie': int(_num(r.get('calorie'))),
        'avg_step_freq': int(_num(r.get('avgStepFreq'))),
        'complete': bool(r.get('complete')),
        'normal_run': r.get('normalRun'),
        'status': r.get('statusInfo') or '',
        'status_code': r.get('status'),
        'state': _record_state(bool(r.get('complete')), r.get('normalRun'),
                               r.get('statusInfo') or '', reasons,
                               r.get('status')),
        'valid_distance_km': (round(_num(valid_raw) / 1000.0, 3)
                              if valid_raw is not None else None),
        'reasons': reasons,
    }


def _looks_descending(rows: list[dict]) -> bool:
    """Is this page newest-first?  Both orders are accepted, never assumed."""
    stamps = [r['start_ms'] for r in rows if r['start_ms']]
    return len(stamps) < 2 or stamps[0] >= stamps[-1]


def recent_records(client, acct, limit: int = 20,
                   page_size: int = 20) -> dict:
    """Newest-first records, capped at `limit`.

    Read-only and idempotent: safe to call on every page load.
    """
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 20
    page_size = max(1, min(int(page_size or limit), 50))
    # +2 pages of slack: a server may cap pageSize below what we asked for, so
    # "limit rows" does not imply "limit / page_size requests".
    max_pages = min(10, max(1, (limit + page_size - 1) // page_size + 2))

    out: list[dict] = []
    seen = set()
    descending = True
    try:
        for page in range(1, max_pages + 1):
            batch = _records_page(client, acct, page, page_size)
            if not batch:
                break
            recs = [r for r in (normalise_record(raw) for raw in batch) if r]
            if page == 1:
                # if the server is oldest-first, stopping at `limit` would keep
                # the *oldest* rows — collect the pages and slice afterwards
                descending = _looks_descending(recs)
            for rec in recs:
                key = rec.get('rrid') or rec.get('uuid')
                if key and key in seen:
                    continue        # servers occasionally repeat the boundary row
                if key:
                    seen.add(key)
                out.append(rec)
            if descending and len(out) >= limit:
                break
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'records': [], 'error': str(e),
                'message': '记录列表查询失败'}

    out.sort(key=lambda r: r['start_ms'], reverse=True)
    if len(out) > limit:
        out = out[:limit]
    return {'ok': True, 'records': out, 'error': None,
            'message': '' if out else '暂无跑步记录'}


def daily_count(client, acct, day: str,
                limit: int = 60) -> tuple[int | None, str]:
    """How many records the server has on Beijing date ``day``.

    Returns ``(count, 'server')`` when the list is readable, or
    ``(None, 'unknown')`` when it is not.  The caller must distinguish the two:
    a silent 0 would tell the operator "you have not run today" when the truth
    is "we could not check".
    """
    r = recent_records(client, acct, limit=limit)
    if not r.get('ok'):
        return None, 'unknown'
    n = sum(1 for rec in (r.get('records') or [])
            if rec.get('date') == day)
    return n, 'server'


def daily_counts(client, acct, days: list[str], limit: int = 60) -> dict:
    """Count records for several Beijing dates from one list request.

    The backfill date picker needs today..3 days ago.  Calling
    ``daily_count`` four times would fetch the same list four times, so this
    groups one read-only response instead.
    """
    wanted = [str(d) for d in (days or [])]
    r = recent_records(client, acct, limit=limit)
    if not r.get('ok'):
        return {'ok': False, 'counts': {d: None for d in wanted},
                'source': 'unknown',
                'error': r.get('error') or r.get('message')}
    # A readable list with no row for a date means zero, not unknown.
    out = {d: 0 for d in wanted}
    for rec in (r.get('records') or []):
        d = rec.get('date')
        if d in out:
            out[d] += 1
    return {'ok': True, 'counts': out, 'source': 'server',
            'records': len(r.get('records') or [])}


def _week_start(d: _dt.date) -> _dt.date:
    return d - _dt.timedelta(days=d.weekday())


def weekly_summary(client, acct, weeks: int = 4, now_ms: int | None = None,
                   page_size: int = 20, max_pages: int = 10) -> dict:
    """Per-week totals for the last `weeks` ISO weeks (Mon–Sun, Beijing).

    Weeks are zero-filled and returned oldest → newest so a chart never
    silently drops an empty week.  `truncated` says the record list ran out of
    pages before the window was covered — treat those weeks as a lower bound.
    """
    try:
        weeks = max(1, min(int(weeks), 26))
    except (TypeError, ValueError):
        weeks = 4

    now = safety.to_beijing(now_ms if now_ms else safety.now_ms())
    cur_monday = _week_start(now.date())
    first = cur_monday - _dt.timedelta(days=7 * (weeks - 1))

    rows: list[dict] = []
    seen = set()
    truncated = False
    descending = True
    try:
        for page in range(1, max_pages + 1):
            batch = _records_page(client, acct, page, page_size)
            if not batch:
                break
            for raw in batch:
                rec = normalise_record(raw)
                if rec is None:
                    continue
                key = rec.get('rrid') or rec.get('uuid')
                if key and key in seen:
                    continue        # page boundaries can repeat a row
                if key:
                    seen.add(key)
                rows.append(rec)
            if page == 1:
                descending = _looks_descending(rows)
            if descending:
                # only safe when pages run newest -> oldest
                stamps = [r['start_ms'] for r in rows if r['start_ms']]
                if stamps and safety.to_beijing(min(stamps)).date() < first:
                    break
            # a short page is *usually* the last one, but servers cap pageSize,
            # so only an empty page proves there is nothing left
        else:
            truncated = True
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'weeks': [], 'totals': {}, 'error': str(e),
                'message': '周汇总查询失败', 'truncated': False}

    buckets: dict[_dt.date, dict] = {}
    for i in range(weeks):
        start = first + _dt.timedelta(days=7 * i)
        iso = start.isocalendar()
        buckets[start] = {
            'week_start': start.isoformat(),
            'week_end': (start + _dt.timedelta(days=6)).isoformat(),
            'iso_week': '%04d-W%02d' % (iso[0], iso[1]),
            'count': 0,
            'distance_km': 0.0,
            'duration_s': 0,
            'complete_count': 0,
            'active_days': 0,
            'daily': [{'date': (start + _dt.timedelta(days=d)).isoformat(),
                       'count': 0, 'distance_km': 0.0} for d in range(7)],
        }

    excluded = 0
    for rec in rows:
        if not rec['start_ms']:
            excluded += 1
            continue
        day = safety.to_beijing(rec['start_ms']).date()
        if day < first:
            excluded += 1
            continue
        b = buckets.get(_week_start(day))
        if b is None:            # future-dated row; not part of the window
            excluded += 1
            continue
        b['count'] += 1
        b['distance_km'] += rec['distance_km']
        b['duration_s'] += rec['duration_s']
        b['complete_count'] += 1 if rec['complete'] else 0
        slot = b['daily'][day.weekday()]
        slot['count'] += 1
        slot['distance_km'] += rec['distance_km']

    out_weeks = []
    for start in sorted(buckets):
        b = buckets[start]
        for slot in b['daily']:
            slot['distance_km'] = round(slot['distance_km'], 3)
        b['active_days'] = sum(1 for slot in b['daily'] if slot['count'])
        b['distance_km'] = round(b['distance_km'], 3)
        b['avg_pace_min_per_km'] = (
            round(b['duration_s'] / 60.0 / b['distance_km'], 2)
            if b['distance_km'] > 0 else 0.0)
        out_weeks.append(b)

    total_km = round(sum(w['distance_km'] for w in out_weeks), 3)
    total_s = sum(w['duration_s'] for w in out_weeks)
    totals = {
        'count': sum(w['count'] for w in out_weeks),
        'distance_km': total_km,
        'duration_s': total_s,
        'complete_count': sum(w['complete_count'] for w in out_weeks),
        'active_days': sum(w['active_days'] for w in out_weeks),
        'avg_pace_min_per_km': (round(total_s / 60.0 / total_km, 2)
                                if total_km > 0 else 0.0),
    }
    return {'ok': True, 'weeks': out_weeks, 'totals': totals,
            'window': {'from': first.isoformat(),
                       'to': (cur_monday + _dt.timedelta(days=6)).isoformat()},
            'excluded': excluded, 'truncated': truncated, 'error': None,
            'message': '' if totals['count'] else '这段时间没有跑步记录'}


# --------------------------------------------------------------------------
# one record, in full: track + cadence + altitude
# --------------------------------------------------------------------------
#
# Evidence that the detail carries the whole run:
#   * official APK constant `api/v61200/runnings/get_one_record`, next to
#     `calculateBadge` (extracted_constants/api_path_scan.txt);
#   * the app's own UI parses exactly these fields:
#         JSON.parse(JSON.parse(pi.run_data).allLocJson)
#         JSON.parse(pi.fixed_point_json).fivePointJson
#         JSON.parse(pi.segment_json)
#         JSON.parse(pi.runFaceCheck)
#   * upstream `records.rs` calls the same family (v70260) with
#     {"rrid", "uuid", "calculateBadge": false}.
#
# Each value may arrive as plain JSON *or* as base64(gzip(json)) — the app
# ships `decompress_gzip_base64_values` for that reason.

DETAIL_PATHS = ('api/v61200/runnings/get_one_record',
                'api/v70260/runnings/get_one_record')


def _unwrap(v, rounds: int = 4):
    """JSON string / base64-gzip string / dict -> the object inside."""
    for _ in range(rounds):
        if not isinstance(v, str):
            return v
        s = v.strip()
        if not s:
            return v
        if s[0] in '{["':
            try:
                v = json.loads(s)
                continue
            except Exception:  # noqa: BLE001
                return v
        try:
            v = json.loads(gzip.decompress(base64.b64decode(s)).decode('utf-8'))
        except Exception:  # noqa: BLE001
            return v
    return v


def parse_track(raw) -> list[dict]:
    """`allLocJson` -> displayable points.  GCJ-02 is what the map needs."""
    rows = _unwrap(raw)
    if not isinstance(rows, list):
        return []
    out = []
    for p in rows:
        if not isinstance(p, dict):
            continue
        glat = _num(p.get('gLat'), 0.0)
        glng = _num(p.get('gLng'), 0.0)
        blat = _num(p.get('lat'), 0.0)
        blng = _num(p.get('lng'), 0.0)
        if glat and glng:
            lat, lng, crs = glat, glng, 'gcj02'
        elif blat and blng:
            # `lat/lng` in this API are BD-09 (`submit.rs`); without gLat we
            # must convert, otherwise the track sits ~500 m off the map
            lat, lng = coords.bd09_to_gcj02(blat, blng)
            crs = 'bd09->gcj02'
        else:
            continue                     # the app filters gLat/gLng <= 0 too
        alt = p.get('bdA', p.get('alt'))
        out.append({
            'lat': round(lat, 7),
            'lng': round(lng, 7),
            'crs': crs,
            'bd_lat': round(blat, 7),
            'bd_lng': round(blng, 7),
            'altitude': round(_num(alt), 2) if alt is not None else None,
            'speed': _num(p.get('speed')),      # unit unverified: raw only
            'distance_m': round(_num(p.get('totalDis')), 1),
            'duration_s': int(_num(p.get('totalTime'))),
            'steps': int(_num(p.get('steps'))),
            'time': p.get('gainTime') or '',
        })
    return out


def _five_list(raw) -> list:
    fp = _unwrap(raw)
    if isinstance(fp, dict):
        fp = _unwrap(fp.get('fivePointJson'))
    return fp if isinstance(fp, list) else []


def _segments(raw) -> list:
    seg = _unwrap(raw)
    if isinstance(seg, dict):
        seg = (seg.get('segmentJson') or seg.get('segments') or [])
    return seg if isinstance(seg, list) else []


def _fetch_obs_detail(client, acct, rrid) -> dict:
    """Read the OBS object when the server row is only a stub.

    Records uploaded with ``jsonFromObs=1`` keep ``allLocJson`` and
    ``fivePointJson`` empty in the database; the full ``OutdoorDecoder`` JSON
    is at ``run_data/{rrid//1000000}/{rrid}.json``.  Without this fallback the
    detail page says "no track" even though the replay has one.
    """
    from ..transport import obs as OBS

    signed = OBS.sign_url(client, acct, OBS.rrid_key(rrid), method='Get')
    status, body = OBS.get_object_bytes(signed)
    if status != 200:
        raise QueryFailed('OBS GET HTTP %s' % status)
    if not body:
        raise QueryFailed('OBS 对象为空')
    try:
        raw = json.loads(body.decode('utf-8'))
    except Exception as e:  # noqa: BLE001
        raise QueryFailed('OBS JSON 解析失败: %s' % e)
    if not isinstance(raw, dict):
        raise QueryFailed('OBS 对象不是 JSON 对象')
    return raw


def record_detail(client, acct, rrid, uuid=None) -> dict:
    """Everything the detail endpoint exposes for one run."""
    if not rrid:
        return {'ok': False, 'error': '缺少 rrid', 'detail': {}, 'track': [],
                'five_point': [], 'segments': [], 'stats': {}, 'fields': []}

    payload = {'rrid': rrid, 'uuid': uuid, 'calculateBadge': False}
    data, errors = None, []
    for path in DETAIL_PATHS:
        try:
            data = _call(client, acct, path, payload)
            break
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
    if data is None:
        return {'ok': False, 'error': ' | '.join(errors), 'detail': {},
                'track': [], 'five_point': [], 'segments': [], 'stats': {},
                'fields': []}

    obj = _unwrap(data)
    if isinstance(obj, dict):
        inner = _unwrap(obj.get('data'))
        if isinstance(inner, dict) and ('run_data' in inner or 'totalDis' in inner):
            obj = inner
    if not isinstance(obj, dict):
        return {'ok': False, 'error': '详情返回不是对象',
                'detail': {}, 'track': [], 'five_point': [], 'segments': [],
                'stats': {}, 'fields': []}
    if not obj:
        return {'ok': False, 'error': '详情为空',
                'detail': {}, 'track': [], 'five_point': [], 'segments': [],
                'stats': {}, 'fields': []}

    run_data = _unwrap(obj.get('run_data'))
    track = parse_track(run_data.get('allLocJson')
                        if isinstance(run_data, dict) else None)
    if not track:
        track = parse_track(obj.get('allLocJson'))

    five = _five_list(obj.get('fixed_point_json'))
    segs = _segments(obj.get('segment_json'))

    # `jsonFromObs=1` means the row is only a summary and the real track lives
    # in OBS.  Pull it on demand so the detail map is not empty.
    json_from_obs = int(_num(obj.get('jsonFromObs')))
    obs_error = ''
    if json_from_obs and (not track or not five or not segs):
        try:
            obs_obj = _fetch_obs_detail(client, acct, rrid)
        except Exception as e:  # noqa: BLE001
            obs_obj, obs_error = None, str(e)
        if isinstance(obs_obj, dict):
            obs_run = _unwrap(obs_obj.get('run_data'))
            if not track:
                track = parse_track(obs_run.get('allLocJson')
                                    if isinstance(obs_run, dict) else None)
            if not five:
                five = _five_list(obs_obj.get('fixed_point_json'))
            if not segs:
                segs = _segments(obs_obj.get('segment_json'))
        elif not obs_error:
            obs_error = 'OBS 对象为空或格式不对'

    reasons = []
    for item in (obj.get('reasonList') or []):
        if isinstance(item, dict):
            reasons.append({'reason': item.get('reason') or '',
                            'complete': bool(item.get('complete'))})

    def opt_num(key):
        return None if obj.get(key) is None else _num(obj.get(key))

    valid_dis = opt_num('validDis')
    valid_time = opt_num('validTime')
    detail = {
        'rrid': obj.get('rrid') or rrid,
        'uuid': obj.get('uuid') or uuid or '',
        'sport_type': int(_num(obj.get('sportType'))),
        'start_ms': _start_ms(obj),
        'distance_m': round(_num(obj.get('totalDis')), 1),
        'duration_s': int(_num(obj.get('totalTime'))),
        'valid_distance_m': round(valid_dis, 1) if valid_dis is not None else None,
        'valid_time_s': int(valid_time) if valid_time is not None else None,
        'calorie': int(_num(obj.get('calorie'))),
        'avg_power': int(_num(obj.get('avgPower'))),
        'total_steps': int(_num(obj.get('totalSteps'))),
        'total_ascent': int(_num(obj.get('totalAscent'))),
        'total_descent': int(_num(obj.get('totalDescent'))),
        'avg_step_freq': int(_num(obj.get('avgStepFreq'))),
        'speed': opt_num('speed'),           # raw; unit unverified
        'address': obj.get('address') or '',
        'status': obj.get('statusInfo') or '',
        'status_code': obj.get('status'),
        'complete': bool(obj.get('complete')),
        'normal_run': obj.get('normalRun'),
        'state': _record_state(bool(obj.get('complete')), obj.get('normalRun'),
                               obj.get('statusInfo') or '', reasons,
                               obj.get('status')),
        'reasons': reasons,
        'json_from_obs': bool(json_from_obs),
        'obs_error': obs_error,
    }

    alts = [p['altitude'] for p in track if p.get('altitude')]
    stats = {
        'points': len(track),
        'alt_min': round(min(alts), 1) if alts else None,
        'alt_max': round(max(alts), 1) if alts else None,
        'has_altitude': bool(alts),
        'has_cadence': detail['avg_step_freq'] > 0 or detail['total_steps'] > 0,
        'has_track': bool(track),
        'has_obs_track': bool(track and json_from_obs),
        'json_from_obs': bool(json_from_obs),
        'segments': len(segs),
        'five_points': len(five),
    }

    return {'ok': True, 'error': None, 'detail': detail, 'track': track,
            'five_point': five, 'segments': segs, 'stats': stats,
            'fields': sorted(obj.keys())}


# --------------------------------------------------------------------------
# leaderboards
# --------------------------------------------------------------------------

def rank(client, acct, kind: str = 'main', page: int = 1,
         page_size: int = 20, **extra) -> dict:
    path = {'main': RANK_PATH, 'history': HISTORY_RANK_PATH,
            'indoor': INDOOR_RANK_PATH}.get(kind)
    if not path:
        raise ValueError('kind must be main/history/indoor')
    payload = {'pageNum': page, 'pageSize': page_size}
    payload.update(extra)
    try:
        return {'ok': True, 'data': _call(client, acct, path, payload)}
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'error': str(e)}


# --------------------------------------------------------------------------
# self test (offline)
# --------------------------------------------------------------------------

def self_test() -> list[tuple[str, bool, str]]:
    out = []

    out.append(('paths look right',
                CHEAT_PATH.startswith('api/') and RANK_PATH.startswith('api/'),
                CHEAT_PATH))

    # missing field -> unknown, not "clean"
    fake = _FakeClient({'error': 10000, 'data': {'list': []}})
    r = check_violations(fake, object())
    out.append(('missing self -> unknown', r['state'] == 'unknown', r['state']))

    # explicit null -> none_found
    fake = _FakeClient({'error': 10000, 'data': {'self': None, 'list': []}})
    r = check_violations(fake, object())
    out.append(('self null -> none_found', r['state'] == 'none_found', r['state']))

    # explicit empty containers -> none_found
    fake = _FakeClient({'error': 10000, 'data': {'self': {}, 'list': []}})
    r = check_violations(fake, object())
    out.append(('self {} -> none_found', r['state'] == 'none_found', r['state']))

    # a non-empty list wins over a missing/null self field
    fake = _FakeClient({'error': 10000, 'data': {
        'self': None, 'list': [{'rrid': 7, 'reason': 'x'}]}})
    r = check_violations(fake, object())
    out.append(('list wins over null self', r['state'] == 'violations'
                and r['count'] == 1, '%s/%s' % (r['state'], r['count'])))

    # failure is reported as failure
    fake = _FakeClient(None)
    r = check_violations(fake, object())
    out.append(('failure != clean', r['state'] == 'failed', r['state']))

    # staleness
    r2 = {'checked_at': int(time.time() * 1000) - 400 * 1000}
    out.append(('stale detection', is_stale(r2), 'older than 5 min'))
    r3 = {'checked_at': int(time.time() * 1000)}
    out.append(('fresh is not stale', not is_stale(r3), 'now'))

    out.append(('bad rank kind rejected', _raises(lambda: rank(None, None, 'nope')),
                'ValueError'))

    fake = _FakeClient({'error': 10000, 'data': {}})
    r = rank(fake, object(), 'history')
    out.append(('history board resolves its path',
                r['ok'] and fake.calls[-1][0] == HISTORY_RANK_PATH,
                fake.calls[-1][0] if fake.calls else ''))

    # ---- recent records ------------------------------------------------
    def ms(y, m, d, hh=8, mm=0):
        return int(_dt.datetime(y, m, d, hh, mm,
                                tzinfo=safety.BEIJING).timestamp() * 1000)

    rows = [
        {'rrid': 3, 'startTime': ms(2026, 10, 21, 18), 'totalDis': 1500,
         'totalTime': 700, 'totalSteps': 2100, 'complete': False},
        {'rrid': 2, 'startTime': ms(2026, 10, 20, 7, 30), 'totalDis': 2000,
         'totalTime': 900, 'complete': True, 'validDis': 2000},
        {'rrid': 1, 'startTime': ms(2026, 10, 13, 7, 10), 'totalDis': 3000,
         'totalTime': 1500, 'complete': True},
    ]
    fake = _FakeClient(pages=[{'error': 10000, 'data': rows}])
    r = recent_records(fake, object(), limit=20)
    out.append(('records returned', r['ok'] and len(r['records']) == 3,
                len(r.get('records', []))))
    out.append(('records sorted newest first',
                [x['rrid'] for x in r['records']] == [3, 2, 1],
                [x['rrid'] for x in r['records']]))
    out.append(('metres convert to km',
                r['records'][1]['distance_km'] == 2.0,
                r['records'][1]['distance_km']))
    out.append(('pace derived from time/distance',
                r['records'][1]['pace_min_per_km'] == 7.5,
                r['records'][1]['pace_min_per_km']))
    out.append(('local date is Beijing', r['records'][0]['date'] == '2026-10-21',
                r['records'][0]['date']))
    out.append(('valid distance parsed when present',
                r['records'][1]['valid_distance_km'] == 2.0
                and r['records'][0]['valid_distance_km'] is None,
                r['records'][1]['valid_distance_km']))

    fake = _FakeClient(pages=[{'error': 10000, 'data': rows}])
    r = recent_records(fake, object(), limit=2)
    out.append(('limit caps the page count', len(r['records']) == 2, len(r['records'])))

    # dict-shaped data and a JSON-string payload both occur in the wild
    fake = _FakeClient(pages=[{'error': 10000,
                               'data': {'list': rows[:1], 'total': 1}}])
    r = recent_records(fake, object(), limit=5)
    out.append(('dict data with list accepted', len(r['records']) == 1, len(r['records'])))
    fake = _FakeClient(pages=[{'error': 10000, 'data': json.dumps(rows)}])
    r = recent_records(fake, object(), limit=5)
    out.append(('json-string data accepted', len(r['records']) == 3, len(r['records'])))

    # a server that caps pageSize returns short pages that are not the last one
    fake = _FakeClient(pages=[{'error': 10000, 'data': rows[:2]},
                              {'error': 10000, 'data': rows[2:]}])
    r = recent_records(fake, object(), limit=10, page_size=20)
    out.append(('short pages do not stop pagination', len(r['records']) == 3,
                len(r['records'])))

    fake = _FakeClient(None)
    r = recent_records(fake, object())
    out.append(('record failure is not empty success',
                r['ok'] is False and 'error' in r, r['message']))

    # daily_count: a readable list counts that Beijing date; a failure must be
    # "unknown", never 0 (0 would read as "you have not run today").
    fake = _FakeClient(pages=[{'error': 10000, 'data': rows}])
    n, src = daily_count(fake, object(), '2026-10-21')
    out.append(('daily count filters by Beijing date', (n, src) == (1, 'server'),
                (n, src)))
    fake = _FakeClient(None)
    n2, src2 = daily_count(fake, object(), '2026-10-21')
    out.append(('daily count failure stays unknown',
                (n2, src2) == (None, 'unknown'), (n2, src2)))

    # seconds-long timestamps are upgraded to milliseconds
    rec = normalise_record({'rrid': 9, 'startTime': 1792500000})
    out.append(('second timestamps become ms', rec['start_ms'] == 1792500000000,
                rec['start_ms']))

    # A server-accepted record is not necessarily a clean one.  The list row
    # must expose that distinction so the UI can put it in “有问题”.
    issue = normalise_record({
        'rrid': 10, 'startTime': 1792500000, 'totalDis': 1000,
        'totalTime': 600, 'complete': True, 'normalRun': False,
        'reasonList': [{'reason': '运动数据有问题', 'complete': False}]})
    out.append(('failed reason is issue, not ok',
                issue['state'] == 'issue', issue['state']))
    clean = normalise_record({
        'rrid': 11, 'startTime': 1792500000, 'totalDis': 1000,
        'totalTime': 600, 'complete': True, 'normalRun': True,
        'reasonList': [{'reason': '运动数据正常', 'complete': True}]})
    out.append(('clean reason stays ok', clean['state'] == 'ok', clean['state']))
    suspicious = normalise_record({
        'rrid': 13, 'startTime': 1792500000, 'totalDis': 1000,
        'totalTime': 600, 'complete': True, 'status': 2})
    out.append(('list status=2 is issue', suspicious['state'] == 'issue',
                suspicious['state']))
    pending = normalise_record({
        'rrid': 12, 'startTime': 1792500000, 'totalDis': 1000,
        'totalTime': 600, 'complete': False, 'normalRun': True,
        'reasonList': [{'reason': '待复核', 'complete': True}]})
    out.append(('incomplete stays warn', pending['state'] == 'warn', pending['state']))

    # an oldest-first server must still yield the newest rows
    asc = list(reversed(rows))
    fake = _FakeClient(pages=[{'error': 10000, 'data': asc[:2]},
                              {'error': 10000, 'data': asc[2:]}])
    r = recent_records(fake, object(), limit=2, page_size=2)
    out.append(('oldest-first pages keep the newest rows',
                [x['rrid'] for x in r['records']] == [3, 2],
                [x['rrid'] for x in r['records']]))

    # ---- weekly roll-up ------------------------------------------------
    fake = _FakeClient(pages=[{'error': 10000, 'data': rows + [
        {'rrid': 0, 'startTime': ms(2026, 9, 20, 9), 'totalDis': 1000,
         'totalTime': 600, 'complete': True},
    ]}])
    w = weekly_summary(fake, object(), weeks=3, now_ms=ms(2026, 10, 22, 12))
    out.append(('weekly ok', w['ok'] is True, w['message']))
    out.append(('weekly zero-fills the empty week',
                [x['week_start'] for x in w['weeks']]
                == ['2026-10-05', '2026-10-12', '2026-10-19'],
                [x['week_start'] for x in w['weeks']]))
    out.append(('weekly counts land in the right week',
                [x['count'] for x in w['weeks']] == [0, 1, 2],
                [x['count'] for x in w['weeks']]))
    out.append(('weekly distance totals',
                w['weeks'][2]['distance_km'] == 3.5 and w['totals']['distance_km'] == 6.5,
                [x['distance_km'] for x in w['weeks']]))
    out.append(('weekly pace uses summed time/distance',
                w['weeks'][2]['avg_pace_min_per_km'] == 7.62,
                w['weeks'][2]['avg_pace_min_per_km']))
    out.append(('records before the window are excluded', w['excluded'] == 1,
                w['excluded']))
    out.append(('iso week label', w['weeks'][2]['iso_week'] == '2026-W43',
                w['weeks'][2]['iso_week']))
    out.append(('daily slots are seven per week',
                len(w['weeks'][2]['daily']) == 7
                and w['weeks'][2]['daily'][1]['date'] == '2026-10-20',
                w['weeks'][2]['daily'][1]))

    w = weekly_summary(_FakeClient(pages=[{'error': 10000, 'data': rows}]),
                       object(), weeks=1, now_ms=ms(2026, 10, 22, 12))
    out.append(('single-week window', len(w['weeks']) == 1 and w['weeks'][0]['count'] == 2,
                w['weeks'][0]['count']))

    w = weekly_summary(_FakeClient(None), object(), weeks=4)
    out.append(('weekly failure is explicit', w['ok'] is False, w['message']))

    # oldest-first pages must not make the window look empty
    w = weekly_summary(_FakeClient(pages=[{'error': 10000, 'data': asc[:2]},
                                          {'error': 10000, 'data': asc[2:]}]),
                       object(), weeks=3, now_ms=ms(2026, 10, 22, 12),
                       page_size=2)
    out.append(('oldest-first pages still fill the weeks',
                [x['count'] for x in w['weeks']] == [0, 1, 2],
                [x['count'] for x in w['weeks']]))

    # ---- one record in full --------------------------------------------
    def enc(obj):
        return base64.b64encode(gzip.compress(json.dumps(obj).encode())).decode()

    locs = [
        {'gLat': 22.4046, 'gLng': 112.9342, 'lat': 22.4000, 'lng': 112.9300,
         'bdA': 25.5, 'speed': 9.2, 'totalDis': 0, 'totalTime': 0,
         'steps': 0, 'gainTime': '2026-10-20 07:30:00'},
        {'gLat': 22.4047, 'gLng': 112.9343, 'lat': 22.4001, 'lng': 112.9301,
         'bdA': 31.0, 'speed': 9.4, 'totalDis': 500, 'totalTime': 300,
         'steps': 600, 'gainTime': '2026-10-20 07:35:00'},
        {'gLat': 0, 'gLng': 0, 'lat': 0, 'lng': 0},      # must be dropped
    ]
    payload = {
        'rrid': 42, 'uuid': 'u-42', 'totalDis': 2000, 'totalTime': 900,
        'validDis': 1980, 'validTime': 890, 'avgStepFreq': 172,
        'totalSteps': 3520, 'calorie': 131, 'totalAscent': 12,
        'totalDescent': 9, 'avgPower': 88, 'sportType': 1,
        'address': '江门市', 'statusInfo': '有效', 'complete': True,
        'startTime': ms(2026, 10, 20, 7, 30),
        'run_data': json.dumps({'allLocJson': json.dumps(locs)}),
        'fixed_point_json': json.dumps({'fivePointJson': [{'id': 1}]}),
        'segment_json': json.dumps([{'km': 1, 'pace': 7.5}]),
    }
    d = record_detail(_FakeClient(pages=[{'error': 10000, 'data': payload}]),
                      object(), 42, 'u-42')
    out.append(('detail parsed', d['ok'] and d['detail']['rrid'] == 42,
                d['detail'].get('rrid')))
    out.append(('double-encoded track parsed', len(d['track']) == 2, len(d['track'])))
    out.append(('zero-coordinate points dropped', all(p['lat'] for p in d['track']),
                len(d['track'])))

    only_bd = parse_track([{'lat': 22.4000, 'lng': 112.9300}])
    out.append(('bd-only points are converted, not plotted raw',
                len(only_bd) == 1 and only_bd[0]['crs'] == 'bd09->gcj02'
                and abs(only_bd[0]['lat'] - 22.4000) > 0.001,
                only_bd[0] if only_bd else None))
    out.append(('altitude range extracted',
                d['stats']['alt_min'] == 25.5 and d['stats']['alt_max'] == 31.0,
                d['stats']))
    out.append(('cadence / ascent / power exposed',
                d['detail']['avg_step_freq'] == 172
                and d['detail']['total_ascent'] == 12
                and d['detail']['avg_power'] == 88,
                d['detail']['avg_step_freq']))
    out.append(('five points parsed', len(d['five_point']) == 1,
                len(d['five_point'])))
    out.append(('segments parsed', len(d['segments']) == 1, len(d['segments'])))

    gz = dict(payload)
    gz['run_data'] = enc({'allLocJson': locs})
    d2 = record_detail(_FakeClient(pages=[{'error': 10000, 'data': gz}]),
                       object(), 42)
    out.append(('base64-gzip run_data parsed', len(d2['track']) == 2,
                len(d2['track'])))

    nested = {'data': json.dumps(payload)}
    d3 = record_detail(_FakeClient(pages=[{'error': 10000, 'data': nested}]),
                       object(), 42)
    out.append(('nested data envelope unwrapped', len(d3['track']) == 2,
                len(d3['track'])))

    fake = _FakeClient(by_path={'v70260': {'error': 10000, 'data': payload}})
    d4 = record_detail(fake, object(), 42)
    out.append(('falls back to the v70260 path',
                d4['ok'] and fake.calls[-1][0].endswith('v70260/runnings/get_one_record'),
                fake.calls[-1][0] if fake.calls else ''))

    d5 = record_detail(_FakeClient(None), object(), 42)
    out.append(('detail failure is explicit', d5['ok'] is False, d5['error'][:40]))

    out.append(('detail without rrid is rejected',
                record_detail(_FakeClient(None), object(), None)['ok'] is False, ''))

    empty = record_detail(_FakeClient(pages=[{'error': 10000, 'data': {}}]),
                          object(), 42)
    out.append(('empty detail is an error, not a zero record',
                empty['ok'] is False and empty['error'] == '详情为空',
                empty['error']))

    out.append(('status:false counts as failure',
                _raises(lambda: _call(_FakeClient({'status': False,
                                                   'message': '未登录'}),
                                      object(), 'api/x')), '未登录'))
    out.append(('records status:false counts as failure',
                _raises(lambda: _records_page(
                    _FakeClient({'status': False, 'message': '未登录'}),
                    object(), 1, 20)), '未登录'))
    return out


class _FakeClient:
    """Serves a fixed business body, a page sequence, or a path -> body map."""

    def __init__(self, biz=None, pages=None, by_path=None):
        self._biz = biz
        self._pages = list(pages) if pages is not None else None
        self._by_path = dict(by_path) if by_path else None
        self._i = 0
        self.calls = []

    def request(self, acct, path, payload=None, **kw):
        self.calls.append((path, payload))
        if self._by_path is not None:
            key = next((k for k in self._by_path if k in path), None)
            if key is None:
                return {'status': 404, 'json': None, 'error': 'not found'}
            return {'status': 200, 'json': self._by_path[key], 'error': None}
        biz = self._biz
        if self._pages is not None:
            if self._i < len(self._pages):
                biz = self._pages[self._i]
            else:
                biz = {'error': 10000, 'data': []}   # past the last page
            self._i += 1
        return {'status': 200, 'json': biz, 'error': None}


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except Exception:
        return True


if __name__ == '__main__':
    ok = True
    for name, passed, detail in self_test():
        print('%-30s %s   %s' % (name, 'PASS' if passed else 'FAIL', detail))
        ok = ok and passed
    print('\nRESULT:', 'PASS' if ok else 'FAIL')
    raise SystemExit(0 if ok else 1)

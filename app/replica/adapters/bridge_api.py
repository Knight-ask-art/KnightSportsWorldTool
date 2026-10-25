"""Replica backend API surface: the 71 protocol bridge methods (00201.0.0)
plus the real-server extensions used by the UI (receipts, semester, routes,
preflight, diagnostics).

Calling convention mirrors the product: every method returns
{"code": 200|500|600, "message": str, "data": ...}.
Core pipeline (students / sport data / upload / gee) is fully implemented
against the mock servers; admin/proxy/billing call through to mock_65530;
hardware-bound methods (VM download, window controls) are documented stubs.

This is a clean-room reimplementation from observed wire behavior — no
original code is copied.
"""
import datetime as _dt
import json
import os
import tempfile
import threading
import time
import uuid
from urllib.parse import urlparse

import requests

from ..application import credentials as CRED
from ..application import device_profile as DP
from ..build_info import (VERSION as APP_VERSION, build_info,
                          version_string)
from ..domain import crypto as C
from ..domain.rundata import generate_run_data, new_run_uuid
from ..transport.realclient import as_dict

#: Prefer the user's own Web-service key.  Do not ship a hard-coded key in an
#: open-source build; AMap binds keys to a domain/app and a leaked one is a
#: billing and quota risk.
# Fixed checkpoint-query distance for the UI.  Campus checkpoints belong to the
# school, not to the distance the operator plans to run today; the requested
# target must not cause a re-fetch or invalidate the points already displayed.
# Submission still asks the server for the target-specific set and snaps the
# route through any extra required point.
CAMPUS_POINTS_DISTANCE_M = 2500
AMAP_KEY = os.environ.get('JHP_AMAP_KEY', '')
APPLE_DB = {
    'models': [row[0] for row in DP.IOS_MODELS],
    'ios': sorted({v for row in DP.IOS_MODELS for v in row[1]}),
}


def _ok(data=None, message='success'):
    return {'code': 200, 'message': message, 'data': data}


def _fail(message='failed', code=500, data=None):
    return {'code': code, 'message': message, 'data': data}


#: Product-level plausibility band, deliberately wider than any one school's
#: rule set.  Outside it the operator gets a yellow reminder, never a block:
#: the server accepted real runs at 9'06"/km and 112 spm.
PLAUSIBLE_PACE_MIN = 4.5
PLAUSIBLE_PACE_MAX = 8.5
PLAUSIBLE_CADENCE_MIN = 150
PLAUSIBLE_CADENCE_MAX = 180


def plausibility_warnings(pace, cadence) -> list[str]:
    """Soft warnings for parameters outside the common running band."""
    out = []
    try:
        p = float(pace)
    except (TypeError, ValueError):
        p = 0.0
    try:
        c = int(float(cadence))
    except (TypeError, ValueError):
        c = 0
    if p and (p < PLAUSIBLE_PACE_MIN or p > PLAUSIBLE_PACE_MAX):
        out.append('配速 %.2f min/km 超出常见 4′30″–8′30″' % p)
    if c and (c < PLAUSIBLE_CADENCE_MIN or c > PLAUSIBLE_CADENCE_MAX):
        out.append('步频 %d spm 超出常见 150–180' % c)
    return out


def _read_json(path: str, default=None):
    """Corrupt or missing file -> `default`, never a crash at startup."""
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _semester_num(data, *keys):
    """First numeric field among `keys` (accepts numeric strings)."""
    if not isinstance(data, dict):
        return None
    for key in keys:
        if data.get(key) is None:
            continue
        try:
            return float(data[key])
        except (TypeError, ValueError):
            continue
    return None


def _semester_payload_nonempty(data) -> bool:
    """True when a semester payload carries at least one real field.

    An empty ``{}`` from a failed/decoded-empty endpoint used to be treated as
    a successful semester, which made the overview show a blank "本学期" card
    with every value ``None`` instead of the honest failure state.
    """
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:  # noqa: BLE001
            return bool(data.strip())
    if isinstance(data, dict):
        return any(v is not None and v != '' for v in data.values())
    return bool(data)


def semester_view(summary, personal):
    """Normalise the two semester payloads into one small display model.

    The two endpoints answer different questions and must not be mixed up:

    * ``getPersonalSemesterInfo`` owns the **goal** (``goalMethod``).  With
      ``goalMethod=2`` that goal is 室内锻炼 counted in 次 — the official app
      adds it as a separate ring segment, not as the outdoor semester goal.
    * ``recordssummary/semester`` owns the **totals** (``semesterValidDis`` /
      ``semesterDis``, metres) and only answers when it is sent the ``sid``
      taken from the personal payload.

    ``outdoor_km`` therefore reports what was really run this semester, while
    ``indoor_*`` keeps the indoor counter separate.  Distance fields are
    metres; the goal heuristic below still tolerates builds that already send
    kilometres.  Missing fields stay ``None`` so the UI can say "未知"
    instead of inventing zero.
    """
    s = as_dict(summary) if summary is not None else {}
    p = as_dict(personal) if personal is not None else {}
    s = s if isinstance(s, dict) else {}
    p = p if isinstance(p, dict) else {}

    def pick(*keys):
        for src in (p, s):
            v = _semester_num(src, *keys)
            if v is not None:
                return v
        return None

    def pick_summary(*keys):
        """Summary first: ``semesterValidDis`` is the server's running total.

        The personal payload only carries the goal, and its ``curValidDis``
        alias is not always present, so the cumulative numbers must come from
        the summary record when it has them.
        """
        for src in (s, p):
            v = _semester_num(src, *keys)
            if v is not None:
                return v
        return None

    def km(v):
        if v is None:
            return None
        # 1000 m is exactly 1 km; the old ``> 1000`` turned a 1000 m goal into
        # 1000 km.  Values below 1000 are already kilometres in this API.
        return round(v / 1000.0, 2) if v >= 1000 else round(v, 2)

    def metres_to_km(v):
        return round(v / 1000.0, 2) if v is not None else None

    def timestamp_ms(v):
        """Normalise epoch seconds *and* milliseconds to milliseconds."""
        if v is None:
            return None
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        if 0 < n < 1e11:          # seconds -> milliseconds
            n *= 1000
        return n

    raw_goal_distance = pick('semesterGoal', 'requiredDistance',
                             'goalDistance')
    raw_done_distance = pick('curValidDis', 'semesterValidDis',
                             'validTotalDis', 'semesterDis')
    raw_goal_count = pick('semesterGoalCount', 'requiredCount')
    raw_valid_count = pick('semesterValidCount', 'semesterValid',
                           'validTotalCount', 'semesterCount', 'validCount')
    try:
        goal_method = int(pick('goalMethod'))
    except (TypeError, ValueError):
        goal_method = None

    # Cumulative semester distance (metres on the wire).  ``semesterValidDis``
    # is the part the school counted; ``semesterDis`` includes rejected runs.
    outdoor_km = metres_to_km(pick_summary('semesterValidDis', 'validTotalDis',
                                           'curValidDis'))
    outdoor_total_km = metres_to_km(pick_summary('semesterDis'))
    if outdoor_km is None:
        outdoor_km = outdoor_total_km

    indoor_goal = indoor_valid = indoor_remaining = None

    if goal_method == 0:
        # Distance goal: the app compares validTotalDis (metres) with
        # semesterGoal / 1000.  Do not apply the heuristic here: a 500 m
        # goal would otherwise become 500 km.
        goal_km = metres_to_km(raw_goal_distance)
        done_km = (outdoor_km if outdoor_km is not None
                   else metres_to_km(raw_done_distance))
        goal_count = None
        valid_count = None
        goal_unit = 'km'
        goal_kind = 'distance'
    elif goal_method == 2:
        # goalMethod=2 is the 室内锻炼 target, not the outdoor semester goal:
        # the official app renders it as a separate ring segment with the
        # unit 次 (``setIndoorExerciseProgress(semesterValid, semesterGoal,
        # "次")``).  Counting it as the semester goal hid the real outdoor
        # distance and always read "0/1 次".
        indoor_goal = _semester_num(p, 'semesterGoal')
        indoor_valid = _semester_num(p, 'semesterValid')
        if indoor_valid is None:
            indoor_valid = _semester_num(p, 'semesterValidCount')
        if indoor_goal is not None and indoor_valid is not None:
            indoor_remaining = max(0, int(indoor_goal - indoor_valid))
        goal_km = None
        done_km = outdoor_km
        goal_count = None
        valid_count = None
        goal_unit = 'indoor'
        goal_kind = 'indoor'
    elif goal_method == 1:
        # Count goal: semesterGoalCount / validTotalCount, with the plural
        # summary counters as fallback.
        goal_count = p.get('semesterGoalCount')
        if goal_count is None or goal_count <= 0:
            goal_count = raw_goal_count
        valid_count = p.get('validTotalCount')
        if valid_count is None:
            valid_count = raw_valid_count
        goal_km = None
        done_km = None
        goal_unit = 'count'
        goal_kind = 'count'
    else:
        # Older/unknown payloads: keep the old heuristic and expose both views
        # when the server returned both.
        goal_km = km(raw_goal_distance)
        done_km = km(raw_done_distance)
        goal_count = raw_goal_count
        valid_count = raw_valid_count
        goal_unit = ('km' if goal_km is not None
                     else ('count' if goal_count is not None else None))
        goal_kind = ('distance' if goal_km is not None
                     else ('count' if goal_count is not None else None))

    start_ms = timestamp_ms(pick('startDate', 'semesterStartTime',
                                 'mSemesterStartTime'))
    end_ms = timestamp_ms(pick('endDate', 'semesterEndTime',
                               'mSemesterEndTime'))
    pct = pick('completedProportion')
    if pct is not None and 0 < pct <= 1:
        pct *= 100.0
    rank = p.get('rank', s.get('rank'))
    sname = p.get('sname') or s.get('sname') or ''
    remaining_km = (round(max(0.0, goal_km - done_km), 2)
                    if goal_km is not None and done_km is not None else None)
    remaining_count = (max(0, int(goal_count - valid_count))
                       if goal_count is not None and valid_count is not None
                       else None)
    days_left = None
    if end_ms:
        try:
            from ..domain import safety as S
            end_day = S.to_beijing(int(end_ms)).date()
            days_left = (end_day - S.to_beijing(S.now_ms()).date()).days
        except Exception:  # noqa: BLE001
            days_left = None
    return {
        'sname': sname,
        'sid': p.get('sid') or s.get('sid'),
        'goal_method': goal_method,
        'goal_kind': goal_kind,
        'goal_unit': goal_unit,
        'goal_km': goal_km,
        'done_km': done_km,
        'remaining_km': remaining_km,
        'goal_count': int(goal_count) if goal_count is not None else None,
        'valid_count': int(valid_count) if valid_count is not None else None,
        'remaining_count': remaining_count,
        'outdoor_km': outdoor_km,
        'outdoor_total_km': outdoor_total_km,
        'indoor_goal': int(indoor_goal) if indoor_goal is not None else None,
        'indoor_valid': (int(indoor_valid) if indoor_valid is not None
                         else None),
        'indoor_remaining': indoor_remaining,
        'start_ms': int(start_ms) if start_ms else None,
        'end_ms': int(end_ms) if end_ms else None,
        'days_left': days_left,
        'completed_pct': round(pct, 1) if pct is not None else None,
        'rank': rank if rank not in (None, 0, '') else None,
        'source': 'personal' if p else ('summary' if s else None),
    }


def apply_semester_goal(view, target_km, target_sid=None):
    """Fill an outdoor target the server did not provide.

    Only used when neither a distance nor a count goal came back — the normal
    shape of an indoor-only semester.  A server goal always wins, and
    ``goal_source`` records where the target came from so the UI never
    presents a self-set goal as the school's.

    ``target_sid`` binds the local value to one semester: a target saved for
    last term must not be applied to the current one.
    """
    if view.get('goal_km') is not None or view.get('goal_count') is not None:
        view['goal_source'] = 'server'
        return view
    current_sid = view.get('sid')
    if (target_sid not in (None, '') and current_sid not in (None, '')
            and str(target_sid) != str(current_sid)):
        view['goal_source'] = None
        return view
    if target_km in (None, ''):
        view['goal_source'] = None
        return view
    try:
        target = round(float(target_km), 2)
    except (TypeError, ValueError):
        view['goal_source'] = None
        return view
    done = view.get('outdoor_km')
    view['goal_km'] = target
    view['goal_kind'] = 'distance'
    view['goal_unit'] = 'km'
    view['done_km'] = done
    view['goal_count'] = None
    view['valid_count'] = None
    view['remaining_count'] = None
    view['remaining_km'] = (round(max(0.0, target - done), 2)
                            if done is not None else None)
    view['goal_source'] = 'local'
    return view


def _receipt_match(receipt, records):
    """Find the server record that corresponds to a receipt, or None.

    Matches an explicit rrid first, then a start-time window (±3 min) plus a
    distance tolerance (max 120 m or 8%).  Read-only; the caller decides what
    to write back.
    """
    rid = receipt.get('rrid')
    if rid:
        for rec in records:
            if str(rec.get('rrid')) == str(rid):
                return rec
    best = None
    for rec in records:
        start = rec.get('start_ms')
        if not start:
            continue
        try:
            dt = abs(int(start) - int(receipt.get('start') or 0))
        except (TypeError, ValueError):
            continue
        if dt > 180_000:
            continue
        try:
            want = float(receipt.get('distance_m') or 0)
            got = float(rec.get('distance_m') or 0)
        except (TypeError, ValueError):
            want = got = 0.0
        if want and abs(got - want) > max(120.0, want * 0.08):
            continue
        score = dt + abs(got - want) * 100.0
        if best is None or score < best[0]:
            best = (score, rec)
    return best[1] if best else None


def _clean_route(route):
    """Validate one route-library item coming from the UI."""
    if not isinstance(route, dict):
        return None
    name = str(route.get('name') or '未命名路线').strip()[:80]
    typ = route.get('type') if route.get('type') in (
        'draw', 'auto', 'stadium', 'file', 'record') else 'draw'
    raw = route.get('line')
    if not isinstance(raw, list):
        return None
    line = []
    for p in raw[:50000]:
        if isinstance(p, dict):
            a, b = p.get('lat'), p.get('lng', p.get('lon'))
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            a, b = p[0], p[1]
        else:
            continue
        try:
            a, b = float(a), float(b)
        except (TypeError, ValueError):
            continue
        if -90 <= a <= 90 and -180 <= b <= 180:
            line.append([round(a, 7), round(b, 7)])
    if len(line) < 2:
        return None
    return {'name': name, 'type': typ, 'line': line,
            'closed': bool(route.get('closed')),
            'saved_at': str(route.get('saved_at') or '')[:32]}


def _write_json(path: str, data) -> None:
    """Atomic replace: a reader never sees a half-written file."""
    folder = os.path.dirname(path) or '.'
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.tmp-', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class ReplicaApi:
    def __init__(self, iydsj_base: str, backend_base: str, rsa_pub: bytes,
                 store_dir: str):
        self.iydsj = iydsj_base.rstrip('/')
        self.backend = backend_base.rstrip('/')
        self.rsa_pub = rsa_pub
        self.store_dir = os.path.abspath(store_dir)
        os.makedirs(self.store_dir, exist_ok=True)
        # Old builds wrote account/password to account.json in clear text.
        # Convert it before any UI reads the new DPAPI store.
        try:
            CRED.migrate_legacy(self.store_dir)
        except Exception:  # noqa: BLE001
            pass
        # Keep rules/limits/receipts/points-cache on the same data root as the
        # session.  Without this the frozen app read rules from the package
        # directory while the session lived next to the exe.
        os.environ['JHP_DATA_DIR'] = self.store_dir
        os.environ.setdefault('JHP_POINTS_CACHE',
                              os.path.join(self.store_dir,
                                           'points_cache.json'))
        self._students: dict[str, dict] = {}
        self._sport: dict[str, dict] = {}
        self._tasks: dict[str, dict] = {}
        self._geetokens: list[dict] = []
        self._keeper = None
        self._keeper_lock = threading.Lock()
        self._device_lock = threading.RLock()
        self._device_cache = None
        self._load()

    # -- persistence ------------------------------------------------------
    def _paths(self):
        return (os.path.join(self.store_dir, 'students.json'),
                os.path.join(self.store_dir, 'sport.json'))

    def _load(self):
        sp, jp = self._paths()
        st = _read_json(sp, {})
        sp_data = _read_json(jp, {})
        self._students = st if isinstance(st, dict) else {}
        self._sport = sp_data if isinstance(sp_data, dict) else {}

    def _save(self):
        sp, jp = self._paths()
        _write_json(sp, self._students)
        _write_json(jp, self._sport)

    def _post(self, base: str, path: str, payload: dict,
              headers: dict | None = None) -> dict:
        h = {'appVersion': '7.3.70'}
        if headers:
            h.update(headers)
        try:
            r = requests.post(base + path, json=payload,
                              headers=h, timeout=10)
            return r.json()
        except Exception as e:  # noqa: BLE001
            return _fail(f'http error: {e}')

    # -- students ----------------------------------------------------------
    def addStudentAccount(self, username, info=None):
        info = dict(info or {})
        info.setdefault('id', username)
        self._students[username] = info
        self._save()
        return _ok(info)

    def getStudentAccounts(self, _filter=None):
        return _ok(list(self._students.values()))

    def getStudentAccountDetail(self, sid):
        st = self._students.get(sid)
        return _ok(st) if st else _fail('no such student')

    def updateStudentAccount(self, sid, info):
        if sid not in self._students:
            return _fail('no such student')
        self._students[sid].update(info or {})
        self._save()
        return _ok(self._students[sid])

    def deleteStudentAccount(self, sid):
        self._students.pop(sid, None)
        self._save()
        return _ok(True)

    def searchStudents(self, keyword):
        kw = str(keyword or '')
        return _ok([s for s in self._students.values()
                    if kw in json.dumps(s, ensure_ascii=False)])

    def getStudentLoginInfoById(self, sid):
        st = self._students.get(sid)
        if st and st.get('isLoggedIn'):
            return _ok(st.get('loginInfo'))
        return _fail('not logged in')

    def studentLogin(self, username, gee_uuid=None):
        if not gee_uuid:
            return _fail('missing gee validation')
        r = self._post(self.iydsj, '/api/v70100/login',
                       {'username': username, 'gee_uuid': gee_uuid})
        if r.get('code') != 200:
            return r
        st = self._students.get(username, {'id': username})
        st.update({'isLoggedIn': True, 'loginInfo': r['data']})
        self._students[username] = st
        self._save()
        return _ok(r['data'])

    def studentLogout(self, sid):
        st = self._students.get(sid)
        if st:
            st['isLoggedIn'] = False
            self._save()
        self._post(self.iydsj, '/api/v6/user/logout', {'id': sid})
        return _ok(True)

    def login_verify(self, info):
        return _ok(bool(info))

    def check_login_ip(self, ip):
        return _ok({'ip': ip, 'allowed': True})

    # -- gee ---------------------------------------------------------------
    def get_geevalidate(self, result):
        if not result or not result.get('geetest_validate'):
            return _fail('empty gee result')
        return _ok({'uuid': result.get('uuid') or uuid.uuid4().hex,
                    'challenge': result.get('geetest_challenge', '')})

    def getGeetestConfig(self, params=None):
        return _ok({'gt': 'mock-gt', 'challenge': uuid.uuid4().hex,
                    'success': 1})

    def getGeetoken(self):
        if self._geetokens:
            return _ok(self._geetokens[-1])
        return _fail('no pooled geetoken', code=600)

    # -- schools / points / geo ---------------------------------------------
    def getSchoolList(self):
        return _ok(self._post(self.iydsj, '/api/v3/university/get/list', {}).get('data'))

    def getCampusRunPoints(self, school, glat, glon):
        return _ok(self._post(
            self.iydsj, '/api/v560/running/campusZoneList',
            {'school': school, 'glat': glat, 'glon': glon}).get('data'))

    def getCampusRunPointsList(self, unid):
        return _ok(self._post(
            self.iydsj, '/api/v560/running/campusZoneList',
            {'unid': unid}).get('data'))

    def get_location(self, keyword):
        return _ok({'lat': 24.87845, 'lng': 118.673878,
                    'address': str(keyword or '')})

    def get_Apple_inspector(self):
        return _ok(APPLE_DB)

    def getAmapKey(self):
        return _ok({'key': os.environ.get('JHP_AMAP_KEY') or AMAP_KEY})

    @staticmethod
    def _normalise_proxy(value: str) -> str:
        text = str(value or '').strip()
        if not text:
            return ''
        if '://' not in text:
            text = 'http://' + text
        p = urlparse(text)
        if p.scheme not in ('http', 'https') or not p.hostname:
            raise ValueError('代理地址应形如 http://127.0.0.1:7890')
        return text

    def get_network_config(self):
        """Session-only network settings; never return the AMap key raw."""
        key = os.environ.get('JHP_AMAP_KEY') or AMAP_KEY
        proxy = os.environ.get('JHP_PROXY') or ''
        return _ok({
            'proxy': proxy,
            'proxy_present': bool(proxy),
            'amap_key_present': bool(key),
            'amap_key_masked': self._mask_secret(key),
        })

    def set_network_config(self, proxy=None, amap_key=None,
                           clear_proxy=False, clear_amap_key=False):
        """Apply proxy / AMap key to this running process only (no disk).

        The settings page is a visual alternative to the CLI flags and env
        vars.  Values live in `os.environ` for the remainder of the process,
        so screenshots and `data/` never contain a plaintext key.
        """
        global AMAP_KEY
        if clear_proxy:
            os.environ.pop('JHP_PROXY', None)
        elif proxy is not None:
            text = str(proxy).strip()
            if text:
                try:
                    text = self._normalise_proxy(text)
                except ValueError as e:
                    return _fail(str(e))
                os.environ['JHP_PROXY'] = text
            else:
                os.environ.pop('JHP_PROXY', None)
        if clear_amap_key:
            os.environ.pop('JHP_AMAP_KEY', None)
            AMAP_KEY = ''
        elif amap_key is not None:
            text = str(amap_key).strip()
            # Blank means “keep the currently configured key”; use the clear
            # button for explicit removal.
            if text:
                os.environ['JHP_AMAP_KEY'] = text
                AMAP_KEY = text
        return self.get_network_config()

    # -- sport data store ----------------------------------------------------
    def save_sport_data(self, e, t, a, w, i, n, r, o):
        sid = f'sport-{uuid.uuid4().hex[:10]}'
        rec = {'id': sid, 'owner': e, 'distance_km': t, 'pace': a,
               'cadence': w, 'payload': i, 'note': n, 'extra': r,
               'flag': o, 'ts': int(time.time())}
        self._sport[sid] = rec
        self._save()
        return _ok(rec)

    def get_sava_sport_datas(self, unid, page=1, pageSize=20):
        items = [s for s in self._sport.values()
                 if not unid or s.get('owner') == unid]
        page, pageSize = int(page or 1), int(pageSize or 20)
        total = len(items)
        total_pages = max(1, (total + pageSize - 1) // pageSize)
        chunk = items[(page - 1) * pageSize:page * pageSize]
        return _ok({'list': chunk, 'total': total, 'page': page,
                    'pageSize': pageSize, 'totalPages': total_pages})

    def delete_sport_data(self, sid):
        self._sport.pop(sid, None)
        self._save()
        return _ok(True)

    def get_student_records(self, sid, _extra=None):
        r = self._post(self.iydsj, '/api/v70230/runnings/records',
                       {'uid': sid})
        return r if isinstance(r.get('data'), list) else _ok([])

    def get_one_record(self, e, t, a, w):
        rid = e or t or a or w
        return _ok(self._post(
            self.iydsj, '/api/v70320/runnings/get_one_record',
            {'id': rid}).get('data'))

    # -- upload (core) ---------------------------------------------------------
    def uploadRunningData(self, sid, run_data, geetoken):
        """Official-shaped submit: full UploadFormatEntity, locally signed
        (originalSign=getPreSign, signature=getSign over the 31-field map),
        hybrid-encrypted envelope, POST to save/record."""
        from ..domain.rundata import build_upload_entity, sign_upload_entity
        st = self._students.get(sid, {})
        login = st.get('loginInfo') or {}
        ctx = {
            'uid': login.get('uid', sid),
            'geeToken': geetoken or '',
            'faceCheck': 0,
            'policy': 0,
            'sportType': 1,
            'selectedUnid': 0,
            'recordUrl': '',
        }
        ent = sign_upload_entity(build_upload_entity(run_data, ctx))
        inner = {
            'uid': ctx['uid'],
            'token': login.get('token', ''),
            'DeviceId': login.get('DeviceId', ''),
            'current_timestamp': int(time.time() * 1000),
            'geeToken': geetoken or '',
            'entity': ent,
            'run_data_': run_data,
        }
        payload = C.hybrid_encrypt(inner, self.rsa_pub)
        return self._post(self.iydsj, '/api/v70260/runnings/save/record',
                          payload, headers=C.upload_headers())

    def build_run_data(self, distance_km=2.5, pace=6.0, cadence=172,
                       lat=24.87845, lng=118.673878, seed=None):
        import random as _random
        rng = _random.Random(seed) if seed is not None else None
        return generate_run_data(distance_km, pace, cadence, lat, lng, rng=rng)

    # -- replica extras (not in the 71; needed to drive tasks end-to-end) --
    def run_task_submit(self, sid, km=2.5, pace=6.0, geetoken='',
                        cadence=172):
        """Create a run task, build forged data, upload, update task status."""
        t = self._task('run', {'sid': sid, 'km': km})
        tid = t['data']['task_uuid']
        try:
            run = self.build_run_data(float(km), float(pace), int(cadence))
            up = self.uploadRunningData(sid, run, geetoken)
            self._tasks[tid]['status'] = ('done' if up.get('code') == 200
                                          else 'failed')
            self._tasks[tid]['result'] = up
            return up
        except Exception as e:  # noqa: BLE001
            self._tasks[tid]['status'] = 'failed'
            return _fail(str(e))

    # -- tasks ------------------------------------------------------------------
    def _task(self, kind, payload):
        tid = f'{kind}-{uuid.uuid4().hex[:8]}'
        self._tasks[tid] = {'uuid': tid, 'kind': kind, 'status': 'queued',
                            'payload': payload}
        return _ok({'task_uuid': tid})

    def get_run_task_uuid_info(self, tid):
        t = self._tasks.get(tid)
        return _ok(t) if t else _fail('no such task')

    def get_cla_task_uuid_info(self, tid):
        return self.get_run_task_uuid_info(tid)

    def kill_task_uuid(self, tid):
        if tid in self._tasks:
            self._tasks[tid]['status'] = 'killed'
            return _ok(True)
        return _fail('no such task')

    def repeat_run_task_uuid(self, tid):
        t = self._tasks.get(tid)
        if not t:
            return _fail('no such task')
        return self._task(t['kind'], t['payload'])

    # -- local settings -----------------------------------------------------------
    def get_app_set(self):
        return _ok({'base_dir': self.store_dir})

    def get_account_and_password(self):
        """Legacy settings-panel read; now backed by the DPAPI store.

        An existing plaintext ``account.json`` from an older build is migrated
        once, then removed.
        """
        d = CRED.load(self.store_dir)
        if d is None:
            legacy_path = os.path.join(self.store_dir, CRED.LEGACY_FILENAME)
            legacy = _read_json(legacy_path)
            if (isinstance(legacy, dict) and legacy.get('account')
                    and legacy.get('password')):
                try:
                    CRED.save(self.store_dir, legacy['account'],
                              legacy['password'])
                except Exception:  # noqa: BLE001
                    pass
                d = {'account': str(legacy['account']),
                     'password': str(legacy['password'])}
        return _ok(d) if d else _fail('not set')

    def set_account_and_password(self, account, password):
        try:
            CRED.save(self.store_dir, account, password)
        except Exception as e:  # noqa: BLE001
            return _fail('本地加密保存失败: %s' % e)
        return _ok(True)

    def get_saved_login(self):
        """Return the DPAPI-protected login for the login page.

        The bridge is loopback-only and origin-checked; the password is needed
        only to prefill the password field after the operator opted in.
        """
        d = CRED.load(self.store_dir)
        if not d:
            return _ok({'saved': False, 'account': '', 'password': ''})
        return _ok({'saved': True, 'account': d['account'],
                    'password': d['password']})

    def save_login(self, account, password, remember=True):
        if not remember:
            try:
                CRED.clear(self.store_dir)
            except Exception as e:  # noqa: BLE001
                return _fail('清除本地保存失败: %s' % e)
            return _ok({'saved': False})
        try:
            CRED.save(self.store_dir, account, password)
        except Exception as e:  # noqa: BLE001
            return _fail('本地加密保存失败: %s' % e)
        return _ok({'saved': True})

    def clear_saved_login(self):
        try:
            CRED.clear(self.store_dir)
        except Exception as e:  # noqa: BLE001
            return _fail('清除本地保存失败: %s' % e)
        return _ok({'saved': False})

    def get_user_proxy(self):
        return _ok({'ip': '', 'port': 0})

    def set_user_proxy(self, ip, port):
        return _ok({'ip': ip, 'port': port})

    def get_TTT(self):
        return _ok(True)

    def checkRequiredFields(self, obj):
        return _ok(bool(obj))

    def open_dirs(self, path):
        return _ok({'path': path})

    # -- backend passthrough (65530) ------------------------------------------------
    def _be(self, path, payload):
        return self._post(self.backend, path, payload)

    def adminLogin(self, a, b):
        return self._be('/api/adminLogin', {'account': a, 'password': b})

    def admini_changePassword(self, a, b, c):
        return self._be('/api/admini_changePassword',
                        {'account': a, 'old': b, 'new': c})

    def admini_getAccountBalance(self, a, b):
        return self._be('/api/admini_getAccountBalance', {'id': a, 'extra': b})

    def checkForUpdates(self):
        return self._be('/api/checkForUpdates', {})

    def downloadUpdate(self, info):
        return self._ok({'started': True, 'info': info})

    def getUpdateProgress(self, tid):
        return _ok({'task': tid, 'percent': 100})

    def installUpdate(self, path, args):
        return _fail('installer not bundled in replica')

    def checkRechargeStatus(self, a, b, c, d, e, f):
        return self._be('/api/checkRechargeStatus',
                        {'a': a, 'b': b, 'c': c, 'd': d, 'e': e, 'f': f})

    def checkSecretKeyStatus(self, a, b):
        return self._be('/api/checkSecretKeyStatus', {'key': a, 'extra': b})

    def redeemSecretKey(self, a, b):
        return self._be('/api/redeemSecretKey', {'id': a, 'secret': b})

    def getUsdtExchangeRate(self):
        return _ok({'rate': 7.2, 'chain': 'ERC20'})

    def transfer_mileage(self, a, b, c, d):
        return self._be('/api/transfer_mileage',
                        {'id': a, 'password': b, 'to': c, 'km': d})

    def get_users_ordrs(self, a, b, c, d, e):
        return self._be('/api/get_users_ordrs',
                        {'a': a, 'b': b, 'page': c, 'd': d, 'e': e})

    def addProxyAgent(self, a, b, c, d):
        return self._be('/api/addProxyAgent', {'a': a, 'b': b, 'c': c, 'd': d})

    def deleteProxyAgent(self, a, b, c):
        return self._be('/api/deleteProxyAgent', {'id': a, 'b': b, 'c': c})

    def getProxyAgents(self, a, b, c=1):
        return self._be('/api/getProxyAgents', {'a': a, 'b': b, 'page': c})

    def queryProxyUser(self, a, b, c):
        return self._be('/api/queryProxyUser', {'a': a, 'b': b, 'c': c})

    def updateProxyAgentStatus(self, a, b, c, d):
        return self._be('/api/updateProxyAgentStatus',
                        {'a': a, 'b': b, 'c': c, 'd': d})

    def getProxyRechargeAddress(self, a, b):
        return self._be('/api/getProxyRechargeAddress', {'a': a, 'b': b})

    def getProxyRechargeInfo(self, a, b):
        return self._be('/api/getProxyRechargeInfo', {'a': a, 'b': b})

    def updateProxyRechargeAddress(self, a, b, c, d, e, f):
        return self._be('/api/updateProxyRechargeAddress',
                        {'a': a, 'b': b, 'c': c, 'd': d, 'e': e, 'f': f})

    # ==================== TOOL EXTENSIONS (real-server workflow) ====================

    def _session_path(self):
        return os.path.join(self.store_dir, 'accounts_emu.json')

    def _read_session(self) -> dict:
        """Missing or half-written file -> {}, never a crash."""
        d = _read_json(self._session_path(), {})
        return d if isinstance(d, dict) else {}

    def _write_session(self, d: dict) -> None:
        """Atomic replace: the server is threaded, a reader must never see
        a truncated file (that would look like "token missing" mid-run)."""
        _write_json(self._session_path(), d)

    def _device_profile(self, session: dict | None = None) -> dict:
        """The installation profile, loaded once per process.

        The cache is deliberately not refreshed on every request: a profile
        change must be an explicit rotate + restart, not a quiet side effect.
        """
        with self._device_lock:
            if self._device_cache is None:
                if session is None:
                    session = self._read_session()
                self._device_cache = DP.load_or_create(
                    self.store_dir, legacy_session=session)
            return self._device_cache

    def _effective_device(self, session: dict | None = None) -> dict:
        """Profile + any explicit session override (manual DeviceId)."""
        if session is None:
            session = self._read_session()
        return DP.effective_device(self._device_profile(session), session)

    def _invalidate_device(self) -> None:
        with self._device_lock:
            self._device_cache = None

    @staticmethod
    def _mask_secret(value: str) -> str:
        value = str(value or '')
        if not value:
            return ''
        if len(value) <= 8:
            return value[:2] + '…'
        return value[:4] + '…' + value[-4:]

    def _public_session(self, d: dict) -> dict:
        """Session shape for the browser: never return token/DeviceId raw.

        The UI only needs to know whether a secret is present and how to mask
        it.  Returning the whole JSON made every page load a credential leak
        into devtools and any screenshot of the network panel.
        """
        d = d if isinstance(d, dict) else {}
        raw_device = ('DeviceId', 'deviceName', 'osVersion', 'cpuModel',
                      'physicPixel', 'logicPixel', 'isRoot', 'idfa',
                      'platform', 'appInstallTime', 'appUpdateTime')
        out = {k: v for k, v in d.items()
               if k not in ('token',) + raw_device}
        out['token_present'] = bool(d.get('token'))
        out['token_masked'] = self._mask_secret(d.get('token'))
        try:
            profile = self._device_profile(d)
            out.update(DP.public_view(profile, d))
        except Exception as e:  # noqa: BLE001
            # A corrupt profile must not crash the login gate.  Keep the
            # session visible, but never claim a device is ready.
            out['device_id_present'] = False
            out['device_id_masked'] = ''
            out['device_name'] = ''
            out['device_os'] = ''
            out['device_source'] = 'error'
            out['device_error'] = str(e)
        return out

    def real_get_session(self):
        return _ok(self._public_session(self._read_session()))

    def real_set_session(self, uid=None, token=None, campusName=None,
                         schoolName=None, unid=None, lat=None, lon=None,
                         DeviceId=None, name=None, nickname=None,
                         deviceName=None, osVersion=None, cpuModel=None,
                         physicPixel=None, logicPixel=None, isRoot=None,
                         appInstallTime=None, appUpdateTime=None,
                         platform=None, idfa=None):
        if token:
            # A new session must not inherit the previous account's keeper
            # thread or in-memory password.
            self._stop_keeper()
        d = self._read_session()
        if token:
            # Different account => clear every identity field first.  The old
            # code only overwrote non-empty arguments, so a new login could
            # inherit the previous uid/campus/device/coordinates.
            old_state = d.get('keepalive')
            d = {'keepalive': old_state if isinstance(old_state, dict) else {}}
            d['keepalive']['auto_relogin'] = False
        for k, v in (('uid', uid), ('token', token),
                     ('campusName', campusName), ('schoolName', schoolName),
                     ('unid', unid), ('lat', lat), ('lon', lon),
                     ('DeviceId', DeviceId), ('name', name),
                     ('nickname', nickname),
                     ('deviceName', deviceName), ('osVersion', osVersion),
                     ('cpuModel', cpuModel), ('physicPixel', physicPixel),
                     ('logicPixel', logicPixel), ('isRoot', isRoot),
                     ('platform', platform), ('idfa', idfa),
                     ('appInstallTime', appInstallTime),
                     ('appUpdateTime', appUpdateTime)):
            if v not in (None, ''):
                d[k] = v
        if token:
            # An explicit new token is a deliberate re-enable.  The old
            # account was disabled by the user; only a new login/token can
            # bring a session back.
            d.pop('disabled', None)
            d.pop('disabled_reason', None)
            d.pop('disabled_at', None)
            state = d.get('keepalive')
            if not isinstance(state, dict):
                state = {}
                d['keepalive'] = state
            state['enabled'] = False
            state['next_check_at'] = 0
            state['token_updated_at'] = int(time.time())
            state['token_expired'] = False
            state['last_error'] = ''
        self._write_session(d)
        self._invalidate_device()
        return _ok(self._public_session(d))

    def real_rotate_device(self, confirm='', platform=None):
        """Advanced one-time device rotation.

        The caller must send the literal confirmation string.  The local
        session is ended first so the old token is never reused with the new
        device; ``device_profile.rotate`` backs up the old fingerprint.  The
        operator must log in again after this call.

        ``platform`` is ``None``/``random`` (use the weighted pool),
        ``android`` or ``ios``.
        """
        if confirm != '更换设备指纹':
            return _fail('确认文字不正确')
        if not self._read_session().get('token'):
            return _fail('先登录再更换设备指纹')
        platform = str(platform or 'random').strip().lower()
        if platform not in ('random', 'android', 'ios'):
            return _fail('platform 必须是 random、android 或 ios')
        wanted = None if platform == 'random' else platform
        self.real_logout(local_only=True)
        try:
            new, backup = DP.rotate(self.store_dir, force=True,
                                    platform=wanted)
        except Exception as e:  # noqa: BLE001
            return _fail(f'更换设备指纹失败: {e}')
        self._invalidate_device()
        return _ok({
            'device': DP.public_view(new),
            'backup': os.path.basename(backup) if backup else '',
            'relogin_required': True,
        })

    def _real_account(self):
        """Build a realclient.Account from the stored session."""
        from ..transport.realclient import Account
        d = self._read_session()
        # A disabled session is never used, even if a stale token survived.
        if d.get('disabled') or not d.get('token'):
            return None
        acct = Account(
            name=d.get('name', 'emu'),
            uid=str(d.get('uid') or ''),
            token=d['token'],
            schoolName=d.get('schoolName') or '',
            campusName=d.get('campusName') or '',
            unid=int(d.get('unid') or 0),
        )
        DP.apply_to_account(acct, self._effective_device(d))
        if d.get('lat') and d.get('lon'):
            acct.center_lat = float(d['lat'])
            acct.center_lng = float(d['lon'])
        return acct

    def real_check_session(self):
        """Verify the stored token against the real server (envelope-decoded).

        Uses GET api/v70100/user/info — the cheapest endpoint that proves both
        the token AND our crypto stack are live, because its reply is a real
        NetSecKit envelope that we must decrypt with the native w1228 entry.
        """
        acct = self._real_account()
        if acct is None:
            return _fail('未配置 token（先在"账号"面板保存）')
        from ..transport import realclient as R
        try:
            rc = R.RealClient()
            res = rc.request(acct, 'api/v70100/user/info', method='GET',
                             retries=2)
        except Exception as e:  # noqa: BLE001
            return _fail(f'sidecar 启动失败: {e}')
        status = res.get('status')
        if res.get('error'):
            return _fail(f'解密失败: {res["error"]} (HTTP {status}, {res["raw_len"]}B)')
        if status in (401, 403):
            return _fail(f'服务端拒绝: HTTP {status}')
        if status is None or (isinstance(status, int) and status >= 500):
            return _fail(f'服务端异常: HTTP {status}')
        obj = res.get('json')
        if not isinstance(obj, dict) or not obj:
            return _fail(f'服务端返回为空 (HTTP {status})')
        if obj.get('status') is False:
            return _fail(f'服务端拒绝: {obj.get("message") or "status=false"} '
                         f'(HTTP {status})')
        if obj.get('error') != 10000:
            return _fail(f'服务端拒绝: error={obj.get("error")} '
                         f'{obj.get("message")} (HTTP {status})')
        d = as_dict(obj.get('data'))
        # Keep the local session's identity in sync with the authoritative
        # user/info response.  This matters after a campus change and for the
        # checkpoint/fence calls, which use unid and center coordinates.
        session = self._read_session()
        changed = False
        for key in ('uid', 'name', 'nickname', 'campusName', 'schoolName',
                    'unid'):
            value = d.get(key)
            if value not in (None, '') and session.get(key) != value:
                session[key] = value
                changed = True
        if changed:
            self._write_session(session)
        return _ok({
            'verdict': 'accepted',
            'status': status,
            'uid': d.get('uid'),
            'name': d.get('name'),
            'nickname': d.get('nickname'),
            'schoolName': d.get('schoolName'),
            'campusName': d.get('campusName'),
            'gradeClass': d.get('gradeClass'),
            'unid': d.get('unid'),
            'height': d.get('height'),
            'weight': d.get('weight'),
        })

    @staticmethod
    def _clear_session_identity(d: dict) -> dict:
        """Remove every account identity field but keep the keepalive shell."""
        for key in ('token', 'uid', 'name', 'nickname', 'campusName',
                    'schoolName', 'unid', 'lat', 'lon', 'DeviceId',
                    'username', 'phone', 'mobile', 'password', 'pwd', 'avatar'):
            d.pop(key, None)
        for key in ('disabled', 'disabled_reason', 'disabled_at'):
            d.pop(key, None)
        state = d.get('keepalive')
        if not isinstance(state, dict):
            state = {}
        state['enabled'] = False
        state['next_check_at'] = 0
        state['token_expired'] = False
        state['last_error'] = ''
        d['keepalive'] = state
        return d

    @staticmethod
    def _remote_logout_background(acct) -> None:
        """Best-effort server logout after the local gate is already closed."""
        try:
            from ..transport import realclient as R
            R.RealClient().logout_and_verify(acct)
        except Exception:  # noqa: BLE001
            pass

    def real_preflight(self, campus_name=None, school_name=None,
                       start_ms=None, expect_city=None):
        """Explicit network/clock preflight (queries the public IP).

        The UI calls this only when the operator presses "网络自检"; it is not
        part of login/submit, so normal use does not leak network location.
        """
        acct = self._real_account()
        if acct is None:
            return _fail('未配置 token（先在"账号"面板保存）')
        from ..application import riskcheck as RK
        from ..domain import safety as S
        campus = campus_name if campus_name is not None else (acct.campusName or '')
        school = school_name if school_name is not None else (acct.schoolName or '')
        try:
            ms = int(start_ms) if start_ms is not None else S.now_ms()
        except (TypeError, ValueError):
            ms = S.now_ms()
        try:
            r = RK.preflight(campus, school, start_ms=ms,
                             expect_city=expect_city,
                             explicit_start=True)
        except Exception as e:  # noqa: BLE001
            return _fail('网络自检失败: %s' % e)
        r = dict(r or {})
        r['campus'] = campus
        r['school'] = school
        r['start_text'] = S.fmt_beijing(ms)
        return _ok(r)

    def real_logout(self, local_only=False):
        """Always end the local session; report server verification separately.

        “随登随退” is the product rule: a network failure must never trap the
        operator in a logged-in UI.  With `local_only=True` (the desktop UI),
        the local token/identity is cleared immediately and the server logout
        runs in a daemon thread; the UI does not wait for the network.

        The default synchronous path still proves `stage == 'verified'` for
        callers that need that proof.  A failed verification is reported as
        `verified: False` instead of pretending it worked.
        """
        self._stop_keeper()
        d = self._read_session()
        try:
            acct = self._real_account()
        except Exception:  # noqa: BLE001
            # A corrupt device profile must never trap the operator in a
            # logged-in UI.  Local logout still clears token/identity; the
            # server logout is simply skipped.
            acct = None

        if local_only:
            d = self._clear_session_identity(d)
            self._write_session(d)
            self._invalidate_device()
            if acct is not None:
                threading.Thread(target=self._remote_logout_background,
                                 args=(acct,), daemon=True).start()
            return _ok({'verified': False, 'local_only': True,
                        'background': acct is not None,
                        'message': '已清除本机登录；服务端注销在后台尝试'})

        res = None
        verify_error = ''
        if acct is not None:
            from ..transport import realclient as R
            try:
                res = R.RealClient().logout_and_verify(acct)
            except Exception as e:  # noqa: BLE001
                verify_error = str(e)

        # Clear local identity unconditionally.  The UI returns to the login
        # page even if the server is unreachable or still considers the token
        # alive; no local credential remains to use that token.
        d = self._clear_session_identity(d)
        self._write_session(d)
        self._invalidate_device()

        if acct is None:
            return _ok({'verified': False, 'local_only': True,
                        'message': '本地没有可用的登录会话，已清理本地会话'})
        if res and res.get('ok'):
            return _ok({'verified': True, 'stage': res.get('stage'),
                        'logout_status': (res.get('logout') or {}).get('status'),
                        'verify_status': (res.get('verify') or {}).get('status')})

        stage = (res or {}).get('stage') or ('error' if verify_error else 'unknown')
        warning = ('服务端退出验证未完成（token 可能仍有效），已清除本机登录'
                   + (('：' + verify_error) if verify_error else ''))
        return _ok({'verified': False, 'local_only': True, 'stage': stage,
                    'warning': warning,
                    'logout_status': ((res or {}).get('logout') or {}).get('status'),
                    'verify_status': ((res or {}).get('verify') or {}).get('status')})

    # -- token keep-alive ------------------------------------------------
    def _stop_keeper(self):
        with self._keeper_lock:
            if self._keeper is not None:
                try:
                    self._keeper.stop(clear_credentials=True)
                except Exception:  # noqa: BLE001
                    pass
                self._keeper = None

    def _keeper_instance(self):
        from functools import partial

        from ..application.tokenkeeper import (DEFAULT_INTERVAL_S, TokenKeeper,
                                               probe_real_session,
                                               relogin_real)
        with self._keeper_lock:
            if self._keeper is None:
                d = self._read_session()
                state = d.get('keepalive')
                if not isinstance(state, dict):
                    state = {}
                self._keeper = TokenKeeper(
                    self._session_path(),
                    probe=partial(probe_real_session,
                                  store_dir=self.store_dir),
                    relogin=partial(relogin_real, store_dir=self.store_dir),
                    interval_s=state.get('interval_s') or DEFAULT_INTERVAL_S,
                    auto_relogin=bool(state.get('auto_relogin')),
                )
            return self._keeper

    def real_keepalive_status(self):
        """Safe status: no token, uid, password or raw response."""
        try:
            return _ok(self._keeper_instance().status())
        except Exception as e:  # noqa: BLE001
            return _fail('读取保活状态失败: %s' % e)

    def real_keepalive_once(self):
        """Run one lightweight probe now (used by the UI '立即检查')."""
        try:
            res = self._keeper_instance().run_once(force=True)
        except Exception as e:  # noqa: BLE001
            return _fail('保活检查失败: %s' % e)
        if res.get('ok'):
            return _ok(res)
        return _fail(res.get('message') or '保活检查失败', data=res)

    def real_keepalive_start(self, interval_s=None, auto_relogin=None,
                             username=None, password=None):
        """Start the background probe.

        ``username``/``password`` are held in memory only and never written to
        disk.  Auto-relogin is opt-in; without credentials the keeper can still
        probe and detect expiry, but it cannot renew the token.
        """
        try:
            keeper = self._keeper_instance()
            if auto_relogin and not (username and password):
                return _fail('自动重登需要本次会话的手机号和密码（不落盘）')
            keeper.configure(interval_s=interval_s,
                             auto_relogin=auto_relogin,
                             username=username, password=password)
            res = keeper.start()
        except Exception as e:  # noqa: BLE001
            return _fail('开启保活失败: %s' % e)
        if res.get('ok'):
            return _ok(res)
        return _fail(res.get('message') or '开启保活失败', data=res)

    def real_keepalive_stop(self):
        try:
            res = self._keeper_instance().stop(clear_credentials=True)
        except Exception as e:  # noqa: BLE001
            return _fail('停止保活失败: %s' % e)
        return _ok(res)

    def real_keepalive_resume(self):
        """Resume probe-only keep-alive after a server restart.

        A password is intentionally not persisted, so auto-relogin resumes
        only after the user enters it again.  A normal probe can still run.
        """
        try:
            d = self._read_session()
            state = d.get('keepalive')
            if not isinstance(state, dict) or not state.get('enabled'):
                return _ok({'resumed': False, 'reason': 'disabled'})
            if d.get('disabled') or not d.get('token'):
                return _ok({'resumed': False, 'reason': 'no session'})
            res = self._keeper_instance().start()
            return _ok({'resumed': bool(res.get('ok')),
                        'status': res.get('status')})
        except Exception as e:  # noqa: BLE001
            return _fail('恢复保活失败: %s' % e)

    def real_login(self, username, password):
        """Pure-protocol login: GT4 slider solved locally, no emulator.

        On success the session is written to the same store as `real_set_session`,
        so the rest of the UI works unchanged.
        """
        try:
            from ..application.login_flow import login as do_login
        except Exception as e:  # noqa: BLE001
            return _fail(f'登录模块不可用: {e}')
        try:
            raw_session = self._read_session()
            device = self._effective_device(raw_session)
            sess = do_login(username, password, log=lambda *_: None,
                            device=device)
        except Exception as e:  # noqa: BLE001
            return _fail(f'登录失败: {e}')
        profile = sess.get('profile') or {}
        # A manual per-account override (CLI/import) must survive the new
        # token write.  Copy only fields that were explicitly in the session;
        # copying the merged profile would shadow a later rotate().
        extra = {}
        if device.get('source') == 'session':
            extra['DeviceId'] = device['DeviceId']
            for key in ('deviceName', 'osVersion', 'cpuModel',
                        'physicPixel', 'logicPixel', 'isRoot',
                        'platform', 'idfa',
                        'appInstallTime', 'appUpdateTime'):
                if raw_session.get(key) not in (None, ''):
                    extra[key] = raw_session[key]
        saved = self.real_set_session(
            uid=sess['uid'], token=sess['token'],
            name=profile.get('name'),
            nickname=profile.get('nickname'),
            schoolName=profile.get('schoolName'),
            campusName=profile.get('campusName'),
            unid=sess.get('unid'), **extra)
        data = dict(sess.get('profile') or {})
        data.pop('token', None)
        data['session'] = (saved or {}).get('data')
        return _ok(data)

    def real_fetch_points(self, school='', glat=0.0, glon=0.0, unid=None,
                          force=False, distance_m=CAMPUS_POINTS_DISTANCE_M):
        """Fetch the campus geofence + campus list from the real server.

        Both replies are real NetSecKit envelopes, decoded via the native w1228
        entry (the earlier "DEC unsolved" note no longer applies).
        """
        acct = self._real_account()
        if acct is None:
            return _fail('未配置 token（先在"账号"面板保存）')
        from ..transport import realclient as R
        if unid is not None:
            acct.unid = int(unid)
        try:
            rc = R.RealClient()
        except Exception as e:  # noqa: BLE001
            return _fail(f'sidecar 启动失败: {e}')

        try:
            dist_m = max(1, int(round(float(distance_m))))
        except (TypeError, ValueError):
            dist_m = CAMPUS_POINTS_DISTANCE_M
        out = {'unid': acct.unid, 'fence': None, 'campuses': None,
               'polygon': None, 'center': None, 'errors': [],
               'distance_m': dist_m}

        # 1) geofence polygon (this is what the track must stay inside)
        try:
            res = rc.request(acct, 'api/v1/getGeoFenceForRun', {}, retries=2)
            obj = res.get('json') or {}
            if obj.get('error') == 10000:
                d = as_dict(obj.get('data'))
                fences = d.get('geoFences') or []
                out['fence'] = fences
                if fences and isinstance(fences[0], dict):
                    pts = fences[0].get('points') or []
                    # The fence endpoint may fill only glat/glon, only
                    # lat/lon, or BD coordinates; pair_gcj normalises all
                    # three.  Reading glat/glon directly produced an empty
                    # polygon for other campuses.
                    from ..domain import coords
                    poly = []
                    for p in pts:
                        if not isinstance(p, dict):
                            continue
                        xy = coords.pair_gcj(p)
                        if xy is None:
                            continue
                        poly.append({'n': p.get('pointsNumber'),
                                     'lat': xy[0], 'lon': xy[1]})
                    out['polygon'] = poly
                    if poly:
                        out['center'] = {
                            'lon': round(sum(p['lon'] for p in poly) / len(poly), 6),
                            'lat': round(sum(p['lat'] for p in poly) / len(poly), 6),
                        }
            else:
                out['errors'].append(f'fence: error={obj.get("error")} {obj.get("message")}')
        except Exception as e:  # noqa: BLE001
            out['errors'].append(f'fence: {e}')

        # 2) campus list for this school
        try:
            res = rc.request(acct, f'api/v61101/campus/zonelist?unid={acct.unid}',
                             method='GET', retries=2)
            obj = res.get('json') or {}
            if obj.get('error') == 10000:
                out['campuses'] = obj.get('data')
            else:
                out['errors'].append(f'zonelist: error={obj.get("error")} {obj.get("message")}')
        except Exception as e:  # noqa: BLE001
            out['errors'].append(f'zonelist: {e}')

        # 3) live checkpoints (the 必经点位 the replay draws)
        try:
            from ..application import points as P
            pts = P.fetch_points(rc, acct,
                                 out['center']['lat'] if out['center'] else acct.center_lat,
                                 out['center']['lon'] if out['center'] else acct.center_lng,
                                 force=bool(force), distance_m=dist_m)
            out['checkpoints'] = pts
            out['mandatory'] = [p for p in pts if p['isMustPoint']]
            out['route_order'] = P.route_order(
                pts,
                out['center']['lat'] if out['center'] else acct.center_lat,
                out['center']['lon'] if out['center'] else acct.center_lng)
        except Exception as e:  # noqa: BLE001
            out['errors'].append(f'points: {e}')

        if out['center'] or out.get('checkpoints'):
            return _ok(out)
        return _fail('; '.join(out['errors']) or '未取到围栏', data=out)

    def preview_route(self, km=2.5, pace=6.0, cadence=172, waypoints=None,
                      closed=True):
        """Local track preview (no network): plan through waypoints."""
        try:
            wp = [(float(a), float(b)) for a, b in (waypoints or [])]
            run = generate_run_data(float(km), float(pace), int(cadence),
                                    waypoints=wp or None,
                                    closed=bool(closed))
            return _ok({
                'allLocJson': run['allLocJson'],
                'km': run['km'],
                'sportTime': run['sportTime'],
                'juli': run['juli'],
                'points': len(run['allLocJson']),
            })
        except Exception as e:  # noqa: BLE001
            return _fail(f'路线生成失败: {e}')

    def get_school_rules(self):
        """User-entered school rules; the model is disabled until configured."""
        from ..application import qualification as Q
        rules = Q.load_rules()
        return _ok({'rules': rules, 'configured': Q.is_configured(rules)})

    def get_semester_goal(self, sid=None):
        """Local outdoor semester target (never sent to the server)."""
        from ..application import semester_goal as SG
        data = SG.load(self.store_dir)
        target = data.get('target_km')
        stored_sid = data.get('sid')
        if (target is not None and sid not in (None, '')
                and stored_sid not in (None, '')
                and str(stored_sid) != str(sid)):
            target = None            # saved for another semester
        return _ok({'target_km': target, 'sid': stored_sid,
                    'set': target is not None})

    def save_semester_goal(self, target_km=None, sid=None):
        from ..application import semester_goal as SG
        try:
            data = SG.save(self.store_dir, target_km, sid=sid)
        except ValueError as e:
            return _fail('学期目标格式有误: %s' % e)
        return _ok({'target_km': data.get('target_km'),
                    'sid': data.get('sid'),
                    'set': data.get('target_km') is not None})

    def save_school_rules(self, rules=None, **kw):
        from ..application import qualification as Q
        payload = dict(rules or {})
        payload.update(kw)
        try:
            clean = Q.save_rules(payload)
        except ValueError as e:
            return _fail('学校要求格式有误: %s' % e)
        return _ok({'rules': clean, 'configured': Q.is_configured(clean)})

    def real_plan(self, days=14, seed=None, km_min=2.0, km_max=3.0):
        """Future run schedule, offline.  Same generator as `--plan`.

        The caller decides the horizon (the UI stops at the semester end);
        this only rejects nonsense such as 0 or 5000 days.
        """
        from ..application import variation as V
        import random as _r
        try:
            days = max(1, min(int(days), 60))
        except (TypeError, ValueError):
            days = 14
        rng = _r.Random(int(seed)) if seed is not None else None
        slots = V.plan(int(days), rng=rng,
                       dist_band=(float(km_min), float(km_max)))
        return _ok({'days': int(days), 'slots': slots,
                    'text': V.render_plan(slots, compact=True)})

    def real_fetch_fence(self):
        """Campus geofence only (no checkpoint call), for drawing the boundary."""
        acct = self._real_account()
        if acct is None:
            return _fail('未配置 token（先在"账号"面板保存）')
        from ..transport import realclient as R
        from ..application import points as P
        try:
            rc = R.RealClient()
            f = P.fetch_fence(rc, acct)
        except Exception as e:  # noqa: BLE001
            return _fail('围栏获取失败: %s' % e)
        return _ok({
            'name': f.get('name') or '',
            'polygon': [{'lat': a, 'lon': b} for a, b in f['polygon']],
            'center': {'lat': f['center'][0], 'lon': f['center'][1]},
        })

    def real_recent_records(self, limit=20):
        """Last N records (read-only)."""
        acct = self._real_account()
        if acct is None:
            return _fail('未配置 token（先在"账号"面板保存）')
        from ..transport import realclient as R
        from ..application import readonly as RO
        try:
            rc = R.RealClient()
            r = RO.recent_records(rc, acct, limit=limit)
        except Exception as e:  # noqa: BLE001
            return _fail('记录查询失败: %s' % e)
        if not r.get('ok'):
            return _fail(r.get('error') or '记录查询失败')
        return _ok(r)

    def real_daily_count(self, days_ago=0, date=None):
        """How many records already exist on the selected Beijing date.

        Backfill means "today's count" is not enough: the school cap applies to
        the date the new run will be stamped with.  Prefer the server list (it
        sees other devices and the official app), fall back to local receipts,
        and say ``unknown`` instead of inventing 0 when neither is readable.
        """
        acct = self._real_account()
        if acct is None:
            return _fail('未配置 token（先在"账号"面板保存）')
        from ..transport import realclient as R
        from ..application import readonly as RO
        from ..domain import safety as S
        from ..application import limits as L
        try:
            day_obj = S.resolve_backfill_date(date, days_ago)
        except ValueError as e:
            return _fail('日期无效: %s' % e)
        noon = _dt.datetime.combine(
            day_obj, _dt.time(12, 0), tzinfo=S.BEIJING)
        day = noon.strftime('%Y-%m-%d')
        day_ms = int(noon.timestamp() * 1000)
        try:
            rc = R.RealClient()
            runs, source = RO.daily_count(rc, acct, day)
        except Exception:  # noqa: BLE001
            runs, source = None, 'unknown'
        try:
            local = int(L.load(acct.uid, day_ms).get('runs', 0))
        except Exception:  # noqa: BLE001
            local = 0
        if runs is None:
            # Do not turn "the server list is unreadable" into "0 runs".
            # Keep the honest unknown state; the local counter is still used
            # by the submit gate and is exposed separately for diagnostics.
            return _ok({'date': day, 'runs': None, 'source': 'unknown',
                        'local_runs': local})
        runs = max(runs, local)
        return _ok({'date': day, 'runs': runs, 'source': source,
                    'local_runs': local})

    def real_daily_counts(self, dates=None):
        """Counts for several dates at once (date picker badges)."""
        acct = self._real_account()
        if acct is None:
            return _fail('未配置 token（先在"账号"面板保存）')
        from ..transport import realclient as R
        from ..application import readonly as RO
        from ..domain import safety as S
        from ..application import limits as L
        today = S.to_beijing(S.now_ms()).date()
        if dates is None:
            wanted = [(today - _dt.timedelta(days=i)).isoformat()
                      for i in range(0, S.BACKFILL_DAYS + 1)]
        else:
            if not isinstance(dates, list):
                return _fail('dates 必须是数组')
            wanted = []
            for raw in dates[:8]:
                try:
                    d = _dt.date.fromisoformat(str(raw))
                except (TypeError, ValueError):
                    continue
                if 0 <= (today - d).days <= S.BACKFILL_DAYS:
                    wanted.append(d.isoformat())
            wanted = sorted(set(wanted))
        if not wanted:
            return _fail('没有可选日期')
        try:
            rc = R.RealClient()
            got = RO.daily_counts(rc, acct, wanted)
        except Exception as e:  # noqa: BLE001
            return _fail('每日次数查询失败: %s' % e)
        rows = []
        for day in wanted:
            noon = _dt.datetime.combine(
                _dt.date.fromisoformat(day), _dt.time(12, 0),
                tzinfo=S.BEIJING)
            day_ms = int(noon.timestamp() * 1000)
            try:
                local = int(L.load(acct.uid, day_ms).get('runs', 0))
            except Exception:  # noqa: BLE001
                local = 0
            server = (got.get('counts') or {}).get(day)
            if server is None:
                runs, source = None, 'unknown'
            else:
                runs, source = max(server, local), got.get('source') or 'server'
            rows.append({'date': day, 'runs': runs, 'source': source,
                         'local_runs': local})
        return _ok({'dates': rows, 'source': got.get('source'),
                    'error': got.get('error')})

    def _receipt_store(self):
        from ..application.guard import ReceiptStore
        return ReceiptStore(os.path.join(self.store_dir,
                                         'submission_receipts'))

    def real_receipts(self, limit=50):
        """List the write-ahead submission receipts for this account.

        Receipts are the only durable record of an in-flight or unknown
        submission.  They contain no token/password/coordinates.
        """
        acct = self._real_account()
        if acct is None:
            return _fail('未配置 token（先在"账号"面板保存）')
        from ..domain import safety as S
        try:
            limit = max(1, min(int(limit), 200))
        except (TypeError, ValueError):
            limit = 50
        try:
            store = self._receipt_store()
            rows = store.list(acct.uid, school=str(acct.unid))
        except Exception as e:  # noqa: BLE001
            return _fail('回执读取失败: %s' % e)
        out = []
        for r in rows:
            start = r.get('start')
            stop = r.get('stop')
            dist = r.get('distance_m') or 0
            out.append({
                'uuid': r.get('uuid') or '',
                'state': r.get('state') or 'unknown',
                'start_ms': start,
                'start_text': S.fmt_beijing(start) if start else '',
                'stop_text': S.fmt_beijing(stop) if stop else '',
                'distance_m': dist,
                'distance_km': round(float(dist) / 1000.0, 3) if dist else 0,
                'rrid': r.get('rrid'),
                'created': r.get('created'),
                'updated': r.get('updated'),
                'note': r.get('note') or '',
            })
        out.sort(key=lambda x: (x.get('created') or 0), reverse=True)
        out = out[:limit]
        return _ok({
            'receipts': out,
            'total': len(rows),
            'pending': sum(1 for r in rows
                           if r.get('state') in ('pending', 'unknown')),
        })

    def real_reconcile_receipts(self, uuid=None, limit=80):
        """Read-only reconciliation: match unknown receipts to server records.

        This never re-submits anything.  A receipt is marked ``accepted`` only
        when a record with the same start window (and close distance) is
        found; otherwise it stays ``unknown`` so the operator can decide.
        """
        acct = self._real_account()
        if acct is None:
            return _fail('未配置 token（先在"账号"面板保存）')
        from ..transport import realclient as R
        from ..application import readonly as RO
        from ..domain import safety as S
        try:
            store = self._receipt_store()
            rows = store.list(acct.uid, school=str(acct.unid))
        except Exception as e:  # noqa: BLE001
            return _fail('回执读取失败: %s' % e)
        todo = [r for r in rows
                if r.get('state') in ('pending', 'unknown')
                and (not uuid or r.get('uuid') == uuid)]
        if not todo:
            return _ok({'checked': 0, 'matched': [], 'unmatched': [],
                        'message': '没有待核对的回执'})
        try:
            limit = max(20, min(int(limit), 200))
        except (TypeError, ValueError):
            limit = 80
        try:
            rc = R.RealClient()
            listing = RO.recent_records(rc, acct, limit=limit)
        except Exception as e:  # noqa: BLE001
            return _fail('记录查询失败: %s' % e)
        if not listing.get('ok'):
            return _fail(listing.get('error') or '记录查询失败')
        records = listing.get('records') or []

        matched, unmatched = [], []
        for receipt in todo:
            rec = _receipt_match(receipt, records)
            if rec is not None:
                try:
                    store.mark(acct.uid, receipt['uuid'], 'accepted',
                               rrid=rec.get('rrid'), school=str(acct.unid),
                               note='read-only reconcile')
                except Exception as e:  # noqa: BLE001
                    unmatched.append({'uuid': receipt.get('uuid'),
                                      'reason': '回执写回失败: %s' % e})
                    continue
                matched.append({
                    'uuid': receipt.get('uuid'),
                    'rrid': rec.get('rrid'),
                    'start_text': S.fmt_beijing(rec.get('start_ms'))
                    if rec.get('start_ms') else receipt.get('start_text'),
                    'distance_km': rec.get('distance_km'),
                })
            else:
                if receipt.get('state') == 'pending':
                    try:
                        store.mark(acct.uid, receipt['uuid'], 'unknown',
                                   school=str(acct.unid),
                                   note='read-only reconcile: not found')
                    except Exception:  # noqa: BLE001
                        pass
                unmatched.append({
                    'uuid': receipt.get('uuid'),
                    'state': 'unknown' if receipt.get('state') == 'pending'
                             else receipt.get('state'),
                    'start_text': S.fmt_beijing(receipt.get('start'))
                    if receipt.get('start') else '',
                    'distance_km': round(float(receipt.get('distance_m') or 0)
                                         / 1000.0, 3),
                    'reason': '最近记录里没有匹配项（不回写、不重试）',
                })
        return _ok({'checked': len(todo), 'matched': matched,
                    'unmatched': unmatched, 'records': len(records)})

    def real_record_detail(self, rrid, uuid=None):
        """One record in full: track points, cadence, altitude, segments."""
        acct = self._real_account()
        if acct is None:
            return _fail('未配置 token（先在"账号"面板保存）')
        from ..transport import realclient as R
        from ..application import readonly as RO
        try:
            rc = R.RealClient()
            r = RO.record_detail(rc, acct, rrid, uuid=uuid)
        except Exception as e:  # noqa: BLE001
            return _fail('详情查询失败: %s' % e)
        if not r.get('ok'):
            return _fail(r.get('error') or '详情查询失败')
        return _ok(r)

    def real_weekly_summary(self, weeks=4):
        """Per-week totals for the last N weeks (derived from the record list)."""
        acct = self._real_account()
        if acct is None:
            return _fail('未配置 token（先在"账号"面板保存）')
        from ..transport import realclient as R
        from ..application import readonly as RO
        try:
            rc = R.RealClient()
            r = RO.weekly_summary(rc, acct, weeks=weeks)
        except Exception as e:  # noqa: BLE001
            return _fail('周汇总查询失败: %s' % e)
        if not r.get('ok'):
            return _fail(r.get('error') or '周汇总查询失败')
        return _ok(r)

    def _routes_path(self):
        return os.path.join(self.store_dir, 'routes.json')

    def real_get_routes(self):
        """Route library stored next to the receipts (not in localStorage).

        localStorage is scoped by origin, so switching ports or moving from
        the dev server to the frozen exe used to hide every saved route.  The
        JSON file survives both.
        """
        data = _read_json(self._routes_path(), {})
        rows = data.get('routes') if isinstance(data, dict) else data
        if not isinstance(rows, list):
            rows = []
        out = []
        for r in rows:
            c = _clean_route(r)
            if c is not None:
                out.append(c)
        return _ok({'routes': out[:300]})

    def real_save_routes(self, routes=None):
        """Replace the local route library with a validated list."""
        if not isinstance(routes, list):
            return _fail('routes 必须是数组')
        out = []
        for r in routes[:300]:
            c = _clean_route(r)
            if c is not None:
                out.append(c)
        try:
            _write_json(self._routes_path(),
                        {'version': 1, 'routes': out})
        except Exception as e:  # noqa: BLE001
            return _fail('路线保存失败: %s' % e)
        return _ok({'routes': out, 'count': len(out)})

    def _semester_payloads(self, rc, acct):
        """Fetch the semester goal and that semester's totals.

        Order matters: ``getPersonalSemesterInfo`` is the only payload that
        carries ``sid``, and ``recordssummary/semester`` answers a blank
        record (empty ``sname``, every counter 0) when ``sid`` is missing.
        Querying the summary first is what made the card read 0.0% while the
        account actually had 10.24 km of valid semester distance.
        """
        from ..application import readonly as RO
        out = {'summary': None, 'personal': None, 'errors': []}
        try:
            r = RO.semester_personal(rc, acct, run_mode=1)
        except Exception as e:  # noqa: BLE001
            r = {'ok': False, 'error': str(e)}
        if r.get('ok'):
            out['personal'] = r.get('data')
        else:
            out['errors'].append('personal: %s' % (r.get('error') or 'failed'))
        personal = out['personal']
        sid = personal.get('sid') if isinstance(personal, dict) else None
        try:
            r = RO.semester_summary(rc, acct, sid=sid)
        except Exception as e:  # noqa: BLE001
            r = {'ok': False, 'error': str(e)}
        if r.get('ok'):
            out['summary'] = r.get('data')
        else:
            out['errors'].append('summary: %s' % (r.get('error') or 'failed'))
        return out

    def real_semester(self):
        """Semester summary + personal completion, normalised for the UI.

        Summary and personal are queried separately; one failing must not hide
        the other.  The raw payloads are returned for troubleshooting, but the
        UI only relies on ``view``.
        """
        acct = self._real_account()
        if acct is None:
            return _fail('未配置 token（先在"账号"面板保存）')
        from ..transport import realclient as R
        try:
            rc = R.RealClient()
        except Exception as e:  # noqa: BLE001
            return _fail('sidecar/加密层启动失败: %s' % e)
        payloads = self._semester_payloads(rc, acct)
        summary = payloads['summary']
        personal = payloads['personal']
        errors = payloads['errors']
        if (not _semester_payload_nonempty(summary)
                and not _semester_payload_nonempty(personal)):
            return _fail('学期数据读取失败: %s' % ('; '.join(errors) or '返回为空'),
                         data={'errors': errors})
        view = semester_view(summary, personal)
        from ..application import semester_goal as SG
        goal = SG.load(self.store_dir)
        apply_semester_goal(view, goal.get('target_km'), goal.get('sid'))
        return _ok({'view': view, 'summary_raw': summary,
                    'personal_raw': personal, 'errors': errors})

    def real_profile(self):
        """Profile + semester for the "我的" page.

        ``user/info`` is the authoritative source.  If it fails, the local
        session still supplies name/campus so a network error does not blank
        the page; ``profile.verified`` and ``errors`` say what is missing.
        """
        acct = self._real_account()
        if acct is None:
            return _fail('未配置 token（先在"账号"面板保存）')
        from ..transport import realclient as R

        errors = []
        try:
            checks = self.real_check_session()
        except Exception as e:  # noqa: BLE001
            checks = _fail('user/info 异常: %s' % e)
        session = self._read_session()
        profile = {
            'uid': session.get('uid') or acct.uid,
            'unid': session.get('unid') or acct.unid,
            'name': session.get('name'),
            'nickname': session.get('nickname'),
            'schoolName': session.get('schoolName'),
            'campusName': session.get('campusName'),
            'gradeClass': None,
            'height': None,
            'weight': None,
            'verified': False,
        }
        if checks.get('code') == 200:
            d = checks.get('data') or {}
            if isinstance(d, dict) and d:
                for key in ('uid', 'name', 'nickname', 'schoolName',
                            'campusName', 'gradeClass', 'unid', 'height',
                            'weight'):
                    if d.get(key) not in (None, ''):
                        profile[key] = d[key]
                profile['verified'] = True
            else:
                errors.append('资料: user/info 返回为空')
        else:
            errors.append('资料: %s'
                          % (checks.get('message') or 'user/info 失败'))

        # Some campuses return only "学校 校区" as the campus name.  Only
        # split it when the prefix actually looks like a school; a plain
        # "江门 校区" must not become a fabricated school named "江门".
        campus = str(profile.get('campusName') or '').strip()
        if not profile.get('schoolName') and ' ' in campus:
            head = campus.rsplit(' ', 1)[0].strip()
            if head.endswith(('大学', '学院', '学校')):
                profile['schoolName'] = head

        summary = personal = None
        semester_error = ''
        try:
            rc = R.RealClient()
            payloads = self._semester_payloads(rc, acct)
            summary = payloads['summary']
            personal = payloads['personal']
            semester_error = '; '.join(payloads['errors'])
        except Exception as e:  # noqa: BLE001
            semester_error = str(e)

        semester = None
        if (_semester_payload_nonempty(summary)
                or _semester_payload_nonempty(personal)):
            from ..application import semester_goal as SG
            view = semester_view(summary, personal)
            goal = SG.load(self.store_dir)
            apply_semester_goal(view, goal.get('target_km'), goal.get('sid'))
            semester = {'view': view}
        elif not semester_error:
            semester_error = '学期数据为空'
        if semester_error:
            errors.append('学期: %s' % semester_error)

        return _ok({'profile': profile, 'semester': semester,
                    'semester_error': semester_error, 'errors': errors,
                    'checked_at': int(time.time() * 1000)})

    def real_violations(self, page=1, page_size=20):
        """Read-only violation self-check.

        ``unknown`` / ``failed`` are returned as data, never disguised as
        "clean".  The UI is responsible for showing that distinction, and this
        method never retries, re-submits or writes anything.
        """
        acct = self._real_account()
        if acct is None:
            return _fail('未配置 token（先在"账号"面板保存）')
        from ..transport import realclient as R
        from ..application import readonly as RO
        try:
            rc = R.RealClient()
        except Exception as e:  # noqa: BLE001
            return _fail('sidecar/加密层启动失败: %s' % e)
        try:
            p = max(1, int(page))
        except (TypeError, ValueError):
            p = 1
        try:
            size = max(1, min(50, int(page_size)))
        except (TypeError, ValueError):
            size = 20
        result = RO.check_violations(rc, acct, page=p, page_size=size)
        if 'count' not in result:
            result['count'] = len(result.get('records') or [])
        result['stale'] = RO.is_stale(result)
        result['page'] = p
        result['page_size'] = size
        return _ok(result)

    def real_fetch_school_rules(self, run_mode=1):
        """Read the server's ``runModePolicy.runRuleModel`` as form values.

        This is the same read-only endpoint submission already uses.  Only
        fields with a proven unit are mapped (see ``policy.rules_from_policy``);
        a field the server does not return is left blank instead of guessed.
        """
        acct = self._real_account()
        if acct is None:
            return _fail('未配置 token（先在"账号"面板保存）')
        from ..transport import realclient as R
        from ..application import policy as POL
        try:
            rc = R.RealClient()
        except Exception as e:  # noqa: BLE001
            return _fail('sidecar/加密层启动失败: %s' % e)

        modes = []
        for raw in (run_mode, POL.RUN_MODE_SCORED, 2):
            try:
                mode = int(raw)
            except (TypeError, ValueError):
                continue
            if mode not in modes:
                modes.append(mode)

        errors = []
        # `operateType=1` (semester) is the request that returns runRuleModel
        # on current builds; `operateType=0` (start a run) returns only
        # `policy` and leaves the rule model null.  Try the semester request
        # first, then the start request for older servers.
        for mode in modes:
            for op in (POL.OPERATE_TYPE_SEMESTER, POL.OPERATE_TYPE_RUN):
                try:
                    data = POL.fetch_policy(rc, acct, run_mode=mode,
                                            operate_type=op)
                    mapped = POL.rules_from_policy(data)
                    if mapped.get('rules'):
                        return _ok({
                            'rules': mapped['rules'],
                            'meta': mapped.get('meta') or {},
                            'raw': mapped.get('raw') or {},
                            'mapped': mapped.get('mapped') or [],
                            'run_mode': mode,
                            'operate_type': op,
                            'policy': data.get('policy'),
                            'source': 'runModePolicy',
                            'errors': errors,
                        })
                    if mapped.get('raw'):
                        errors.append(
                            'runMode=%d operateType=%d 有规则模型但没有可映射字段'
                            % (mode, op))
                    else:
                        errors.append(
                            'runMode=%d operateType=%d 未返回 runRuleModel'
                            % (mode, op))
                except Exception as e:  # noqa: BLE001
                    errors.append('runMode=%d operateType=%d: %s'
                                  % (mode, op, e))
        return _fail('服务器没有返回可用的学校规则: %s' % '; '.join(errors),
                     data={'errors': errors})

    def app_version(self):
        """Version + build identifier (shown in 设置 → 关于 and the sidebar)."""
        info = build_info()
        info['label'] = version_string()
        return _ok(info)

    def real_altitude_template(self, text=None):
        """Parse a local GPX/JSON elevation profile for the parameter panel.

        The file is read by the browser and sent to this loopback bridge; it
        is parsed here only and is never written to disk or uploaded.
        """
        from ..domain.rundata import parse_altitude_template, resample_profile
        raw = text if isinstance(text, str) else ''
        if not raw.strip():
            return _fail('模板文件为空')
        if len(raw.encode('utf-8', 'ignore')) > 8 * 1024 * 1024:
            return _fail('模板超过 8 MiB，请先裁剪')
        try:
            values = parse_altitude_template(raw)
            # The UI only needs a compact shape; the full file may have
            # hundreds of thousands of points.
            profile = resample_profile(values, min(len(values), 600))
        except Exception as e:  # noqa: BLE001
            return _fail('模板解析失败: %s' % e)
        return _ok({
            'profile': [round(float(v), 2) for v in profile],
            'points': len(values),
            'min': round(min(values), 1),
            'max': round(max(values), 1),
        })

    def real_diagnostics(self):
        """Redacted support bundle; safe to paste into an issue."""
        from ..diagnostics import collector as D
        from ..domain import safety as S
        try:
            info = D.collect(crypto_backend='netsec',
                             app_version=version_string(False),
                             extra={'generated_by': 'replica_api',
                                    'build': build_info(),
                                    'session_present': bool(self._real_account())})
            return _ok({'info': info, 'text': D.render(info, 'text'),
                        'at': S.fmt_beijing(S.now_ms())})
        except Exception as e:  # noqa: BLE001
            return _fail('诊断包生成失败: %s' % e)

    def real_preview_model(self, km=2.5, pace=6.0, cadence=172,
                           altitude=25.0, waypoints=None, checkpoints=None,
                           days_ago=0, start_time=None,
                           daily_runs=None, daily_source=None, date=None,
                           closed=True, altitude_mode='auto',
                           altitude_range=None, altitude_profile=None):
        """Evaluate the user-entered rules against a local preview run.

        This is offline (no server call).  It lets the UI show a real score
        before submission instead of a hard-coded demo number.  The final
        submission re-evaluates the actual generated track.

        ``date`` (``YYYY-MM-DD``) / ``start_time`` describe the backfill slot
        the operator picked; ``days_ago`` remains as the CLI shorthand.
        ``daily_runs`` is the server count the caller already knows; when it is
        omitted the check stays explicitly ``unknown`` instead of pretending
        this tool's local receipt counter proves anything about the server.
        """
        from ..domain.rundata import generate_run_data
        from ..application import qualification as Q
        from ..domain import safety as S
        try:
            wps = [(float(a), float(b)) for a, b in (waypoints or [])]
            cps = []
            for p in (checkpoints or []):
                if not isinstance(p, dict):
                    continue
                lat = p.get('lat', p.get('glat'))
                lon = p.get('lon', p.get('glon'))
                if lat is None or lon is None:
                    continue
                cps.append({'lat': float(lat), 'lon': float(lon),
                            'name': p.get('name') or p.get('pointName') or '',
                            'isMustPoint': bool(p.get('isMustPoint'))})
            d = self._read_session()
            duration_s = float(km) * float(pace) * 60.0
            try:
                start_ms = S.resolve_start_ms(days_ago, start_time, duration_s,
                                              date=date)
            except ValueError as e:
                return _fail('开始时间无效: %s' % e)
            run = generate_run_data(
                float(km), float(pace), int(cadence),
                center_lat=float(d.get('lat') or 22.4108),
                center_lng=float(d.get('lon') or 112.9409),
                waypoints=wps or None,
                checkpoints=cps or None,
                altitude_base=float(altitude),
                altitude_mode=altitude_mode or 'auto',
                altitude_range=altitude_range or None,
                altitude_profile=altitude_profile or None,
                start_ms=start_ms,
                closed=bool(closed),
                run_uuid=new_run_uuid(
                    self._effective_device(d).get('platform')))
            if daily_runs is None:
                # The caller explicitly says it does not know the server count.
                # Do not silently substitute this tool's local lower bound:
                # 0 local receipts is not proof that today has 0 runs.
                daily_source = 'unknown'
            else:
                try:
                    daily_runs = int(daily_runs)
                except (TypeError, ValueError):
                    daily_runs, daily_source = None, 'unknown'
            model = Q.evaluate(run, Q.load_rules(),
                               checkpoints=cps or None,
                               daily_runs=daily_runs,
                               daily_source=daily_source)
            return _ok({
                'model': model,
                'param_warnings': plausibility_warnings(pace, cadence),
                'start': {'ms': start_ms,
                          'text': S.fmt_beijing(start_ms),
                          'days_ago': (S.to_beijing(S.now_ms()).date()
                                       - S.to_beijing(start_ms).date()).days},
                'daily': {'runs': daily_runs, 'source': daily_source,
                          'date': S.to_beijing(start_ms).strftime('%Y-%m-%d')},
                'run': {'km': run.get('km'), 'sportTime': run.get('sportTime'),
                        'juli': run.get('juli'),
                        'points': len(run.get('allLocJson') or [])},
            })
        except Exception as e:  # noqa: BLE001
            return _fail('模型预览失败: %s' % e)

    def real_snap_route(self, points=None, key=None, max_legs=24):
        """Snap a hand-drawn polyline to real walking paths (AMap).

        Each leg between two drawn vertices becomes a walking route; the result
        follows streets instead of cutting across buildings.  Slow by design
        (one request per leg, rate-limited), so the UI shows progress.
        """
        from ..transport import amap
        pts = [(float(a), float(b)) for a, b in (points or [])]
        if len(pts) < 2:
            return _fail('至少需要 2 个点')
        # Without a key every leg would silently degrade to a straight line and
        # the caller would think snapping worked.  Say it plainly instead.
        import os as _os
        if not (key or _os.environ.get(amap.KEY_ENV)):
            return _fail('吸附需要高德「Web服务」Key：在控制台创建后填到「高德Key」')
        try:
            res = amap.snap_polyline(pts, key=key,
                                     max_legs=int(max_legs or 24), delay=0.35)
        except Exception as e:  # noqa: BLE001
            return _fail('吸附失败: %s' % e)
        return _ok(res)

    def real_submit_run(self, km=2.5, pace=6.0, cadence=172,
                        waypoints=None, geeToken='', use_points=True,
                        altitude=25.0, snap_route=True,
                        days_ago=0, start_time=None, date=None,
                        closed=True, altitude_mode='auto',
                        altitude_range=None, altitude_profile=None):
        """Forge a run, sign it with the official scheme, submit it for real,
        then read the record back so the caller sees the completion checklist.

        When use_points is true the campus checkpoints are pulled first (300 s
        cache) and the track is routed through them, so the replay draws the
        green ticks instead of an empty map.  policy defaults to 1 (see
        rundata.build_upload_entity): that is the rule set real 学分跑 records
        carry and the only one that reaches 6/6.

        A write-ahead receipt guards against double submission and against
        re-sending when a previous outcome is unknown.
        """
        acct = self._real_account()
        if acct is None:
            return _fail('未配置 token（先在"账号"面板保存）')
        from ..transport import realclient as R
        from ..application import points as P
        from ..domain.rundata import build_upload_entity, sign_upload_entity
        from ..application.guard import ReceiptStore, AlreadySubmitted, UnknownPending, GuardError

        try:
            rc = R.RealClient()
        except Exception as e:  # noqa: BLE001
            return _fail(f'sidecar/加密层启动失败: {e}')

        # ---- 0. policy: supplies the `runes` timestamp ---------------------
        policy_ts = None
        policy_data = None
        try:
            from ..application import policy as POL
            for rm in (1, 2):
                try:
                    policy_data = POL.fetch_policy(rc, acct, run_mode=rm)
                    policy_ts = POL.policy_timestamp(policy_data)
                    break
                except Exception:
                    continue
        except Exception:
            policy_data = None

        # ---- 1. live checkpoints -> route through them --------------------
        # Checkpoints are fetched even when a route was supplied: a record
        # without them can never be complete (this used to be skipped for any
        # hand-drawn or imported route).
        cps = []
        wps = [(float(a), float(b)) for a, b in (waypoints or [])]
        if use_points:
            try:
                cps = P.fetch_points(rc, acct, acct.center_lat, acct.center_lng,
                                     distance_m=float(km) * 1000.0)
                if not wps:
                    wps = P.route_order(cps, acct.center_lat, acct.center_lng)
            except Exception as e:  # noqa: BLE001
                # A record without the mandatory checkpoint hits cannot be
                # complete.  Submitting anyway created an accepted-but-invalid
                # run, so stop before the write-ahead receipt is reserved.
                return _fail('点位加载失败，已停止提交（避免生成不完整记录）: %s'
                             % e)

        # policy's must-points must be on the track even when the checkpoint
        # endpoint does not return them (policy.rs::route_points).  A malformed
        # list aborts, same as the reference: dropping a required point yields
        # a record that can never be complete.
        try:
            from ..application import policy as _POL
            mp = _POL.extract_must_points(policy_data) if policy_data else []
        except Exception as e:  # noqa: BLE001
            return _fail(f'策略必经点无法解析，已中止: {e}')
        if not use_points and mp:
            return _fail('学校策略要求必经点位，不能使用自由跑（请关闭自由跑后重试）')
        if mp:
            first = wps[0] if wps else None
            wps = P.merge_required(mp, wps)
            # The user's first tap is the start.  Policy must-points are
            # prepended by merge_required, so rotate the loop back to it.
            wps = P.rotate_to(wps, first)

        # ---- 1b. a user route must still pass the checkpoints -----------
        from ..domain import routes as RT
        missed = []
        if wps and cps:
            missed = RT.missed_points(wps, cps)
            if missed:
                if snap_route:
                    wps = RT.snap_through(wps, missed)
                    missed = RT.missed_points(wps, cps)
                if missed:
                    names = '、'.join(m['name'] or '(未命名)'
                                      for m in missed[:8])
                    more = '' if len(missed) <= 8 else (' 等 %d 个' % len(missed))
                    return _fail('路线没有经过全部打卡点: %s%s'
                                 % (names, more))

        # ---- 2. forge ----------------------------------------------------
        # Smooth the control points so the submitted track has curves, not
        # straight chords.  The curve passes through every control point, so
        # checkpoint hits are preserved.
        try:
            if len(wps) >= 3:
                wps = RT.smooth(wps, samples=8, closed=bool(closed))
        except Exception:  # noqa: BLE001
            pass
        # The operator may backfill today .. 3 days ago, 09:00-22:00.  Resolve
        # the slot before forging so every timestamp in the record agrees.
        from ..domain import safety as S
        duration_s = float(km) * float(pace) * 60.0
        try:
            start_ms = S.resolve_start_ms(days_ago, start_time, duration_s,
                                          date=date)
        except ValueError as e:
            return _fail(f'开始时间无效: {e}')
        day_key = S.to_beijing(start_ms).strftime('%Y-%m-%d')
        start_days_ago = (S.to_beijing(S.now_ms()).date()
                          - S.to_beijing(start_ms).date()).days

        # Keep the night guard, but do not turn the school's daily cap into a
        # hard block here.  The cap is a *reminder* in the model; the operator
        # may still have a legitimate reason to submit (and the server decides).
        from ..application import limits as L
        refusal = L.check(acct.uid, start_ms, max_per_day=0,
                          block_night=True)
        if refusal:
            return _fail(refusal, data={'start': {
                'ms': start_ms, 'text': S.fmt_beijing(start_ms),
                'days_ago': start_days_ago}})

        try:
            run = generate_run_data(float(km), float(pace), int(cadence),
                                    center_lat=acct.center_lat,
                                    center_lng=acct.center_lng,
                                    waypoints=wps or None,
                                    checkpoints=cps or None,
                                    altitude_base=float(altitude),
                                    altitude_mode=altitude_mode or 'auto',
                                    altitude_range=altitude_range or None,
                                    altitude_profile=altitude_profile or None,
                                    start_ms=start_ms,
                                    closed=bool(closed),
                                    run_uuid=new_run_uuid(
                                        getattr(acct, 'platform', None)))
        except Exception as e:  # noqa: BLE001
            return _fail(f'路线生成失败: {e}')

        # Same pre-submit gate as the CLI: geometry vs claimed distance,
        # BD/GCJ pairs, isPass, bucket sums, lap coverage.
        from ..domain.rundata import validate_run_data
        issues = validate_run_data(run)
        if issues:
            return _fail('提交前自检未通过: %s' % issues)

        # User-entered school rules -> qualification model.  If the rules are
        # incomplete the model stays silent; it never guesses a school's rules.
        from ..application import qualification as Q
        from ..application import readonly as RO
        rules = Q.load_rules()
        # The school's daily cap applies to the *run date*.  Prefer the
        # server's record list (it also sees other devices/app runs); the local
        # receipt counter is a lower bound but is not proof that the server
        # list is readable.  If the list fails, keep `unknown` and let the
        # model warn instead of claiming a false 0.
        day_runs, day_source = RO.daily_count(rc, acct, day_key)
        try:
            local_runs = int(L.load(acct.uid, start_ms).get('runs', 0))
        except Exception:  # noqa: BLE001
            local_runs = 0
        if day_runs is None:
            day_source = 'unknown'
        else:
            day_runs = max(day_runs, local_runs)
        model = Q.evaluate(run, rules, checkpoints=cps,
                           daily_runs=day_runs, daily_source=day_source)
        if model.get('configured') and model.get('verdict') == 'fail':
            bad = [c['label'] + ' ' + c['value'] for c in model['checks']
                   if c['status'] == 'fail']
            return _fail('合格模型未通过: %s' % '；'.join(bad),
                         data={'model': model,
                               'param_warnings': plausibility_warnings(pace, cadence)})

        from ..application import policy as POL
        from ..application import riskcheck as RK
        city = RK.device_city(acct.campusName, acct.schoolName) or \
            f'{acct.schoolName} {acct.campusName}'.strip()
        ctx = {
            'uid': acct.uid,
            'selectedUnid': acct.unid,
            'sportType': 1,
            'geeToken': geeToken or '',
            # upstream: address = device identity.city (issue #16 default 大连市)
            'address': city,
            # upstream: policy = the server's rule set, not a hardcoded value
            'policy': POL.policy_value(policy_data, 1),
        }
        min_d = POL.rule_min_distance(policy_data)
        if min_d > 0:
            ctx['selDistance'] = min_d
        ent = sign_upload_entity(build_upload_entity(run, ctx))

        # ---- 3. write-ahead receipt --------------------------------------
        store = ReceiptStore(os.path.join(self.store_dir,
                                          'submission_receipts'))
        try:
            store.reserve(acct.uid, ent['uuid'], ent['startTime'], ent['stopTime'],
                          school=str(acct.unid), distance_m=run['juli'])
        except (AlreadySubmitted, UnknownPending) as e:
            return _fail(f'提交被回执拦下: {e}')
        except GuardError as e:
            return _fail(f'回执写入失败，已停止提交: {e}')

        # ---- 4. submit ----------------------------------------------------
        try:
            res = rc.submit_run(acct, run, ent, ent, policy_ts=policy_ts)
        except Exception as e:  # noqa: BLE001
            store.mark(acct.uid, ent['uuid'], 'unknown',
                       school=str(acct.unid), note=str(e)[:120])
            return _fail(f'提交异常，结果未知（已锁定该时段）: {e}')

        res['km'] = run['km']
        res['sportTime'] = run['sportTime']
        res['juli'] = run['juli']
        # `save/record` does not always echo the uuid back; the signed entity
        # is the authoritative copy.  Without this fallback OBS upload and the
        # read-back both received None and the record was left without a trail.
        run_uuid = res.get('uuid') or ent.get('uuid') or run.get('sportunid') or ''
        res['uuid'] = run_uuid
        res['policy'] = ent.get('policy')
        res['model'] = model
        res['start'] = {'ms': start_ms, 'text': S.fmt_beijing(start_ms),
                        'days_ago': start_days_ago}
        res['daily'] = {'date': day_key, 'runs': day_runs,
                        'source': day_source}
        res['checkpoints'] = len(cps)
        res['waypoints'] = len(wps)
        res['missed_points'] = missed

        if res.get('error') != 10000 or not res.get('rrid'):
            # Distinguish "explicitly refused" (retry allowed) from "we do not
            # know" (must stay locked) — the old code labelled both as 拒绝.
            if res.get('error') in (None, 0):
                state = 'unknown'
                head = '提交结果未知（已锁定该时段，请勿重试）'
            elif res.get('error') == 10000:
                state = 'unknown'
                head = '服务器已受理但未返回记录号，结果未知（已锁定该时段）'
            else:
                state = 'rejected'
                head = '提交被拒'
            store.mark(acct.uid, ent['uuid'], state,
                       school=str(acct.unid), note=str(res.get('message'))[:120])
            return _fail('%s: error=%s %s (HTTP %s)'
                         % (head, res.get('error'), res.get('message'),
                            res.get('status')), data=res)

        try:
            store.mark(acct.uid, ent['uuid'], 'accepted',
                       rrid=res['rrid'], school=str(acct.unid))
        except Exception as e:  # noqa: BLE001
            # The record exists; do not turn a receipt-file failure into a
            # failed submission.  Surface it so the operator can reconcile.
            res['receipt_error'] = str(e)
        try:
            L.record(acct.uid, start_ms)
        except Exception:  # noqa: BLE001
            pass

        # ---- 4b. OBS trail upload ----------------------------------------
        # The replay/detail page reads the trail from OBS, not from the record
        # column. Without this the run exists but shows no track and no
        # checkpoints (upstream issue #85).
        try:
            from ..transport import obs as OBS
            time.sleep(1.0)          # reference: flow.rs sleeps 1 s here
            fpj = json.dumps(run['fixed_point_json'], separators=(',', ':'))
            obs_res = OBS.upload_obs(rc, acct, run, res['rrid'], res['uuid'],
                                     fpj, log=lambda *_: None)
            res['obs'] = obs_res
            time.sleep(2.0)          # reference: flow.rs sleeps 2 s before verify
        except Exception as e:  # noqa: BLE001
            res['obs_error'] = str(e)

        # ---- 5. read back -------------------------------------------------
        try:
            back = rc.request(acct, 'api/v70260/runnings/get_one_record', {
                'uuid': res['uuid'], 'rrid': res['rrid'],
                'calculateBadge': False})
            if back.get('error'):
                res['readback_error'] = '解密失败: %s' % back.get('error')
            else:
                obj = as_dict(back.get('json'))
                if obj.get('error') not in (None, 10000):
                    res['readback_error'] = ('error=%s %s'
                                             % (obj.get('error'),
                                                obj.get('message') or ''))
                else:
                    d = as_dict(obj.get('data'))
                    if not d:
                        res['readback_error'] = '回读响应为空'
                    else:
                        res['readback'] = {
                            'isCheat': d.get('isCheat'),
                            'complete': d.get('complete'),
                            'policy': d.get('policy'),
                            'totalDis': d.get('totalDis'),
                            'totalTime': d.get('totalTime'),
                            'speed': d.get('speed'),
                            'calorie': d.get('calorie'),
                            'statusInfo': d.get('statusInfo'),
                            'reasonList': d.get('reasonList'),
                        }
        except Exception as e:  # noqa: BLE001
            res['readback_error'] = str(e)
        res['param_warnings'] = plausibility_warnings(pace, cadence)
        return _ok(res)

    # -- hardware-bound stubs (documented, need real env) ------------------------------
    def _stub(self, name):
        return {'code': 600,
                'message': f'{name} needs real VM/hotspot hardware env',
                'data': None}

    def connectVM(self, a, b, c, d):
        return self._stub('connectVM')

    def disconnectVM(self):
        return _ok(True)

    def downloadVM(self):
        return self._stub('downloadVM')

    def downloadCS7(self):
        return self._stub('downloadCS7')

    def getVMStatus(self):
        return _ok({'running': False})

    def getAppLogo(self):
        return _ok({'logo': ''})

    def close_window(self):
        return _ok(True)

    def minimize_window(self):
        return _ok(True)

    def maximize_window(self):
        return _ok(True)

    def is_window_maximized(self):
        return _ok(False)

    def start_drag(self):
        return _ok(True)

    def startResize(self, _edge):
        return _ok(True)

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
import base64
import json
import math
import os
import random
import secrets
import tempfile
import threading
import time
import uuid
from urllib.parse import urlparse

import requests

from ..application import credentials as CRED
from ..application import account_guard as AG
from ..application import device_profile as DP
from ..application import session_store as SESS
from ..application import shield as SHIELD
from ..build_info import VERSION as APP_VERSION  # noqa: F401 - re-exported
from ..build_info import build_info, version_string
from ..domain import crypto as C
from ..domain.rundata import generate_run_data, new_run_uuid, preview_series
from ..transport.realclient import as_dict

#: Prefer the user's own Web-service key.  Do not ship a hard-coded key in an
#: open-source build; AMap binds keys to a domain/app and a leaked one is a
#: billing and quota risk.
# Default checkpoint-query distance for callers without a target (CLI/tests).
# The UI passes the selected target distance because the endpoint is
# distance-scoped: a fixed 2500 m can be an invalid distance for another school
# and return no points.  Submission still asks for the target-specific set.
CAMPUS_POINTS_DISTANCE_M = 2500
AMAP_KEY = os.environ.get('JHP_AMAP_KEY', '')

#: How long a manual slider challenge stays solvable, and how many may be
#: parked at once.  GeeTest's ticket is short lived too; this only exists so a
#: slow operator does not stare at an image the server already forgot.
GT4_MANUAL_TTL = 180.0
GT4_MANUAL_MAX = 8


def _png_data_url(blob: bytes) -> str:
    """Inline PNG for the manual slider: no extra route, no temp files."""
    return 'data:image/png;base64,' + base64.b64encode(blob).decode('ascii')

APPLE_DB = {
    'models': [row[0] for row in DP.IOS_MODELS],
    'ios': sorted({v for row in DP.IOS_MODELS for v in row[1]}),
}


def _ok(data=None, message='success'):
    return {'code': 200, 'message': message, 'data': data}


def _fail(message='failed', code=500, data=None):
    return {'code': code, 'message': message, 'data': data}


def _fmt_seconds(seconds):
    """Human-readable remaining freeze time."""
    total = max(0, int(seconds or 0))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return '%d 小时 %d 分' % (hours, minutes)
    if minutes:
        return '%d 分 %d 秒' % (minutes, secs)
    return '%d 秒' % secs


def _normalise_ui_points(points) -> list[dict]:
    """Map the UI's rendered checkpoint rows back to the submit shape.

    ``renderPoints`` stores ``{n, lat, lng, must, isFixed, position, seq}``
    (GCJ).  Submit must use exactly that set: the endpoint returns a random
    five-of-N draw per request, so a fresh fetch is a different set from the
    one the operator previewed.  Invalid rows are dropped; the caller then
    falls back to a live fetch.
    """
    out = []
    for p in points or []:
        if not isinstance(p, dict):
            continue
        try:
            lat = float(p['lat'] if p.get('lat') is not None
                        else p.get('glat'))
            lon = float(p['lon'] if p.get('lon') is not None
                        else (p.get('lng') if p.get('lng') is not None
                              else p.get('glon')))
        except (KeyError, TypeError, ValueError):
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue
        out.append({
            'lat': lat,
            'lon': lon,
            'name': p.get('name') or p.get('n') or '',
            'isMustPoint': bool(p.get('isMustPoint') or p.get('must')),
            'isFixed': p.get('isFixed'),
            'position': p.get('position'),
            'seq': p.get('seq'),
        })
    return out


# --------------------------------------------------------------------------
# login cover image: header-only probes, no Pillow in the frozen build
# --------------------------------------------------------------------------
COVER_MAX_BYTES = 8 << 20
COVER_MIN_WIDTH = 800
COVER_MIN_AREA = 800 * 400


def _image_info(raw: bytes):
    """Return (mime, width, height) from the first bytes, or None."""
    if len(raw) < 32:
        return None
    if raw[:8] == b'\x89PNG\r\n\x1a\n':
        try:
            w = int.from_bytes(raw[16:20], 'big')
            h = int.from_bytes(raw[20:24], 'big')
        except (IndexError, ValueError):
            return None
        return ('image/png', w, h)
    if raw[:3] == b'\xff\xd8\xff':
        i = 2
        while i + 9 < len(raw):
            if raw[i] != 0xFF:
                i += 1
                continue
            marker = raw[i + 1]
            if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            if i + 4 > len(raw):
                return None
            seglen = int.from_bytes(raw[i + 2:i + 4], 'big')
            if seglen < 2:
                return None
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                if i + 9 > len(raw):
                    return None
                h = int.from_bytes(raw[i + 5:i + 7], 'big')
                w = int.from_bytes(raw[i + 7:i + 9], 'big')
                return ('image/jpeg', w, h)
            i += 2 + seglen
        return None
    if raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
        kind = raw[12:16]
        if kind == b'VP8X' and len(raw) >= 30:
            w = 1 + int.from_bytes(raw[24:27], 'little')
            h = 1 + int.from_bytes(raw[27:30], 'little')
            return ('image/webp', w, h)
        if kind == b'VP8 ' and len(raw) >= 30:
            w = int.from_bytes(raw[26:28], 'little') & 0x3FFF
            h = int.from_bytes(raw[28:30], 'little') & 0x3FFF
            return ('image/webp', w, h)
        if kind == b'VP8L' and len(raw) >= 25:
            bits = int.from_bytes(raw[21:25], 'little')
            w = (bits & 0x3FFF) + 1
            h = ((bits >> 14) & 0x3FFF) + 1
            return ('image/webp', w, h)
    return None


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
        'draw', 'auto', 'stadium', 'file', 'record', 'learned') else 'draw'
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
        # Server-aligned clock cached across restarts.  The system clock is
        # never touched; only our own timestamps use the offset.
        from ..domain import clock as CLOCK
        self._clock_path = os.path.join(self.store_dir, 'clock.json')
        if CLOCK.source() == 'local':
            try:
                CLOCK.load(self._clock_path)
            except Exception:  # noqa: BLE001 - a bad cache must not block boot
                pass
        self._students: dict[str, dict] = {}
        self._sport: dict[str, dict] = {}
        self._tasks: dict[str, dict] = {}
        # Last locally generated record, for the offline shape audit.  It is
        # never persisted and never sent; it only lets the UI inspect what it
        # just previewed/submitted.
        self._last_run = None
        self._geetokens: list[dict] = []
        self._gt4_manual: dict[str, dict] = {}
        self._gt4_manual_lock = threading.Lock()
        self._keeper = None
        self._keeper_lock = threading.Lock()
        self._rm_lock = threading.RLock()
        self._rm_exec = threading.Lock()
        self._rm_progress = {
            'running': False,
            'phase': 'idle',
            'done': 0,
            'total': 0,
            'error': '',
            'owner': '',
            'started_at_ms': 0,
            'finished_at_ms': 0,
        }
        self._rm_thread = None
        self._rm_cache_lock = threading.RLock()
        self._rm_suggest_cache = None
        self._rm_apply_cache = None
        self._rm_fence_cache = None
        self._agent_lock = threading.RLock()
        self._agent_session = None
        self._agent_choice_cache_obj = None
        self._device_lock = threading.RLock()
        self._device_cache = None
        self._device_migration = None
        # One-time migration: 0.1.6 and earlier wrote the token in clear text.
        try:
            SESS.migrate(self._session_path())
        except Exception:  # noqa: BLE001 - a bad file must not block startup
            pass
        # Repair the shared/old device fingerprint before any request can use
        # it.  A migrated profile must never reuse the old token: the server
        # would see one token switch phones mid-session.  Clearing the local
        # identity here forces the normal login flow to establish a token for
        # the new device.
        try:
            session = self._read_session()
            profile, migration = DP.load_or_create_ex(
                self.store_dir, legacy_session=session)
            self._device_cache = profile
            self._device_migration = (migration
                                      or DP.read_migration_marker(
                                          self.store_dir))
            if (self._device_migration
                    and (session.get('token') or session.get('uid'))):
                self._stop_keeper()
                self._write_session(self._clear_session_identity(session))
        except Exception:  # noqa: BLE001 - corrupt profiles surface on read
            pass
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
        h = {'appVersion': '7.4.0'}
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
        """Forward geocode.  Never invent a coordinate.

        The old stub returned a fixed Xiamen point, which silently moved any
        caller that used it as an anchor.  Without an AMap key the honest
        answer is an error, not a plausible-looking wrong place.
        """
        from ..transport import amap
        key = os.environ.get('JHP_AMAP_KEY') or AMAP_KEY
        if not key:
            return _fail('未配置高德 key，无法解析地点坐标')
        try:
            lng, lat = amap.geocode(str(keyword or ''), key=key)
        except Exception as e:  # noqa: BLE001
            return _fail('地点解析失败: %s' % e)
        return _ok({'lat': lat, 'lng': lng,
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

    def build_run_data(self, distance_km=2.5, pace=6.0, cadence=150,
                       lat=None, lng=None, seed=None, km_times=None):
        import random as _random
        if lat is None or lng is None:
            d = self._read_session()
            lat = float(d.get('lat') or 0)
            lng = float(d.get('lon') or 0)
        if not self._center_ok(lat, lng):
            raise ValueError('校区坐标未解析，不能生成跑步数据')
        rng = _random.Random(seed) if seed is not None else None
        return generate_run_data(distance_km, pace, cadence, lat, lng, rng=rng,
                                 km_times=km_times)

    # -- replica extras (not in the 71; needed to drive tasks end-to-end) --
    def run_task_submit(self, sid, km=2.5, pace=6.0, geetoken='',
                        cadence=150):
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
        return SESS.read(self._session_path())

    def _write_session(self, d: dict) -> None:
        """Atomic replace; the token is DPAPI-sealed, never plaintext."""
        SESS.write(self._session_path(), d)

    def _device_profile(self, session: dict | None = None) -> dict:
        """The installation profile, loaded once per process.

        The cache is deliberately not refreshed on every request: a profile
        change must be an explicit rotate + restart, not a quiet side effect.
        """
        with self._device_lock:
            if self._device_cache is None:
                if session is None:
                    session = self._read_session()
                self._device_cache, migration = DP.load_or_create_ex(
                    self.store_dir, legacy_session=session)
                if migration:
                    self._device_migration = migration
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
               if k not in ('token', 'token_enc') + raw_device}
        out['token_present'] = bool(d.get('token'))
        out['token_masked'] = self._mask_secret(d.get('token'))
        # The map watermark needs a stable account label.  Older sessions were
        # written before `username` was persisted; the DPAPI login store has
        # the same phone and is only read here, never returned with a password.
        if out['token_present'] and not out.get('username'):
            try:
                cred = CRED.load(self.store_dir)
                if isinstance(cred, dict) and cred.get('account'):
                    out['username'] = str(cred['account'])
            except Exception:  # noqa: BLE001 - a broken store must not hide the session
                pass
        try:
            profile = self._device_profile(d)
            out.update(DP.public_view(profile, d))
            migration = self._device_migration or DP.read_migration_marker(
                self.store_dir)
            if migration:
                out['device_migration_notice'] = migration.get('message') or ''
                out['device_migration'] = {
                    'kind': migration.get('kind'),
                    'from': migration.get('from'),
                    'backup': migration.get('backup'),
                    'old_device_masked': migration.get('old_device_masked'),
                }
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
                         platform=None, idfa=None, username=None):
        if token and username:
            blocked = self._account_guard_block(username)
            if blocked:
                return blocked
        if token:
            # A new session must not inherit the previous account's keeper
            # thread or in-memory password.
            self._stop_keeper()
        d = self._read_session()
        identity_changed = any(
            v not in (None, '') and str(d.get(k)) != str(v)
            for k, v in (('DeviceId', DeviceId), ('platform', platform),
                         ('deviceName', deviceName), ('osVersion', osVersion),
                         ('cpuModel', cpuModel), ('physicPixel', physicPixel),
                         ('logicPixel', logicPixel), ('isRoot', isRoot)))
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
                     ('nickname', nickname), ('username', username),
                     ('deviceName', deviceName), ('osVersion', osVersion),
                     ('cpuModel', cpuModel), ('physicPixel', physicPixel),
                     ('logicPixel', logicPixel), ('isRoot', isRoot),
                     ('platform', platform), ('idfa', idfa),
                     ('appInstallTime', appInstallTime),
                     ('appUpdateTime', appUpdateTime)):
            if v not in (None, ''):
                d[k] = v
        if not token and identity_changed and d.get('token'):
            # A device/platform change is a new phone.  Reusing the old token
            # would make the server see one token switch devices mid-session.
            self._stop_keeper()
            d = self._clear_session_identity(d)
            self._write_session(d)
            self._invalidate_device()
            out = self._public_session(d)
            out['relogin_required'] = True
            out['message'] = '设备身份已变更，请重新登录'
            return _ok(out)
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
        self._device_migration = None
        return _ok({
            'device': DP.public_view(new),
            'backup': os.path.basename(backup) if backup else '',
            'relogin_required': True,
        })

    def real_get_device_options(self):
        """Supported model/OS pairs for the custom-device form."""
        try:
            catalog = DP.device_catalog()
            current = DP.public_view(self._effective_device())
        except Exception as e:  # noqa: BLE001
            return _fail(f'设备信息不可用: {e}')
        return _ok({'catalog': catalog, 'current': current})

    def real_set_custom_device(self, confirm='', platform='', deviceName='',
                               osVersion='', DeviceId='', installDays='',
                               physicPixel='', logicPixel='', cpuModel=''):
        """Install an operator-defined device profile.

        Model, OS, both resolutions and CPU are free-form; the backend checks
        platform rules (aspect ratio, iOS 2x/3x, CPU family) instead of
        requiring a catalog model.  ``installDays`` stays optional.

        The local session is ended before the new profile is installed, so an
        old token is never reused with a different device.
        """
        if confirm != '自定义设备':
            return _fail('确认文字不正确')
        try:
            candidate = DP.build_custom_profile(
                platform=platform, device_name=deviceName,
                os_version=osVersion, device_id=DeviceId,
                install_days=installDays, physic_pixel=physicPixel,
                logic_pixel=logicPixel, cpu_model=cpuModel)
        except Exception as e:  # noqa: BLE001
            return _fail(str(e))

        session = self._read_session()
        had_identity = bool(session.get('token') or session.get('uid'))
        # Validate first, then end the session: a typo must not log the user
        # out.  real_logout also starts the best-effort server logout.
        self.real_logout(local_only=True)
        try:
            new, backup = DP.set_custom(self.store_dir, candidate)
        except Exception as e:  # noqa: BLE001
            return _fail(f'保存自定义设备失败: {e}')
        self._invalidate_device()
        self._device_migration = None
        return _ok({
            'device': DP.public_view(new),
            'backup': os.path.basename(backup) if backup else '',
            'relogin_required': had_identity,
        })

    @staticmethod
    def _center_ok(lat, lng) -> bool:
        """A usable coordinate pair: in range and not the 0/0 sentinel."""
        try:
            lat, lng = float(lat), float(lng)
        except (TypeError, ValueError):
            return False
        return (abs(lat) <= 90.0 and abs(lng) <= 180.0
                and (abs(lat) > 1e-6 or abs(lng) > 1e-6))

    def _save_center(self, lat, lng) -> None:
        """Persist the resolved campus centre on the session.

        Kept in the same atomic file as the token so the next launch does not
        have to guess (and never inherits another account's campus).
        """
        d = self._read_session()
        d['lat'] = round(float(lat), 6)
        d['lon'] = round(float(lng), 6)
        self._write_session(d)

    def real_set_campus_center(self, lat=None, lon=None):
        """Operator-set campus centre for schools with no geofence.

        The desktop tool has no GPS.  When the server returns no fence and no
        usable centre, the operator pans the map to their campus and stores
        that point.  It is only a coordinate; no network call is made here.
        """
        try:
            lat = float(lat)
            lon = float(lon)
        except (TypeError, ValueError):
            return _fail('校区坐标无效')
        if not self._center_ok(lat, lon):
            return _fail('校区坐标无效')
        self._save_center(lat, lon)
        return _ok({'lat': round(lat, 6), 'lon': round(lon, 6)})

    def real_clear_campus_center(self):
        d = self._read_session()
        d.pop('lat', None)
        d.pop('lon', None)
        self._write_session(d)
        return _ok(True)

    def _fetch_run_policy(self, rc, acct):
        """`runModePolicy` for this account (run areas + fence + rules).

        The official UI calls it on entering the run page.  `runMode=1` may be
        rejected outside the school's valid window, so try the free-run mode
        too.  The official `RunPolicyViewModel` caches **one** reply; areas and
        fence always belong together, so never splice the two modes: pick the
        single most complete reply instead.
        """
        from ..application import policy as POL
        replies = []
        for rm in (1, 2):
            try:
                data = POL.fetch_policy(rc, acct, run_mode=rm)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(data, dict) or not data:
                continue
            replies.append(data)
            # This reply carries both halves; official caches exactly one.
            if 'runAreaModels' in data and 'geoFence' in data:
                break
        if not replies:
            return None
        if len(replies) == 1:
            return replies[0]

        def score(data):
            # A reply that carries both halves is authoritative.  Otherwise
            # prefer the one with the fence (it decides the submit coordinate).
            both = 'runAreaModels' in data and 'geoFence' in data
            return (1 if both else 0,
                    1 if 'geoFence' in data else 0,
                    1 if 'runAreaModels' in data else 0,
                    1 if data.get('policy') is not None else 0,
                    len(data))

        return max(replies, key=score)

    def _ensure_fence(self, acct, client=None, policy_data=None,
                      run_area=None, anchor=None) -> dict:
        """Read this account's policy fence and resolve a campus centre.

        The fence list is authoritative evidence for in-area checks, but it
        does not always identify a single centre: a run area may own several
        polygons and there may be no run area at all.  With one polygon its
        centroid is the anchor; with several, use a saved/manual centre or the
        route's first point only when it is actually inside one of them.
        Never pick the first polygon just because it is first.
        """
        if client is None:
            from ..transport import realclient as R
            client = R.RealClient()
        from ..application import points as P
        if policy_data is None:
            raise P.PolicyUnavailable(
                'runModePolicy 未返回，无法确定围栏；不会用旧围栏兜底')
        fence = P.fetch_fence(client, acct, policy_data=policy_data,
                              run_area=run_area)
        polygons = fence.get('polygons') or (
            [fence['polygon']] if fence.get('polygon') else [])

        if not polygons:
            if self._center_ok(acct.center_lat, acct.center_lng):
                fence['center'] = (acct.center_lat, acct.center_lng)
                fence['center_source'] = 'manual'
                return fence
            # Keep the reason: a failed legacy fallback ("读取失败") must not
            # read like "this school has no fence".
            reason = fence.get('reason') or '学校未配置跑步围栏'
            raise P.FenceNotConfigured('%s，且本地没有校区坐标' % reason)

        from ..domain import routes as RT
        if len(polygons) == 1:
            center = fence.get('center')
            if not center or not self._center_ok(center[0], center[1]):
                raise RuntimeError('围栏中心无效: %r' % (center,))
            lat, lng = float(center[0]), float(center[1])
            acct.center_lat, acct.center_lng = lat, lng
            self._save_center(lat, lng)
            fence['center'] = (lat, lng)
            fence['center_source'] = 'fence'
            return fence

        manual = None
        if self._center_ok(acct.center_lat, acct.center_lng):
            manual = (float(acct.center_lat), float(acct.center_lng))
        if manual and RT.point_in_fences(manual[0], manual[1], polygons):
            fence['center'] = manual
            fence['center_source'] = 'manual'
            return fence

        if anchor and len(anchor) == 2 and RT.point_in_fences(
                float(anchor[0]), float(anchor[1]), polygons):
            lat, lng = float(anchor[0]), float(anchor[1])
            # Transient only: a route's first point must not become the
            # account's campus for later checkpoint queries.
            acct.center_lat, acct.center_lng = lat, lng
            fence['center'] = (lat, lng)
            fence['center_source'] = 'route'
            return fence

        error = P.FenceAmbiguous(
            '检测到多个跑步围栏，无法确定校区中心：请先在跑区下拉框选择跑区，'
            '或把地图拖到学校后点「设为校区」')
        # Keep the polygons on the exception so the preview can still draw
        # them without adopting an out-of-fence map centre.
        error.fence = fence
        raise error

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
        device = self._effective_device(d)
        issues = DP.audit_profile(device)
        if issues:
            raise DP.DeviceProfileError(
                '设备指纹不自洽，已停止请求: %s' % '；'.join(issues))
        DP.apply_to_account(acct, device)
        if self._center_ok(d.get('lat'), d.get('lon')):
            acct.center_lat = float(d['lat'])
            acct.center_lng = float(d['lon'])
        return acct

    def real_check_session(self):
        """Verify the stored token against the real server (envelope-decoded).

        Uses GET user/info — the cheapest endpoint that proves both the token
        AND our crypto stack are live, because its reply is a real NetSecKit
        envelope that we must decrypt with the native w1228 entry.  7.4.0 moved
        it to ``v70400``; the older ``v70100`` is kept as a 404-only fallback
        (see ``replica/transport/routes.py``).
        """
        try:
            acct = self._real_account()
        except DP.DeviceProfileError as e:
            return _fail(str(e))
        if acct is None:
            return _fail('未配置 token（先在"账号"面板保存）')
        from ..transport import realclient as R
        from ..transport.routes import request_user_info
        try:
            rc = R.RealClient()
            res = request_user_info(rc, acct, method='GET', retries=2)
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
        old_unid = session.get('unid')
        changed = False
        for key in ('uid', 'name', 'nickname', 'campusName', 'schoolName',
                    'unid'):
            value = d.get(key)
            if value not in (None, '') and session.get(key) != value:
                session[key] = value
                changed = True
        if str(old_unid or '') != str(session.get('unid') or ''):
            # A campus change invalidates the old centre.  Drop it so the
            # next submit/fetch resolves the new fence instead of reusing the
            # previous campus's coordinate.
            session.pop('lat', None)
            session.pop('lon', None)
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
                    'username', 'phone', 'mobile', 'password', 'pwd', 'avatar',
                    'platform', 'deviceName', 'osVersion', 'cpuModel',
                    'physicPixel', 'logicPixel', 'isRoot', 'idfa',
                    'appInstallTime', 'appUpdateTime'):
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

    def _sync_server_clock(self, acct=None, rc=None, force=False,
                           timeout=8.0, max_age_s=600.0):
        """Measure ``gmttime`` and apply it to our clock.  Never raises.

        The operator's system clock is not modified.  Every generated or
        signed timestamp reads :mod:`replica.domain.clock`, so after this call
        a laptop that is minutes fast still produces valid requests.
        """
        from ..domain import clock as C
        if not force and not C.needs_sync(max_age_s):
            return C.state()
        if acct is None:
            try:
                acct = self._real_account()
            except Exception:  # noqa: BLE001
                acct = None
        if acct is None or not getattr(acct, 'token', ''):
            return C.state()
        try:
            from ..transport import realclient as R
            client = rc or R.RealClient(proxy=os.environ.get('JHP_PROXY')
                                        or None)
            info = client.server_time(acct, timeout=timeout)
            try:
                C.save(self._clock_path)
            except Exception:  # noqa: BLE001
                pass
            return info
        except Exception as e:  # noqa: BLE001 - diagnostics must not block
            st = C.state()
            st['error'] = str(e)[:160]
            return st

    def real_preflight(self, campus_name=None, school_name=None,
                       start_ms=None, expect_city=None):
        """Explicit network/clock preflight (queries the public IP).

        The UI calls this only when the operator presses "网络自检"; it is not
        part of login/submit, so normal use does not leak network location.
        """
        try:
            acct = self._real_account()
        except Exception:  # noqa: BLE001 - a corrupt profile must not block the check
            acct = None
        from ..application import riskcheck as RK
        from ..domain import safety as S
        campus = campus_name if campus_name is not None else (
            getattr(acct, 'campusName', '') if acct is not None else '')
        school = school_name if school_name is not None else (
            getattr(acct, 'schoolName', '') if acct is not None else '')
        # Measure the server clock first: the diagnostic's "start is in the
        # future" check and the displayed time should speak the server's
        # timeline, not this laptop's.
        clock = self._sync_server_clock(acct, force=True, timeout=8.0)
        try:
            ms = int(start_ms) if start_ms is not None else S.now_ms()
        except (TypeError, ValueError):
            ms = S.now_ms()
        # The official client resolves `address` by reverse-geocoding the
        # run's start point (MapLogKt.getCity).  For this diagnostic use the
        # campus centre when known; the name fallback still handles schools
        # whose campus name contains a city.
        expected = expect_city
        if not expected and acct is not None:
            expected = RK.address_for_start(
                getattr(acct, 'center_lat', 0.0),
                getattr(acct, 'center_lng', 0.0),
                campus, school,
                key=os.environ.get('JHP_AMAP_KEY') or AMAP_KEY) or None
        applied = bool(isinstance(clock, dict) and clock.get('applied'))
        try:
            r = RK.preflight(campus, school, start_ms=ms,
                             expect_city=expected,
                             explicit_start=True,
                             clock_offset_ms=(clock.get('offset_ms')
                                              if applied else None),
                             clock_error=(None if applied else
                                          (clock.get('error')
                                           or clock.get('reason')
                                           or '未配置登录')))
        except Exception as e:  # noqa: BLE001
            return _fail('网络自检失败: %s' % e)
        r = dict(r or {})
        r['campus'] = campus
        r['school'] = school
        r['start_text'] = S.fmt_beijing(ms)
        if clock is not None:
            r.setdefault('info', {})['server_time'] = clock
        r['clock_state'] = S.now_ms()
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
        blocked = self._shield_block()
        if blocked:
            return blocked
        blocked = self._account_guard_block(username)
        if blocked:
            return blocked
        try:
            raw_session = self._read_session()
            device = self._effective_device(raw_session)
            issues = DP.audit_profile(device)
            if issues:
                return _fail('设备指纹不自洽，已停止登录: %s'
                             % '；'.join(issues))
            sess = do_login(username, password, log=lambda *_: None,
                            device=device)
        except Exception as e:  # noqa: BLE001
            return _fail(f'登录失败: {e}')
        return self._persist_login(sess, device, raw_session, username)

    def real_login_ticket(self, username, password, lotNumber='',
                          captchaOutput='', passToken='', genTime=''):
        """Finish a login from four GeeTest credentials.

        Kept as a separate entry point for external callers and for the
        two-step debugging flow; the in-app manual slider now does both halves
        in one call (`real_gt4_manual_finish`), because there the challenge is
        already in memory and must be consumed exactly once.
        """
        try:
            from ..application.login_flow import login_with_ticket
        except Exception as e:  # noqa: BLE001
            return _fail(f'登录模块不可用: {e}')
        ticket = {'lotNumber': lotNumber, 'captchaOutput': captchaOutput,
                  'passToken': passToken, 'genTime': genTime}
        if not all(ticket.values()):
            return _fail('手动验证凭证不完整，请重新滑一次')
        blocked = self._shield_block()
        if blocked:
            return blocked
        blocked = self._account_guard_block(username)
        if blocked:
            return blocked
        try:
            raw_session = self._read_session()
            device = self._effective_device(raw_session)
            issues = DP.audit_profile(device)
            if issues:
                return _fail('设备指纹不自洽，已停止登录: %s'
                             % '；'.join(issues))
            sess = login_with_ticket(username, password, ticket,
                                     log=lambda *_: None, device=device)
        except Exception as e:  # noqa: BLE001
            return _fail(f'登录失败: {e}')
        return self._persist_login(sess, device, raw_session, username)

    def real_gt4_config(self):
        """GeeTest constants and the active RSA key's provenance.

        The manual fallback no longer loads `gt4.js` (it does not render in this
        WebView -- see `real_gt4_manual_start`), but these values are what a
        bug report needs to identify the served build, so they stay exposed.
        """
        try:
            from ..transport import gt4
            return _ok({
                'captcha_id': gt4.CAPTCHA_ID_CANDIDATES[0],
                'candidates': list(gt4.CAPTCHA_ID_CANDIDATES),
                'script': 'https://static.geetest.com/v4/gt4.js',
                'rsa_bits': gt4.modulus_bits(),
                'rsa_source': gt4.rsa_source(),
            })
        except Exception as e:  # noqa: BLE001
            return _fail(f'极验配置不可用: {e}')

    def real_gt4_probe(self):
        """Canary: solve one slider and stop before `geevalidate`.

        Passing here plus a later 10003 means the business server scored the
        ticket low; failing here means the solver or GeeTest changed.
        """
        try:
            from ..transport import gt4
            return _ok(gt4.probe(log=lambda *_: None))
        except Exception as e:  # noqa: BLE001
            return _fail(f'极验自检失败: {e}')

    # -- manual slider fallback ------------------------------------------------
    #
    # Why this is not the official `gt4.js` widget any more: on
    # gcaptcha4.js v1.9.8-0d53fd the `product:'embed'` path throws inside
    # slide.init (`TypeError: ... is not a function`) and never calls back,
    # while `product:'float'` does call `onReady` but inserts zero nodes --
    # measured in both the automation Chromium and the app's own WebView2
    # (15 elements total, 0 canvas, 0 img) even though /load, bg and slice all
    # return 200.  A button that opens an empty box is worse than no button.
    #
    # So the fallback runs at the protocol level instead: we fetch the same
    # /load, show the same two images, and let the human do the one thing the
    # solver does -- say where the gap is.  The `w` is then built by the exact
    # same code as the automatic path, which keeps one implementation of the
    # protocol and makes the manual route a true fallback rather than a second
    # client that can rot on its own.

    def _prune_manual(self):
        """Drop expired challenges.  Called under the lock by the callers."""
        now = time.time()
        for key, item in list(self._gt4_manual.items()):
            if now - item['ts'] > GT4_MANUAL_TTL:
                self._gt4_manual.pop(key, None)

    def real_gt4_manual_start(self, captcha_id=None):
        """Fetch one challenge and hand the operator the two images.

        One-shot token, 3-minute TTL: the ticket GeeTest hands back is short
        lived too, and keeping PNGs in RAM indefinitely buys nothing.
        """
        from ..transport import gt4
        trace: dict = {}
        try:
            ch = gt4.load_challenge(captcha_id, log=lambda *_: None,
                                    trace=trace)
        except gt4.GeeError as e:
            trace.update({'ok': False, 'source': 'manual',
                          'error': str(e), 'error_type': type(e).__name__})
            gt4.write_dump(trace)
            return _fail(f'读取极验题目失败：{e}')
        except Exception as e:  # noqa: BLE001
            return _fail(f'读取极验题目失败：{e}')

        # The slice only moves horizontally, but it still has to be drawn on the
        # right row: painting it at `top: 0` puts the piece nowhere near the
        # notch, which is exactly what the first manual build did.  The match
        # already knows the row; this is the only reason the manual path touches
        # the CV at all -- `set_left` itself is the operator's number.
        try:
            gap = gt4.locate_gap(ch['bg'], ch['slice'])
            top = int(gap['y'])
        except Exception as e:  # noqa: BLE001
            return _fail(f'这张图定位不到缺口（{e}），换一张试试')

        token = uuid.uuid4().hex
        with self._gt4_manual_lock:
            self._prune_manual()
            # A stuck dialog must not be able to grow this without bound.
            while len(self._gt4_manual) >= GT4_MANUAL_MAX:
                self._gt4_manual.pop(next(iter(self._gt4_manual)))
            self._gt4_manual[token] = {'challenge': ch, 'trace': trace,
                                       'ts': time.time()}
        max_left = max(0, int(ch['bg_width']) - int(ch['slice_width']))
        return _ok({
            'token': token,
            'captcha_id': ch['captcha_id'],
            'bg': _png_data_url(ch['bg']),
            'slice': _png_data_url(ch['slice']),
            'bg_width': int(ch['bg_width']),
            'bg_height': int(ch['bg_height']),
            'slice_width': int(ch['slice_width']),
            'slice_height': int(ch['slice_height']),
            'slice_top': top,
            'max_left': max_left,
            'expires_in': int(GT4_MANUAL_TTL),
            'static_path': ch['static_path'],
        })

    def real_gt4_manual_finish(self, token, set_left, username='',
                               password=''):
        """Turn the operator's slider position into a ticket, then log in.

        ``set_left`` is the slice's x offset in **image pixels** -- the same
        number the automatic solver gets from `gt4_image.locate_gap`, so the
        payload builder cannot tell the two paths apart.
        """
        username = str(username or '').strip()
        if not username or not password:
            return _fail('请先填好手机号和密码，再完成手动验证')
        blocked = self._shield_block()
        if blocked:
            return blocked
        blocked = self._account_guard_block(username)
        if blocked:
            return blocked
        # Validate before consuming: a malformed position must not cost the
        # operator the puzzle they just solved.
        try:
            distance = int(round(float(set_left)))
        except (TypeError, ValueError):
            return _fail('滑块位置无效，请重新拖动')

        with self._gt4_manual_lock:
            self._prune_manual()
            entry = self._gt4_manual.pop(str(token or ''), None)
        if entry is None:
            return _fail('这次验证已过期，请关掉重新来一次')

        from ..transport import gt4
        ch = entry['challenge']
        trace = entry['trace']
        distance = max(0, min(int(ch['bg_width']) - 1, distance))
        trace.update({'source': 'manual', 'gap': distance})

        try:
            creds = gt4.submit_distance(ch, distance, log=lambda *_: None,
                                        trace=trace, dump=True)
        except gt4.GeeRejected as e:
            # A miss is the normal outcome of a hand-dragged slider.  The
            # evidence still goes to disk, but the operator gets one short
            # sentence instead of a JSON blob.
            trace.update({'ok': False, 'error': str(e),
                          'error_type': type(e).__name__})
            gt4.write_dump(trace)
            return _fail('这次没对上缺口，换一张再来')
        except gt4.GeeError as e:
            trace.update({'ok': False, 'error': str(e),
                          'error_type': type(e).__name__})
            gt4.write_dump(trace)
            return _fail(f'手动验证没有通过：{e}')
        except Exception as e:  # noqa: BLE001
            return _fail(f'手动验证失败：{e}')

        try:
            from ..application.login_flow import login_with_ticket
        except Exception as e:  # noqa: BLE001
            return _fail(f'登录模块不可用: {e}')
        try:
            raw_session = self._read_session()
            device = self._effective_device(raw_session)
            issues = DP.audit_profile(device)
            if issues:
                return _fail('设备指纹不自洽，已停止登录: %s'
                             % '；'.join(issues))
            sess = login_with_ticket(username, password, creds,
                                     log=lambda *_: None, device=device)
        except Exception as e:  # noqa: BLE001
            return _fail(f'登录失败: {e}')
        return self._persist_login(sess, device, raw_session, username)

    def real_gt4_manual_cancel(self, token):
        """Forget a challenge the operator abandoned."""
        with self._gt4_manual_lock:
            self._gt4_manual.pop(str(token or ''), None)
        return _ok(True)

    def _shield_block(self):
        """Require the four-layer device credential before a real login.

        Returns a ``_fail`` payload or None.  A source checkout without the
        native binaries skips this; a frozen build refuses the login and the
        text never names a layer, path, or reason.
        """
        try:
            res = SHIELD.require(self.store_dir)
        except Exception:  # noqa: BLE001 - a bug must not bypass the gate
            return _fail(SHIELD.SHIELD_MESSAGE, code=403,
                         data={'reason': 'shield'})
        if res.get('blocked'):
            return _fail(res.get('message') or SHIELD.SHIELD_MESSAGE,
                         code=403, data={'reason': 'shield'})
        return None

    def _account_guard_block(self, username):
        """Refuse a second account on this device ("一号一设备").

        Returns a ``_fail`` payload or None.  The sidecar is optional in a
        source checkout, so a missing binary falls back to allowing the
        login; a present binary that reports a mismatch/tamper blocks it.
        """
        if not username:
            return None
        try:
            res = AG.check(username, self.store_dir)
        except Exception as e:  # noqa: BLE001 - never trap login on a bug
            return _fail(f'{AG.SUPPORT_MESSAGE}（{e}）', code=403,
                         data={'reason': 'guard_error'})
        if res.get('blocked'):
            return _fail(res.get('message') or AG.MESSAGE, code=403,
                         data={'reason': res.get('reason'),
                               'hint': res.get('hint') or ''})
        return None

    def _persist_login(self, sess, device, raw_session, username=''):
        """Write a successful login and return the sanitised profile."""
        profile = sess.get('profile') or {}
        if username:
            # Enforcement point as well as the pre-login check: two logins
            # racing must not leave the second account with a usable session.
            bound = AG.bind(username, self.store_dir)
            if bound.get('blocked'):
                return _fail(bound.get('message') or AG.MESSAGE, code=403,
                             data={'reason': bound.get('reason'),
                                   'hint': bound.get('hint') or ''})

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
            unid=sess.get('unid'), username=username, **extra)
        # The new token was issued for the repaired profile; the one-shot
        # notice has served its purpose.
        try:
            DP.clear_migration_marker(self.store_dir)
        except Exception:  # noqa: BLE001
            pass
        self._device_migration = None
        # A new token replaces the identity block, including lat/lon.  Keep a
        # manually set campus centre when the same campus logs in again; a
        # school without a fence has no other way to recover it.
        try:
            old_unid = str(raw_session.get('unid') or '')
            new_unid = str(sess.get('unid') or '')
            if old_unid and old_unid == new_unid and self._center_ok(
                    raw_session.get('lat'), raw_session.get('lon')):
                self._save_center(raw_session['lat'], raw_session['lon'])
        except Exception:  # noqa: BLE001
            pass
        # Resolve the campus centre once, right after login.  A failure here
        # must not block login (records are still readable); submit retries and
        # refuses instead of falling back to a built-in coordinate.
        try:
            acct = self._real_account()
            if acct is not None:
                self._ensure_fence(acct)
        except Exception:  # noqa: BLE001
            pass
        data = dict(sess.get('profile') or {})
        data.pop('token', None)
        data['session'] = (saved or {}).get('data')
        return _ok(data)

    def real_fetch_points(self, school='', glat=0.0, glon=0.0, unid=None,
                          force=False, distance_m=CAMPUS_POINTS_DISTANCE_M,
                          run_area_id=None):
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
        # The checkpoint hit radius is a protocol constant
        # (RunSeqLocationManager ticks at `distance <= 25.0`).  The run page
        # checklist used to hard-code 40 m, so it could show "all points hit"
        # for a route the server would score as incomplete.  Send the value
        # with the points so the UI never re-invents it.
        from ..domain.routes import HIT_RADIUS_M
        out = {'unid': acct.unid, 'fence': None, 'fences': [],
               'campuses': None, 'polygon': None, 'center': None,
               'errors': [], 'notes': [],
               'fence_missing': False, 'fence_count': 0,
               'points_missing': False, 'points_empty': False,
               'checkpoints': None, 'points_ok': None,
               'points_distance_m': None, 'points_stale': False,
               'points_source': '', 'points_rate_limited': False,
               'points_retry_after_s': 0,
               'run_area': None, 'run_area_valid': None,
               'run_areas': [], 'distance_m': dist_m,
               'hit_radius_m': HIT_RADIUS_M}

        # 0) The account's run areas come from runModePolicy, exactly like
        # `RunPolicyViewModel.getPolicy()` (selectUnid = this account's unid).
        # The official default is selectedPosition=0; the dialog only changes
        # it when the operator explicitly picks another area.
        from ..application import policy as POL
        policy_data = self._fetch_run_policy(rc, acct)
        area = POL.select_run_area(
            policy_data, 0,
            campus_name=acct.campusName,
            school_name=acct.schoolName) \
            if policy_data else None
        if run_area_id not in (None, '', -1, '-1'):
            # Operator picked an area in the run page (official
            # RunningAreaDialog).  Only an area the server currently offers
            # may override the default.
            chosen = POL.find_run_area(policy_data, run_area_id)
            if chosen:
                area = chosen
            else:
                out['notes'].append('所选跑区不可用，已按默认跑区重新匹配')
        out['run_area'] = area
        out['run_area_valid'] = (None if area is None
                                 else area.get('valid') is not False)
        out['run_areas'] = POL.run_area_models(policy_data) \
            if policy_data else []
        if area is not None and area.get('valid') is False:
            out['notes'].append('默认跑区不在有效时间内，请在跑区下拉框选择其他跑区')
        freeze = POL.freeze_seconds(policy_data) if policy_data else 0
        face_on, face_why = (POL.face_required(policy_data)
                             if policy_data else (False, ''))
        sequential = bool(policy_data) and POL.policy_value(policy_data, 0) == 1
        out['freeze_seconds'] = freeze
        out['face_required'] = face_on
        out['face_reason'] = face_why
        out['sequential'] = sequential
        if not policy_data:
            out['notes'].append('跑区信息未读取，围栏/点位使用通用匹配')
        if freeze > 0:
            out['notes'].append(
                '账号处于跑步冻结期（剩余 %s）；官方客户端此时不能开始跑步，'
                '本工具只提醒，不阻止提交' % _fmt_seconds(freeze))
        if face_on:
            out['notes'].append(
                '学校启用了人脸抽检（%s）；本工具无法完成人脸校验，提交会被阻止'
                % face_why)

        # 1) geofence polygon + authoritative centre.  A school without a
        # fence is a valid configuration (the official client treats an empty
        # list as "always in area"), so that is a note, not an error.  The
        # caller's glat/glon is the fallback anchor when the fence is absent.
        from ..application import points as P
        anchor = (float(glat), float(glon)) if self._center_ok(glat, glon) \
            else None
        fence_polygons = []
        try:
            fence = self._ensure_fence(acct, rc, policy_data=policy_data,
                                       run_area=area, anchor=anchor)
            polygons = fence.get('polygons') or (
                [fence['polygon']] if fence.get('polygon') else [])
            fence_polygons = polygons
            out['fences'] = [
                {'name': fence.get('name') or '', 'points': [
                    {'glat': a, 'glon': b} for a, b in ring]}
                for ring in polygons
            ]
            out['fence'] = out['fences']
            out['fence_count'] = len(polygons)
            if polygons:
                out['polygon'] = [{'n': i + 1, 'lat': a, 'lon': b}
                                  for i, (a, b) in enumerate(polygons[0])]
            else:
                out['fence_missing'] = True
                out['notes'].append('该校区没有配置跑步围栏，不限制跑步位置')
            out['center'] = {'lat': round(acct.center_lat, 6),
                             'lon': round(acct.center_lng, 6)}
        except P.FenceNotConfigured as e:
            out['fence'] = []
            out['fences'] = []
            out['fence_count'] = 0
            out['fence_missing'] = True
            out['notes'].append('该校区没有配置跑步围栏，但本地没有校区坐标')
            out['errors'].append(f'fence: {e}')
        except P.PolicyUnavailable as e:
            out['errors'].append(f'fence: {e}')
            out['fence_error'] = str(e)
            return _fail(str(e), data=out)
        except P.FenceAmbiguous as e:
            ambiguous = getattr(e, 'fence', None) or {}
            fence_polygons = ambiguous.get('polygons') or []
            out['fences'] = [
                {'name': ambiguous.get('name') or '', 'points': [
                    {'glat': a, 'glon': b} for a, b in ring]}
                for ring in fence_polygons
            ]
            out['fence'] = out['fences']
            out['fence_count'] = len(fence_polygons)
            out['errors'].append(f'fence: {e}')
            out['fence_error'] = str(e)
        except Exception as e:  # noqa: BLE001
            out['errors'].append(f'fence: {e}')
            out['fence_error'] = str(e)
        if not self._center_ok(acct.center_lat, acct.center_lng) \
                and self._center_ok(glat, glon):
            from ..domain import routes as RT
            # Only adopt the caller's map centre when there is no fence, or
            # when it is actually inside one of the fences.  An ambiguous
            # multi-fence school must not persist an out-of-area point.
            if (not fence_polygons
                    or RT.point_in_fences(float(glat), float(glon),
                                          fence_polygons)):
                acct.center_lat, acct.center_lng = float(glat), float(glon)
                self._save_center(glat, glon)
        if self._center_ok(acct.center_lat, acct.center_lng):
            out['center'] = {'lat': round(acct.center_lat, 6),
                             'lon': round(acct.center_lng, 6)}

        if not self._center_ok(acct.center_lat, acct.center_lng):
            return _fail('无法获取校区围栏/坐标：请把地图拖到你的学校，点「设为校区」；'
                         '或先画一条以学校为起点的路线。'
                         + ('（%s）' % '; '.join(out['errors'])
                            if out['errors'] else ''),
                         data=out)

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
            meta = P.fetch_points_meta(
                rc, acct, acct.center_lat, acct.center_lng,
                run_area_id=area['id'] if area else None,
                force=bool(force), distance_m=dist_m, allow_empty=True)
        except P.PointsRateLimited as e:
            out['errors'].append(f'points: {e}')
            out['points_error'] = str(e)
            out['points_ok'] = False
            out['points_rate_limited'] = True
            out['points_retry_after_s'] = int(
                getattr(e, 'retry_after', 0) or 0)
        except Exception as e:  # noqa: BLE001
            out['errors'].append(f'points: {e}')
            out['points_error'] = str(e)
            out['points_ok'] = False
        else:
            pts = meta['points']
            out['checkpoints'] = pts
            out['points_ok'] = True
            out['points_distance_m'] = meta['distance_m']
            out['points_stale'] = bool(meta['stale'])
            out['points_source'] = meta['source']
            out['points_rate_limited'] = bool(meta['rate_limited'])
            out['points_retry_after_s'] = int(meta['retry_after_s'] or 0)
            out['mandatory'] = [p for p in pts if p['isMustPoint']]
            out['route_order'] = P.route_order(
                pts, acct.center_lat, acct.center_lng,
                sequential=sequential)
            if meta['stale']:
                out['notes'].append(
                    '点位接口限流，沿用 %d m 的缓存点位（当前目标 %d m）'
                    % (meta['distance_m'], dist_m))
            if not pts:
                # Preview may display "no points", but it is not proof that
                # the campus allows a free run.  Submit defaults to blocking.
                out['points_empty'] = True
                out['notes'].append(
                    '服务器没有返回打卡点；无法确认该校区是否允许自由跑，'
                    '提交时默认阻止')

        # A missing fence/points list is not a failure once a campus centre is
        # known.  Errors stay in ``errors`` so the UI can show them as notes.
        if out['center']:
            return _ok(out)
        return _fail('; '.join(out['errors']) or '未取到围栏', data=out)

    def preview_route(self, km=2.5, pace=6.0, cadence=150, waypoints=None,
                      closed=True, km_times=None):
        """Local track preview (no network): plan through waypoints."""
        try:
            wp = [(float(a), float(b)) for a, b in (waypoints or [])]
            d = self._read_session()
            lat = float(d.get('lat') or 0)
            lng = float(d.get('lon') or 0)
            if not self._center_ok(lat, lng):
                if not wp:
                    return _fail('尚未解析校区坐标，请先加载围栏/点位')
                lat, lng = wp[0]
            run = generate_run_data(float(km), float(pace), int(cadence),
                                    center_lat=lat, center_lng=lng,
                                    waypoints=wp or None,
                                    closed=bool(closed),
                                    km_times=km_times)
            return _ok({
                'allLocJson': run['allLocJson'],
                'km': run['km'],
                'sportTime': run['sportTime'],
                'juli': run['juli'],
                'splits': run.get('km_splits') or [],
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
        try:
            payload = dict(rules or {})
        except (TypeError, ValueError):
            return _fail('学校要求必须是对象')
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
        from ..application import policy as POL
        try:
            rc = R.RealClient()
            policy_data = self._fetch_run_policy(rc, acct)
            if policy_data is None:
                return _fail('无法读取跑步策略，已停止（不会用旧围栏兜底）')
            area = POL.select_run_area(
                policy_data, 0,
                campus_name=acct.campusName,
                school_name=acct.schoolName)
            f = P.fetch_fence(rc, acct, policy_data=policy_data,
                              run_area=area)
        except Exception as e:  # noqa: BLE001
            return _fail('围栏获取失败: %s' % e)
        polygons = f.get('polygons') or (
            [f['polygon']] if f.get('polygon') else [])
        center = f.get('center')
        return _ok({
            'name': f.get('name') or '',
            'polygon': [{'lat': a, 'lon': b} for a, b in (polygons[0] if polygons else [])],
            'polygons': [[{'lat': a, 'lon': b} for a, b in ring]
                         for ring in polygons],
            'fence_count': len(polygons),
            'center': ({'lat': center[0], 'lon': center[1]}
                       if center else None),
            'missing': not polygons,
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

    def real_audit_run(self, rrid=None, uuid=None, **preview):
        """Audit the last generated run or one existing server record.

        Offline for generated data; read-only for a record.  When the UI passes
        preview parameters, generate with them first (this updates
        ``_last_run``), then audit that exact object.  It never submits.
        """
        from ..application import audit as A
        if preview:
            got = self.real_preview_model(**preview)
            if got.get('code') != 200:
                return got
        if rrid or uuid:
            acct = self._real_account()
            if acct is None:
                return _fail('未配置 token（先在"账号"面板保存）')
            from ..transport import realclient as R
            from ..application import readonly as RO
            try:
                detail = RO.record_detail(R.RealClient(), acct, rrid, uuid=uuid)
            except Exception as e:  # noqa: BLE001
                return _fail('审计详情读取失败: %s' % e)
            if not detail.get('ok'):
                return _fail(detail.get('error') or '审计详情读取失败')
            return _ok(A.audit_detail(detail))
        run = self._last_run
        if not isinstance(run, dict):
            return _fail('还没有生成数据：先预览一次路线，再跑审计')
        from ..application import preferences as P
        try:
            return _ok(A.audit_generated(
                run, key_mode=P.get(self.store_dir, 'obs_key_mode', 'single')))
        except Exception as e:  # noqa: BLE001
            return _fail('形状审计失败: %s' % e)

    def real_explain_error(self, code=None, message='', context=None):
        """Explain a server/transport error without network access."""
        from ..application import errors as E
        try:
            return _ok(E.explain(code, message, context))
        except Exception as e:  # noqa: BLE001
            return _fail('错误解释失败: %s' % e)

    def real_verify_record(self, rrid=None, uuid=None):
        """Read-only post-submit verification; never resubmits."""
        acct = self._real_account()
        if acct is None:
            return _fail('未配置 token（先在"账号"面板保存）')
        from ..transport import realclient as R
        from ..application import verification as V
        try:
            out = V.verify_record(R.RealClient(), acct, rrid, uuid=uuid)
        except Exception as e:  # noqa: BLE001
            return _fail('只读核验失败: %s' % e)
        return _ok(out)

    def real_obs_key_mode(self, mode=None, pause_profile=None,
                          gps_profile=None, terrain_coupling=None):
        """Read or set OBS key mode and the local realism profile."""
        from ..application import preferences as P
        doc = P.load(self.store_dir)
        current_mode = str(doc.get('obs_key_mode') or 'single').lower()
        pause = str(doc.get('pause_profile') or 'none').lower()
        gps = str(doc.get('gps_profile') or 'urban').lower()
        terrain = bool(doc.get('terrain_coupling'))
        if mode is None and pause_profile is None and gps_profile is None \
                and terrain_coupling is None:
            saved = (os.path.isfile(P.path(self.store_dir))
                     and bool(P.load(self.store_dir)))
            return _ok({'mode': current_mode, 'pause_profile': pause,
                        'gps_profile': gps, 'terrain_coupling': terrain,
                        'saved': bool(saved)})
        mode = current_mode if mode is None else str(mode).lower()
        if mode not in ('single', 'legacy'):
            return _fail('OBS key 模式只能是 single 或 legacy')
        if pause_profile is not None:
            pause = str(pause_profile).lower()
            if pause not in ('none', 'auto', 'manual'):
                return _fail('暂停模式只能是 none/auto/manual')
        if gps_profile is not None:
            gps = str(gps_profile).lower()
            if gps not in ('clean', 'urban', 'tunnel'):
                return _fail('GPS 档案只能是 clean/urban/tunnel')
        if terrain_coupling is not None:
            if isinstance(terrain_coupling, str):
                terrain = terrain.strip().lower() in ('1', 'true', 'yes', 'on')
            else:
                terrain = bool(terrain_coupling)
        doc.update({'obs_key_mode': mode, 'pause_profile': pause,
                    'gps_profile': gps, 'terrain_coupling': terrain})
        if not P.save(self.store_dir, doc):
            return _fail('设置写入失败：数据目录不可写')
        return _ok({'mode': mode, 'pause_profile': pause,
                    'gps_profile': gps, 'terrain_coupling': terrain,
                    'saved': True})

    def real_presets(self, action='list', name=None, data=None):
        """Named run presets stored under the data directory."""
        from ..application import presets as P
        try:
            return _ok(P.handle(self.store_dir, action, name=name, data=data))
        except Exception as e:  # noqa: BLE001
            return _fail('预设操作失败: %s' % e)

    def real_stats(self, weeks=12):
        """Local receipts + read-only server records; never writes/submits."""
        acct = self._real_account()
        from ..application import stats as ST
        records, error = [], ''
        if acct is not None:
            try:
                from ..transport import realclient as R
                from ..application import readonly as RO
                got = RO.recent_records(R.RealClient(), acct, limit=200)
                records = got.get('records') or []
                error = got.get('error') or ''
            except Exception as e:  # noqa: BLE001
                error = str(e)
        try:
            out = ST.load_for_account(
                os.path.join(self.store_dir, 'submission_receipts'),
                getattr(acct, 'uid', '') if acct is not None else '',
                getattr(acct, 'unid', '') if acct is not None else '',
                weeks=weeks, records=records, record_error=error)
        except Exception as e:  # noqa: BLE001
            return _fail('统计失败: %s' % e)
        return _ok(out)

    def real_export_fit(self, rrid=None, uuid=None):
        """Build a FIT file from one server record; read-only."""
        acct = self._real_account()
        if acct is None:
            return _fail('未配置 token（先在"账号"面板保存）')
        from ..transport import realclient as R
        from ..application import readonly as RO
        from ..application import export_fit as FIT
        try:
            d = RO.record_detail(R.RealClient(), acct, rrid, uuid=uuid)
            if not d.get('ok'):
                return _fail(d.get('error') or '详情读取失败')
            track = d.get('track') or []
            detail = d.get('detail') or {}
            out = FIT.export(
                track, start_ms=int(detail.get('start_ms') or 0),
                filename='run-%s.fit' % (detail.get('rrid') or rrid or 'record'),
                calories=int(detail.get('calorie') or 0),
                ascent=detail.get('total_ascent'))
        except Exception as e:  # noqa: BLE001
            return _fail('FIT 导出失败: %s' % e)
        return _ok(out)

    def real_diagnostics_zip(self):
        """Redacted support archive; no credentials, coordinates or URLs."""
        from ..application import diagnostics_bundle as B
        from ..application import preferences as P
        from ..application import audit as A
        from ..domain import clock as C
        from ..diagnostics import collector as D
        from .. import build_info as BI
        try:
            audit = (A.audit_generated(
                self._last_run,
                key_mode=P.get(self.store_dir, 'obs_key_mode', 'single'))
                if self._last_run else None)
            receipts = []
            acct = self._real_account()
            if acct is not None:
                try:
                    receipts = self._receipt_store().list(
                        str(acct.uid), str(acct.unid))
                except Exception:  # noqa: BLE001
                    receipts = []
            out = B.build(
                self.store_dir,
                diagnostics_text=D.render(D.collect(
                    app_version=BI.version_string(False)), 'text'),
                audit=audit, settings=P.load(self.store_dir),
                receipts=receipts, clock_state=C.state(),
                app_version=BI.version_string(False))
        except Exception as e:  # noqa: BLE001
            return _fail('诊断包生成失败: %s' % e)
        return _ok(out)

    def real_decoy_activate(self, key=''):
        """Passive honeypot entry -- not used by any page or login path.

        A reverse engineer can find and call this; a normal user never sees
        an entry.  It returns a fake ``pro`` plan for three seconds and then
        reports ``expired``.  No file is written, nothing is logged, and the
        key is not retained.
        """
        from ..application import license_decoy as D
        return _ok(D.activate_license(key))

    def real_decoy_status(self):
        """Status of the passive honeypot; never gates a real feature."""
        from ..application import license_decoy as D
        return _ok({'status': D.license_status()})

    def real_integrity_status(self):
        """Native sidecar hash report for the diagnostic view.

        Informational only: a missing/mismatched manifest is shown, not used
        to block login or submit.  Source checkouts report ``skipped``.
        """
        from ..application import integrity as I
        try:
            return _ok(I.check_native())
        except Exception:  # noqa: BLE001 - diagnostics must never break the UI
            return _ok({'ok': False, 'checked': 0, 'missing': [],
                        'mismatch': [], 'reason': 'manifest'})

    def real_inspection(self, freeze_seconds=0):
        """Read-only account/record/clock inspection; never submits."""
        from ..application import inspection as I
        from ..application import readonly as RO
        from ..application import audit as A
        from ..application import preferences as P
        from ..domain import clock as C
        from ..domain import safety as S
        acct = self._real_account()
        session = self._read_session() if acct is not None else None
        records, records_error = [], ''
        daily = None
        receipts = []
        if acct is not None:
            try:
                from ..transport import realclient as R
                rc = R.RealClient()
                got = RO.recent_records(rc, acct, limit=20)
                records = got.get('records') or []
                records_error = got.get('error') or ''
                day = S.to_beijing(S.now_ms()).strftime('%Y-%m-%d')
                runs, source = RO.daily_count(rc, acct, day)
                daily = {'runs': runs, 'source': source, 'date': day}
            except Exception as e:  # noqa: BLE001
                records_error = str(e)
            try:
                receipts = self._receipt_store().list(
                    str(acct.uid), str(acct.unid))
            except Exception:  # noqa: BLE001
                receipts = []
        try:
            try:
                freeze = max(0, int(freeze_seconds or 0))
            except (TypeError, ValueError):
                freeze = 0
            out = I.run(session=session, freeze_seconds=freeze,
                        daily=daily, records=records,
                        records_error=records_error, clock_state=C.state(),
                        receipts=receipts,
                        audit=A.audit_generated(
                            self._last_run,
                            key_mode=P.get(self.store_dir, 'obs_key_mode',
                                           'single'))
                        if self._last_run else None)
        except Exception as e:  # noqa: BLE001
            return _fail('只读巡检失败: %s' % e)
        return _ok(out)

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
                        freeze = POL.freeze_seconds(data)
                        face_on, face_why = POL.face_required(data)
                        return _ok({
                            'rules': mapped['rules'],
                            'meta': mapped.get('meta') or {},
                            'raw': mapped.get('raw') or {},
                            'mapped': mapped.get('mapped') or [],
                            'run_mode': mode,
                            'operate_type': op,
                            'policy': data.get('policy'),
                            'freeze_seconds': freeze,
                            'face_required': face_on,
                            'face_reason': face_why,
                            'sequential': POL.policy_value(data, 0) == 1,
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

    # -- small UI state ---------------------------------------------------
    def _ui_state_path(self) -> str:
        return os.path.join(self.store_dir, 'ui_state.json')

    def real_get_ui_state(self):
        """Preferences that must survive a random localhost port.

        `localStorage` is scoped to scheme+host+port; app.py picks a fresh free
        port on every launch, so a checklist kept only there reappears on the
        next start.  This file lives with the rest of `data/` and is the source
        of truth; the frontend cache is only a fast path.
        """
        raw = _read_json(self._ui_state_path(), {})
        data = raw if isinstance(raw, dict) else {}
        from ..application import preferences as P
        prefs = data.get('run_area_choice')
        try:
            disclaimer_version = int(data.get('disclaimer_version') or 0)
        except (TypeError, ValueError, OverflowError):
            disclaimer_version = 0
        seen = data.get('announcement_seen')
        seen = seen if isinstance(seen, dict) else {}
        announcement_seen = {}
        for key, value in seen.items():
            if isinstance(value, bool):
                continue
            try:
                version = int(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if version > 0:
                announcement_seen[str(key)] = version
        return _ok({
            'onboard_done': bool(data.get('onboard_done')),
            'post_submit_route_action': str(
                data.get('post_submit_route_action') or 'ask'),
            'onboard_done_at': data.get('onboard_done_at') or '',
            'run_area_choice': prefs if isinstance(prefs, dict) else {},
            'disclaimer_version': disclaimer_version,
            'disclaimer_accepted_at': str(
                data.get('disclaimer_accepted_at') or ''),
            'announcement_seen': announcement_seen,
            'skin': P.normalize_skin(data.get('skin')),
        })

    def real_get_announcements(self):
        """Active notices plus the per-id seen versions.

        The catalog lives in ``application/announcements.py``; the frontend
        only renders what this method returns, so publishing a new notice is a
        data change rather than a UI change.
        """
        from ..application import announcements as A
        state = self.real_get_ui_state()
        seen = (state.get('data') or {}).get('announcement_seen') or {}
        return _ok({'items': A.current(), 'seen': seen})

    def real_set_ui_state(self, onboard_done=None, post_submit_route_action=None,
                          run_area_choice=None, disclaimer_version=None,
                          announcement_seen=None, skin=None):
        path = self._ui_state_path()
        raw = _read_json(path, {})
        data = raw if isinstance(raw, dict) else {}
        if onboard_done is not None:
            data['onboard_done'] = bool(onboard_done)
            data['onboard_done_at'] = (time.strftime('%Y-%m-%d %H:%M:%S')
                                       if onboard_done else '')
        if post_submit_route_action is not None:
            action = str(post_submit_route_action)
            if action not in ('ask', 'save', 'clear'):
                return _fail('post_submit_route_action 只能是 ask/save/clear')
            data['post_submit_route_action'] = action
        if run_area_choice is not None:
            if not isinstance(run_area_choice, dict):
                return _fail('run_area_choice 必须是 {uid: areaId}')
            stored = data.get('run_area_choice')
            stored = dict(stored) if isinstance(stored, dict) else {}
            for key, value in run_area_choice.items():
                session_key = str(key)
                if value in (None, '', 0, '0'):
                    stored.pop(session_key, None)
                    continue
                try:
                    stored[session_key] = int(value)
                except (TypeError, ValueError, OverflowError):
                    return _fail('跑区 id 无效: %r' % (value,))
            data['run_area_choice'] = stored
        if disclaimer_version is not None:
            if isinstance(disclaimer_version, bool):
                return _fail('disclaimer_version 必须是整数')
            try:
                version = int(disclaimer_version)
            except (TypeError, ValueError, OverflowError):
                return _fail('disclaimer_version 必须是整数')
            if version < 0:
                return _fail('disclaimer_version 不能为负数')
            if version == 0:
                data.pop('disclaimer_version', None)
                data.pop('disclaimer_accepted_at', None)
            else:
                data['disclaimer_version'] = version
                data['disclaimer_accepted_at'] = time.strftime(
                    '%Y-%m-%d %H:%M:%S')
        if announcement_seen is not None:
            if not isinstance(announcement_seen, dict):
                return _fail('announcement_seen 必须是 {id: version}')
            stored = data.get('announcement_seen')
            stored = dict(stored) if isinstance(stored, dict) else {}
            for key, value in announcement_seen.items():
                announcement_id = str(key)
                if isinstance(value, bool):
                    return _fail('announcement_seen 的 version 必须是整数')
                if value in (None, '', 0, '0'):
                    stored.pop(announcement_id, None)
                    continue
                try:
                    version = int(value)
                except (TypeError, ValueError, OverflowError):
                    return _fail('announcement_seen 的 version 必须是整数')
                if version < 0:
                    return _fail('announcement_seen 的 version 不能为负数')
                stored[announcement_id] = version
            if stored:
                data['announcement_seen'] = stored
            else:
                data.pop('announcement_seen', None)
        if skin is not None:
            from ..application import preferences as P
            value = str(skin or '').strip().lower()
            if value not in P.SKINS:
                return _fail('未知外观主题: %s' % value)
            data['skin'] = value
        try:
            _write_json(path, data)
        except OSError as e:
            return _fail('写入 ui_state 失败: %s' % e)
        return _ok(data)

    # -- login cover (settings → 登录页封面) -------------------------------
    def _cover_path(self) -> str:
        return os.path.join(self.store_dir, 'login-cover.img')

    def _cover_meta_path(self) -> str:
        return os.path.join(self.store_dir, 'login-cover.json')

    def _cover_info(self) -> dict:
        meta = _read_json(self._cover_meta_path(), {})
        meta = meta if isinstance(meta, dict) else {}
        path = self._cover_path()
        if not os.path.isfile(path):
            return {'has_custom': False, 'url': '', 'width': 0, 'height': 0,
                    'size': 0, 'updated_at': ''}
        stamp = str(meta.get('updated_at') or int(os.path.getmtime(path)))
        return {
            'has_custom': True,
            'url': '/login-cover?t=' + stamp,
            'width': int(meta.get('width') or 0),
            'height': int(meta.get('height') or 0),
            'size': int(os.path.getsize(path)),
            'updated_at': stamp,
        }

    def real_get_login_cover(self):
        return _ok(self._cover_info())

    def real_set_login_cover(self, data: str = '', mime: str = '',
                             width=None, height=None):
        """Store a landscape cover for the login gate.

        The browser re-encodes large photos to JPEG, but small banners are
        sent as-is and direct bridge calls may be PNG/WebP.  Only the header is
        parsed: Pillow is not part of the frozen build, and a 20 MP camera JPEG
        must not be decoded just to read two integers.
        """
        import base64 as _b64
        import binascii
        raw_text = str(data or '').strip()
        if not raw_text:
            return _fail('没有收到图片数据')
        if raw_text.startswith('data:'):
            raw_text = raw_text.split(',', 1)[-1]
        try:
            raw = _b64.b64decode(raw_text, validate=False)
        except (binascii.Error, ValueError):
            return _fail('图片数据不是有效的 base64')
        if not raw:
            return _fail('图片数据为空')
        if len(raw) > COVER_MAX_BYTES:
            return _fail('图片超过 %d MB' % (COVER_MAX_BYTES // (1 << 20)))
        info = _image_info(raw)
        if info is None:
            return _fail('只支持 JPEG / PNG / WebP 图片')
        real_mime, w, h = info
        if w <= 0 or h <= 0:
            return _fail('读不出图片尺寸')
        if w <= h:
            return _fail('只支持横图（宽必须大于高）')
        if w < COVER_MIN_WIDTH or w * h < COVER_MIN_AREA:
            return _fail('图片太小：宽度至少 %d，面积至少 %d×%d'
                         % (COVER_MIN_WIDTH, COVER_MIN_WIDTH,
                            COVER_MIN_AREA // COVER_MIN_WIDTH))
        if w > 8000 or h > 8000:
            return _fail('图片尺寸过大（最大 8000 像素）')
        try:
            os.makedirs(self.store_dir, exist_ok=True)
            tmp = self._cover_path() + '.tmp'
            with open(tmp, 'wb') as fh:
                fh.write(raw)
            os.replace(tmp, self._cover_path())
            _write_json(self._cover_meta_path(), {
                'mime': real_mime, 'width': w, 'height': h,
                'size': len(raw),
                'updated_at': int(time.time() * 1000),
            })
        except OSError as e:
            return _fail('保存封面失败: %s' % e)
        return _ok(self._cover_info())

    def real_clear_login_cover(self):
        for path in (self._cover_path(), self._cover_meta_path(),
                     self._cover_path() + '.tmp'):
            try:
                os.remove(path)
            except OSError:
                pass
        return _ok(self._cover_info())

    # -- auto update ------------------------------------------------------
    def real_check_update(self):
        """Read the release manifest; the four states live in ``data.state``.

        A failed network check is not a bridge error: the UI stays quiet and
        the manual "检查更新" button can show the reason.
        """
        from ..application import updater
        return _ok(updater.check())

    def real_download_update(self, url: str = '', sha256: str = '',
                             size: int = 0, file: str = '', urls=None):
        """Fetch the package into ``data/updates`` and verify it (no install).

        ``urls`` is the mirror list from the manifest: the updater probes them
        and downloads from the fastest that answers.  ``url`` stays supported
        for an older UI and for manifests that only carry one link.
        """
        from ..application import updater
        targets = urls if isinstance(urls, (list, tuple)) and urls else url
        try:
            info = updater.download(
                targets, os.path.join(self.store_dir, 'updates'),
                sha256=sha256, size=size, file=file)
            return _ok(info)
        except Exception as e:  # noqa: BLE001 - show the reason in the UI
            return _fail('下载失败: %s' % e)

    def real_start_download_update(self, url: str = '', sha256: str = '',
                                   size: int = 0, file: str = '', urls=None):
        """Start a resumable background download and return its state."""
        from ..application import updater
        targets = urls if isinstance(urls, (list, tuple)) and urls else url
        try:
            state = updater.start_download(
                targets, os.path.join(self.store_dir, 'updates'),
                sha256=sha256, size=size, file=file,
                store_dir=self.store_dir)
            return _ok(state)
        except Exception as e:  # noqa: BLE001
            return _fail('启动下载失败: %s' % e)

    def real_update_download_status(self):
        from ..application import updater
        return _ok(updater.download_status(self.store_dir))

    def real_cancel_update_download(self):
        from ..application import updater
        return _ok(updater.cancel_download(self.store_dir))

    def real_update_last_result(self):
        from ..application import updater
        return _ok(updater.last_result(self.store_dir))

    def real_clear_update_last_result(self):
        from ..application import updater
        updater.clear_last_result(self.store_dir)
        return _ok({'cleared': True})

    def real_open_updates(self):
        """Open ``data/updates`` (manual fallback when auto-apply fails)."""
        path = os.path.join(self.store_dir, 'updates')
        try:
            os.makedirs(path, exist_ok=True)
            if hasattr(os, 'startfile'):
                os.startfile(path)  # noqa: S606 - Windows desktop app
        except OSError as e:
            return _fail('无法打开更新目录: %s' % e)
        return _ok({'path': path})

    def real_apply_update(self, file: str = '', version: str = ''):
        """Stage the downloaded zip and swap files after this process exits."""
        from ..application import updater
        try:
            package = updater.locate_package(self.store_dir, file)
            return _ok(updater.apply(package, self.store_dir, version=version))
        except Exception as e:  # noqa: BLE001 - the dialog shows the reason
            return _fail('自动更新失败: %s' % e)

    def real_quit_app(self):
        """Exit so the detached updater can replace the exe and restart it."""
        def _exit_later():
            time.sleep(1.2)
            os._exit(0)

        threading.Thread(target=_exit_later, daemon=True).start()
        return _ok({'closing': True})

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
            extra = {'generated_by': 'replica_api',
                     'build': build_info(),
                     'session_present': bool(self._real_account())}
            acct = self._real_account()
            if acct is not None:
                # One line per wire-visible identity field: when a report says
                # "iOS behaves differently", this is the first thing to check.
                from ..transport.realclient import user_agent
                extra['client'] = {
                    'platform': acct.platform,
                    'model': acct.deviceName,
                    'os': acct.osVersion,
                    'app_version': acct.appVersion,
                    'user_agent': user_agent(acct),
                }
                # Last local submission window.  When a report says "提交没入账",
                # the first question is which slot the tool actually claimed;
                # this answers it without asking the user to dig through files.
                try:
                    rows = self._receipt_store().list(str(acct.uid),
                                                      str(acct.unid))
                    last = max(rows, key=lambda r: int(r.get('start') or 0),
                               default=None)
                except Exception:  # noqa: BLE001 - diagnostics must not fail
                    last = None
                if last:
                    # NB: the collector drops top-level keys containing "ip",
                    # and "receipt" contains it -- hence last_submission.
                    extra['last_submission'] = {
                        'state': last.get('state'),
                        'start': S.fmt_beijing(last.get('start')) if last.get('start') else '',
                        'stop': S.fmt_beijing(last.get('stop')) if last.get('stop') else '',
                        'distance_m': last.get('distance_m'),
                    }
            info = D.collect(crypto_backend='netsec',
                             app_version=version_string(False),
                             extra=extra)
            return _ok({'info': info, 'text': D.render(info, 'text'),
                        'at': S.fmt_beijing(S.now_ms())})
        except Exception as e:  # noqa: BLE001
            return _fail('诊断包生成失败: %s' % e)

    def real_preview_model(self, km=2.5, pace=6.0, cadence=150,
                           altitude=25.0, waypoints=None, checkpoints=None,
                           days_ago=0, start_time=None,
                           daily_runs=None, daily_source=None, date=None,
                           closed=True, altitude_mode='auto',
                           altitude_range=None, altitude_profile=None,
                           km_times=None, with_series=False, seed=None,
                           sequential=False, pause_profile='none',
                           gps_profile='urban', terrain_coupling=False):
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
                            'isMustPoint': (bool(p.get('isMustPoint'))
                                            or str(p.get('isFixed')).strip()
                                            .lower() in ('1', 'true', 'yes',
                                                         'y', '是')),
                            'isFixed': p.get('isFixed'),
                            'position': p.get('position'),
                            'seq': p.get('seq')})
            d = self._read_session()
            lat = float(d.get('lat') or 0)
            lng = float(d.get('lon') or 0)
            if not self._center_ok(lat, lng):
                if not wps:
                    return _fail('尚未解析校区坐标，请先加载围栏/点位')
                lat, lng = wps[0]
            from ..domain.rundata import (estimate_pause_seconds,
                                          planned_active_seconds)
            duration_s = planned_active_seconds(km, pace, km_times)
            duration_s += estimate_pause_seconds(
                pause_profile, None, int(duration_s))
            rules = Q.load_rules()
            try:
                start_ms = S.resolve_start_ms(
                    days_ago, start_time, duration_s, date=date,
                    windows=rules.get('windows'))
            except ValueError as e:
                return _fail('开始时间无效: %s' % e)
            try:
                run_seed = int(seed) if seed is not None else secrets.randbits(32)
            except (TypeError, ValueError):
                run_seed = secrets.randbits(32)
            run = generate_run_data(
                float(km), float(pace), int(cadence),
                center_lat=lat, center_lng=lng,
                rng=random.Random(run_seed),
                waypoints=wps or None,
                checkpoints=cps or None,
                altitude_base=float(altitude),
                altitude_mode=altitude_mode or 'auto',
                altitude_range=altitude_range or None,
                altitude_profile=altitude_profile or None,
                start_ms=start_ms,
                closed=bool(closed),
                km_times=km_times,
                sequential=bool(sequential),
                pause_profile=pause_profile or 'none',
                gps_profile=gps_profile or 'urban',
                terrain_coupling=bool(terrain_coupling),
                run_uuid=new_run_uuid(
                    self._effective_device(d).get('platform')))
            self._last_run = run
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
            model = Q.evaluate(run, rules,
                               checkpoints=cps or None,
                               daily_runs=daily_runs,
                               daily_source=daily_source)
            return _ok({
                'model': model,
                'param_warnings': plausibility_warnings(pace, cadence),
                'seed': run_seed,
                'preview': preview_series(run) if with_series else None,
                'start': {'ms': start_ms,
                          'text': S.fmt_beijing(start_ms),
                          'days_ago': (S.to_beijing(S.now_ms()).date()
                                       - S.to_beijing(start_ms).date()).days},
                'daily': {'runs': daily_runs, 'source': daily_source,
                          'date': S.to_beijing(start_ms).strftime('%Y-%m-%d')},
                'run': {'km': run.get('km'), 'sportTime': run.get('sportTime'),
                        'juli': run.get('juli'),
                        'splits': run.get('km_splits') or [],
                        'points': len(run.get('allLocJson') or []),
                        'pause_s': run.get('pause_total_s', 0),
                        'gps_profile': run.get('gps_profile', 'urban'),
                        'terrain_coupling': bool(run.get('terrain_coupling'))},
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

    def real_submit_run(self, km=2.5, pace=6.0, cadence=150,
                        waypoints=None, geeToken='', use_points=True,
                        altitude=25.0, snap_route=True,
                        days_ago=0, start_time=None, date=None,
                        closed=True, altitude_mode='auto',
                        altitude_range=None, altitude_profile=None,
                        km_times=None, seed=None, run_area_id=None,
                        points_distance_m=None, points=None,
                        pause_profile='none', gps_profile='urban',
                        terrain_coupling=False):
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
        if self._device_migration:
            return _fail(self._device_migration.get('message')
                         or '设备指纹已修复，请重新登录后再提交')
        try:
            acct = self._real_account()
        except DP.DeviceProfileError as e:
            return _fail(str(e))
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

        # Align to the server clock before ANY signed request in this flow:
        # a laptop several minutes fast used to make the first policy call
        # look stale and the generated start time look "in the future".
        # Fail-open: an offline check must not block a submission.
        clock_info = self._sync_server_clock(acct, rc=rc, force=True,
                                             timeout=8.0)

        # The submit coordinate normally comes from this account's real fence.
        # Some schools configure no fence at all (official client: empty fence
        # list means "always in area"), and a manual campus point / the first
        # waypoint is a legitimate anchor there.  A wrong coordinate is still
        # worse than stopping, so never fall back to a built-in city.
        wps = [(float(a), float(b)) for a, b in (waypoints or [])]
        submit_notes = []

        # ---- 0. policy: the account's run areas + fence + runes ----------
        # `RunPolicyViewModel.getPolicy()` sends selectUnid=user.unid and the
        # reply carries `runAreaModels` and `geoFence`.  That is the official,
        # per-account campus mapping; use it before any name/nearest guess.
        from ..application import policy as POL
        policy_data = self._fetch_run_policy(rc, acct)
        if not policy_data:
            return _fail('无法读取跑步策略，已停止提交（不会用旧围栏或猜测校区）')
        area = POL.select_run_area(
            policy_data, 0,
            campus_name=acct.campusName,
            school_name=acct.schoolName)
        if run_area_id not in (None, '', -1, '-1'):
            chosen = POL.find_run_area(policy_data, run_area_id)
            if chosen:
                area = chosen
            else:
                submit_notes.append('所选跑区不可用，已按默认跑区重新匹配')
        if area is not None and area.get('valid') is False:
            return _fail('跑区有效时间已更新，请重新选择')
        policy_ts = POL.policy_timestamp(policy_data)
        freeze = POL.freeze_seconds(policy_data)
        face_on, face_why = POL.face_required(policy_data)
        sequential = POL.policy_value(policy_data, 0) == 1
        if area:
            submit_notes.append('跑区：%s' % (area.get('name') or area.get('id')))
        if freeze > 0:
            # A freeze is a strong warning, not a hard block: the operator may
            # have a legitimate reason and the server decides.
            submit_notes.append(
                '账号处于跑步冻结期（剩余 %s）；官方客户端此时不能开始跑步，'
                '仍按你的要求继续提交' % _fmt_seconds(freeze))
        if face_on:
            # The real client can only report `faceCheck=1` after a face check.
            # This tool cannot perform one, so submitting would be a false
            # claim; stop instead of guessing.
            return _fail(
                '学校启用了人脸抽检（%s），本工具无法完成人脸校验，已停止提交'
                '（不会伪造 faceCheck=1）' % face_why)
        if sequential:
            submit_notes.append('顺序跑：按点位编号排序并依次经过')

        fence = {'polygon': [], 'polygons': []}
        try:
            fence = self._ensure_fence(
                acct, rc, policy_data=policy_data, run_area=area,
                anchor=(wps[0] if wps else None))
            polygons = fence.get('polygons') or (
                [fence['polygon']] if fence.get('polygon') else [])
            if not polygons:
                submit_notes.append('该校区未配置围栏，无法校验轨迹是否在围栏内')
            elif fence.get('center_source') == 'route':
                submit_notes.append(
                    '检测到多个围栏：按路线首点确定本次校区（不会保存为账号校区）')
            elif fence.get('center_source') == 'manual':
                submit_notes.append('检测到多个围栏：按已保存的校区坐标提交')
        except P.PolicyUnavailable as e:
            return _fail(str(e))
        except P.FenceAmbiguous as e:
            return _fail(str(e))
        except P.FenceNotConfigured as e:
            center = None
            if self._center_ok(acct.center_lat, acct.center_lng):
                center = (acct.center_lat, acct.center_lng)
                self._save_center(*center)
                submit_notes.append('该校区未配置围栏，已按手动校区坐标提交')
            elif wps:
                # A route's first point is a valid transient anchor for a
                # school with no fence, but it is not the account's campus.
                center = wps[0]
                acct.center_lat, acct.center_lng = float(center[0]), float(center[1])
                submit_notes.append(
                    '该校区未配置围栏，已按路线首点确定本次坐标（不会保存为校区）')
            if center is None:
                return _fail('学校未配置围栏，且没有可用的校区坐标：请先把地图'
                             '拖到学校后点「设为校区」，或画一条以学校为起点的路线')
        except Exception as e:  # noqa: BLE001
            # A saved centre says *where* the campus is, not whether the track
            # is inside its polygon.  Continuing here used to leave `fence`
            # empty and skip the in-area check entirely, so a transient read
            # failure could submit an out-of-fence record.
            return _fail('无法读取校区围栏，已停止提交（避免提交围栏外轨迹）: %s'
                         % e)

        # ---- 1. live checkpoints -> route through them --------------------
        # Checkpoints are fetched even when a route was supplied: a record
        # without them can never be complete (this used to be skipped for any
        # hand-drawn or imported route).
        cps = []
        if use_points:
            # Prefer the exact set the operator saw on the map.  The endpoint
            # returns a different random five-point draw per request (see
            # points.py), so re-querying at submit time would route through
            # points that were never displayed.
            cps = _normalise_ui_points(points)
            if cps:
                submit_notes.append('使用地图上的 %d 个打卡点' % len(cps))
                try:
                    shown = float(points_distance_m or 0)
                    target = float(km) * 1000.0
                except (TypeError, ValueError):
                    shown, target = 0.0, 0.0
                if shown > 0 and target > 0 and abs(shown - target) >= 50:
                    submit_notes.append(
                        '点位按 %d m 加载，本次目标 %d m；提交复用该套点位'
                        % (int(round(shown)), int(round(target))))
            else:
                dist_points = P.points_distance_for_submit(
                    points_distance_m, km)
                try:
                    meta = P.fetch_points_meta(
                        rc, acct, acct.center_lat, acct.center_lng,
                        run_area_id=area['id'] if area else None,
                        distance_m=dist_points, allow_empty=False)
                    cps = meta['points']
                    if meta['stale']:
                        submit_notes.append(
                            '点位沿用 %d m 的缓存（当前目标 %d m；接口限流）'
                            % (meta['distance_m'],
                               int(round(float(km) * 1000))))
                    if not cps:
                        return _fail(
                            '服务器没有返回打卡点，无法确认该校区是否允许自由跑；'
                            '如你确认学校没有点位，请勾选「跳过打卡点」后重试')
                except ValueError as e:
                    # Empty points are not proof of a free-run campus.  The
                    # decompiled client has no such branch on the normal
                    # distance endpoint; official free run is serviceType 3 /
                    # sportType 4.
                    return _fail(
                        '服务器没有返回有效打卡点，无法确认该校区是否允许自由跑；'
                        '如你确认学校没有点位，请勾选「跳过打卡点」后重试: %s' % e)
                except Exception as e:  # noqa: BLE001
                    # A record without the mandatory checkpoint hits cannot be
                    # complete.  Submitting anyway created an
                    # accepted-but-invalid run, so stop before the write-ahead
                    # receipt is reserved.
                    return _fail(
                        '点位加载失败，已停止提交（避免生成不完整记录）: %s' % e)
            if not wps:
                wps = P.route_order(cps, acct.center_lat, acct.center_lng,
                                    sequential=sequential)
        else:
            submit_notes.append(
                '已按操作员确认跳过打卡点；本工具尚未实现官方自由跑'
                '（serviceType=3），提交仍按普通跑步生成')

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
            return _fail('学校策略要求必经点位，不能跳过打卡点'
                         '（请取消「跳过打卡点」后重试）')
        if not use_points and sequential:
            return _fail('学校要求顺序经过点位，不能跳过打卡点'
                         '（请取消「跳过打卡点」后重试）')
        if mp:
            if sequential:
                # `merge_required` prepends must-points and `rotate_to` moves
                # the user's first point back to index 0; either would break
                # the school's numbering for a sequential run.  The checkpoint
                # list already carries every point the route must visit, so
                # only append a policy point that is genuinely absent.
                missing = []
                for p in mp:
                    if not any(abs(p[0] - q[0]) < 1e-6
                               and abs(p[1] - q[1]) < 1e-6 for q in wps):
                        missing.append(p)
                if missing:
                    wps.extend(missing)
                    submit_notes.append(
                        '顺序跑：策略必经点不在点位列表，已追加到路线末尾')
                else:
                    submit_notes.append('顺序跑：策略必经点已包含在点位序列中')
            else:
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
        # The operator may backfill today .. 3 days ago.  Resolve the slot
        # before forging so every timestamp in the record agrees.  The school's
        # validTime bands decide the allowed starts; merge the policy fetched
        # above so a stale school_rules.json cannot reject a valid 18:00 run.
        from ..domain import safety as S
        from ..domain.rundata import (estimate_pause_seconds,
                                      planned_active_seconds)
        from ..application import qualification as Q
        from ..application import policy as POL
        rules = Q.load_rules()
        rules, rules_refreshed = POL.merge_rules(rules, policy_data)
        if rules_refreshed:
            submit_notes.append(
                '已按服务器最新规则刷新学校要求（手动保存的规则不会被自动覆盖）')
            try:
                Q.save_rules(rules)
            except Exception:  # noqa: BLE001
                pass
        duration_s = planned_active_seconds(km, pace, km_times)
        duration_s += estimate_pause_seconds(
            pause_profile, None, int(duration_s))
        try:
            start_ms = S.resolve_start_ms(
                days_ago, start_time, duration_s, date=date,
                windows=rules.get('windows'))
        except ValueError as e:
            return _fail(f'开始时间无效: {e}')
        day_key = S.to_beijing(start_ms).strftime('%Y-%m-%d')
        start_days_ago = (S.to_beijing(S.now_ms()).date()
                          - S.to_beijing(start_ms).date()).days

        # Keep the night guard, but do not turn the school's daily cap into a
        # hard block here.  The cap is a *reminder* in the model; the operator
        # may still have a legitimate reason to submit (and the server decides).
        from ..application import limits as L
        start_hhmm = S.to_beijing(start_ms).strftime('%H:%M')
        allow_night = bool(rules.get('windows')) and S.in_windows(
            start_hhmm, rules['windows'])
        refusal = L.check(acct.uid, start_ms, max_per_day=0,
                          block_night=not allow_night)
        if refusal:
            return _fail(refusal, data={'start': {
                'ms': start_ms, 'text': S.fmt_beijing(start_ms),
                'days_ago': start_days_ago}})

        try:
            run_seed = None
            if seed is not None:
                try:
                    run_seed = int(seed)
                except (TypeError, ValueError):
                    run_seed = None
            run = generate_run_data(float(km), float(pace), int(cadence),
                                    center_lat=acct.center_lat,
                                    center_lng=acct.center_lng,
                                    rng=(random.Random(run_seed)
                                         if run_seed is not None else None),
                                    waypoints=wps or None,
                                    checkpoints=cps or None,
                                    altitude_base=float(altitude),
                                    altitude_mode=altitude_mode or 'auto',
                                    altitude_range=altitude_range or None,
                                    altitude_profile=altitude_profile or None,
                                    start_ms=start_ms,
                                    closed=bool(closed),
                                    km_times=km_times,
                                    run_area_id=area['id'] if area else None,
                                    geo_fences=fence.get('fences') or None,
                                    sequential=sequential,
                                    pause_profile=pause_profile or 'none',
                                    gps_profile=gps_profile or 'urban',
                                    terrain_coupling=bool(terrain_coupling),
                                    run_uuid=new_run_uuid(
                                        getattr(acct, 'platform', None)))
        except Exception as e:  # noqa: BLE001
            return _fail(f'路线生成失败: {e}')
        self._last_run = run

        # Same pre-submit gate as the CLI: geometry vs claimed distance,
        # BD/GCJ pairs, isPass, bucket sums, lap coverage.
        from ..domain.rundata import validate_run_data
        issues = validate_run_data(run)
        if issues:
            return _fail('提交前自检未通过: %s' % issues)

        # A wrong centre is the one mistake that still yields an *accepted*
        # record.  When the school has a fence, reject the track when it starts
        # outside it, or when most of it is outside.  A school without a fence
        # has nothing to validate against, and saying so is better than
        # refusing a valid run.
        polygons = fence.get('polygons') or (
            [fence['polygon']] if fence.get('polygon') else [])
        if polygons:
            try:
                import json as _json
                from ..domain import routes as RT
                wrap = run.get('locJsonEntity') or {}
                raw = wrap.get('allLocJson') if isinstance(wrap, dict) else None
                if isinstance(raw, str):
                    raw = _json.loads(raw)
                gcj = [(float(p['gLat']), float(p['gLng']))
                       for p in (raw or [])
                       if isinstance(p, dict) and 'gLat' in p and 'gLng' in p]
                if not gcj:
                    return _fail('提交前围栏校验失败：轨迹缺少 GCJ 坐标，已停止提交')
                if not RT.point_in_fences(gcj[0][0], gcj[0][1], polygons):
                    return _fail('生成路线起点不在任一校区围栏内（坐标未初始化？），'
                                 '已停止提交')
                bad = RT.outside_fences(gcj, polygons)
                if bad['count']:
                    worst = max(p['distance_m'] for p in bad['points'])
                    if bad['count'] > len(gcj) * 0.25 or worst > 150:
                        return _fail('生成路线有 %d/%d 个点不在任何围栏内（最远 %.0fm），'
                                     '已停止提交' % (bad['count'], bad['total'],
                                                     worst))
            except Exception as e:  # noqa: BLE001
                return _fail('提交前围栏校验失败，已停止提交: %s' % e)

        # User-entered school rules -> qualification model.  The rules were
        # already merged with the fresh runModePolicy before start resolution;
        # reuse that copy so the gate and the timestamp agree.
        from ..application import qualification as Q
        from ..application import policy as POL
        from ..application import readonly as RO
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
        rules_warning = ''
        if not model.get('configured'):
            # No usable band.  Do not hard-block: a school that publishes no
            # rule model has nothing for the server to score `useMobilityTools`
            # against either, so refusing here would only break a valid flow.
            # Say it out loud instead of passing silently.
            rules_warning = ('学校要求没有读到，本次提交没有校验配速/步频区间；'
                             '服务端若按 runModePolicy 复核，可能判为不合格。')
            model.setdefault('warnings', []).insert(0, rules_warning)
        if model.get('configured') and model.get('verdict') == 'fail':
            bad = [c['label'] + ' ' + c['value'] +
                   ('（' + c.get('detail') + '）' if c.get('detail') else '')
                   for c in model['checks'] if c['status'] == 'fail']
            return _fail('合格模型未通过: %s' % '；'.join(bad),
                         data={'model': model,
                               'param_warnings': plausibility_warnings(pace, cadence)})

        from ..application import policy as POL
        from ..application import riskcheck as RK
        # Official `OutdoorRunViewModel.saveStartPointToDb` reverse-geocodes
        # the run's start point and stores the city in `SportRecord.address`.
        # Use the generated first point, not the campus name; if AMap is not
        # configured fall back to the city in the campus name, then to "".
        first = (run.get('locPoints') or [{}])[0]
        city = RK.address_for_start(
            first.get('gLat'), first.get('gLng'),
            acct.campusName, acct.schoolName,
            key=os.environ.get('JHP_AMAP_KEY') or AMAP_KEY) or ''
        ctx = {
            'uid': acct.uid,
            'selectedUnid': acct.unid,
            'sportType': 1,
            'geeToken': geeToken or '',
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
        res['freeze_seconds'] = freeze
        res['face_required'] = face_on
        res['sequential'] = sequential
        res['clock'] = clock_info
        if not (isinstance(clock_info, dict) and clock_info.get('applied')):
            res.setdefault('warnings', [])
            if isinstance(res['warnings'], list):
                res['warnings'].append(
                    '未能校准服务器时间，本次按本机时钟生成；'
                    '若提交被软拒绝，重试一次即可（不会修改系统设置）。')
        if freeze > 0:
            res.setdefault('warnings', [])
            if isinstance(res['warnings'], list):
                res['warnings'].append(
                    '账号处于跑步冻结期（剩余 %s）' % _fmt_seconds(freeze))
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
            from ..application import preferences as PREF
            time.sleep(1.0)          # reference: flow.rs sleeps 1 s here
            fpj = json.dumps(run['fixed_point_json'], separators=(',', ':'))
            obs_res = OBS.upload_obs(
                rc, acct, run, res['rrid'], res['uuid'], fpj,
                log=lambda *_: None,
                key_mode=PREF.get(self.store_dir, 'obs_key_mode', 'single'))
            res['obs'] = obs_res
            time.sleep(2.0)          # reference: flow.rs sleeps 2 s before verify
        except Exception as e:  # noqa: BLE001
            res['obs_error'] = str(e)

        # ---- 5. read back -------------------------------------------------
        try:
            back = rc.request(acct, 'api/v70320/runnings/get_one_record', {
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
        res['param_warnings'] = (plausibility_warnings(pace, cadence)
                                 + ([rules_warning] if rules_warning else [])
                                 + submit_notes)
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

    # -- LLM settings & connection probe ---------------------------------------
    def real_llm_settings(self):
        from ..application import llm_settings
        return _ok({
            'settings': llm_settings.load(self.store_dir),
            'protocols': llm_settings.presets(),
        })

    def real_llm_settings_save(self, payload=None, **kw):
        from ..application import llm_settings
        from ..application.credentials import CredentialError
        if payload is not None and not isinstance(payload, dict):
            return _fail('LLM 设置参数格式无效')
        merged = dict(payload) if isinstance(payload, dict) else {}
        if kw:
            merged.update(kw)
        try:
            saved = llm_settings.save(self.store_dir, merged)
        except (ValueError, CredentialError) as e:
            return _fail(str(e))
        return _ok({
            'settings': saved,
            'protocols': llm_settings.presets(),
        })

    def real_llm_settings_clear(self):
        from ..application import llm_settings
        return _ok({'cleared': bool(llm_settings.clear(self.store_dir))})

    def real_llm_test(self):
        from ..application import llm_client, llm_settings
        settings = llm_settings.effective(self.store_dir)
        if settings is None:
            return _fail('LLM 未启用或未配置 API Key')
        proxy = (os.environ.get('JHP_PROXY') or '').strip()
        proxies = {'http': proxy, 'https': proxy} if proxy else None
        res = llm_client.test_connection(settings, proxies=proxies)
        if res.get('ok'):
            return _ok(res)
        return _fail(res.get('error') or '连接测试失败', data=res)

    # -- route memory ------------------------------------------------------
    @staticmethod
    def _rm_default_progress() -> dict:
        return {
            'running': False,
            'phase': 'idle',
            'done': 0,
            'total': 0,
            'error': '',
            'owner': '',
            'started_at_ms': 0,
            'finished_at_ms': 0,
        }

    def _rm_ensure_state(self):
        lock = getattr(self, '_rm_lock', None)
        if lock is None:
            lock = threading.RLock()
            self._rm_lock = lock
        if not isinstance(getattr(self, '_rm_progress', None), dict):
            self._rm_progress = self._rm_default_progress()
        return lock

    def _rm_get_progress(self) -> dict:
        lock = self._rm_ensure_state()
        with lock:
            return dict(self._rm_progress)

    def _rm_progress_for(self, owner: str) -> dict:
        prog = self._rm_get_progress()
        if prog.get('owner') and prog.get('owner') != owner:
            return self._rm_default_progress()
        return prog

    def _rm_exec_lock(self):
        lock = getattr(self, '_rm_exec', None)
        if lock is None:
            lock = threading.Lock()
            self._rm_exec = lock
        return lock

    def _rm_suggest_cache_obj(self):
        with self._rm_cache_lock:
            if self._rm_suggest_cache is None:
                from ..application import agent_cache
                self._rm_suggest_cache = agent_cache.TTLCache(
                    ttl_s=90.0, max_entries=16)
            return self._rm_suggest_cache

    def _rm_apply_cache_obj(self):
        with self._rm_cache_lock:
            if self._rm_apply_cache is None:
                from ..application import agent_cache
                self._rm_apply_cache = agent_cache.TTLCache(
                    ttl_s=300.0, max_entries=24)
            return self._rm_apply_cache

    def _rm_fence_cache_obj(self):
        with self._rm_cache_lock:
            if self._rm_fence_cache is None:
                from ..application import agent_cache
                self._rm_fence_cache = agent_cache.TTLCache(
                    ttl_s=60.0, max_entries=4)
            return self._rm_fence_cache

    def _agent_choice_cache(self):
        with self._rm_cache_lock:
            if self._agent_choice_cache_obj is None:
                from ..application import agent_cache
                self._agent_choice_cache_obj = agent_cache.ChoiceCache(
                    self.store_dir)
            return self._agent_choice_cache_obj

    def _rm_cache_clear(self):
        with self._rm_cache_lock:
            if self._rm_suggest_cache is not None:
                self._rm_suggest_cache.clear()
            if self._rm_apply_cache is not None:
                self._rm_apply_cache.clear()

    def _rm_run_learn_guarded(self, acct, limit_n: int, campus: dict) -> dict:
        # Serialize every learn (sync or background) so two callers never
        # rewrite progress.json / the memory doc at the same time.
        with self._rm_exec_lock():
            return self._rm_run_learn(acct, limit_n, campus)

    def _rm_error_text(self, prefix: str, err, acct=None) -> str:
        from ..domain import safety as S

        text = S.redact_text(str(err or '')).strip()
        token = str(getattr(acct, 'token', '') or '')
        if token:
            text = text.replace(token, '***')
        return (prefix + '：' + text) if text else prefix

    def _rm_set_progress(self, mirror: bool = True, **fields) -> dict:
        lock = self._rm_ensure_state()
        with lock:
            self._rm_progress.update(fields)
            snap = dict(self._rm_progress)
            if mirror:
                try:
                    _write_json(
                        os.path.join(self.store_dir, 'route_memory', 'progress.json'),
                        snap,
                    )
                except Exception:  # noqa: BLE001 - best-effort UI progress mirror
                    pass
        return snap

    def _rm_require_account(self):
        try:
            acct = self._real_account()
        except DP.DeviceProfileError as e:
            return None, _fail(str(e))
        if acct is None or not getattr(acct, 'token', ''):
            return None, _fail('未配置 token（先在"账号"面板保存）')
        return acct, None

    @staticmethod
    def _rm_normalise_points(points) -> list[dict]:
        out: list[dict] = []
        for p in points or []:
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
            if abs(flat) < 1e-9 or abs(flon) < 1e-9:
                continue
            out.append({
                'lat': flat,
                'lon': flon,
                'isMustPoint': bool(
                    p.get('isMustPoint') if 'isMustPoint' in p else p.get('must')),
                'name': str(p.get('name') or p.get('n') or p.get('pointName') or ''),
            })
        return out

    @staticmethod
    def _rm_normalise_fence(fence) -> list[list[tuple[float, float]]] | None:
        if not fence:
            return None
        if isinstance(fence, dict):
            for key in ('polygons', 'rings', 'points'):
                if isinstance(fence.get(key), (list, tuple)):
                    fence = fence[key]
                    break
            else:
                if isinstance(fence.get('polygon'), (list, tuple)):
                    fence = [fence['polygon']]
                else:
                    return None
        if not isinstance(fence, (list, tuple)) or not fence:
            return None
        first = fence[0]
        is_point = (
            (isinstance(first, dict)
             and any(k in first for k in ('lat', 'glat', 'gLat', 'latitude')))
            or (isinstance(first, (list, tuple)) and len(first) >= 2
                and not isinstance(first[0], (list, tuple, dict)))
        )
        raw_rings = [fence] if is_point else fence
        rings: list[list[tuple[float, float]]] = []
        for raw_ring in raw_rings:
            if isinstance(raw_ring, dict) and isinstance(raw_ring.get('points'), (list, tuple)):
                raw_ring = raw_ring['points']
            if not isinstance(raw_ring, (list, tuple)):
                continue
            pts: list[tuple[float, float]] = []
            for item in raw_ring:
                if isinstance(item, dict):
                    lat = next((item[k] for k in ('lat', 'glat', 'gLat', 'latitude')
                                if item.get(k) is not None), None)
                    lng = next((item[k] for k in ('lon', 'lng', 'glon', 'gLng', 'longitude')
                                if item.get(k) is not None), None)
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    lat, lng = item[0], item[1]
                else:
                    continue
                try:
                    flat, flng = float(lat), float(lng)
                except (TypeError, ValueError):
                    continue
                if (math.isfinite(flat) and math.isfinite(flng)
                        and -90.0 <= flat <= 90.0 and -180.0 <= flng <= 180.0
                        and (abs(flat) > 1e-9 or abs(flng) > 1e-9)):
                    pts.append((flat, flng))
            if len(pts) >= 3:
                rings.append(pts)
        return rings or None

    def _rm_tool_receipt_ids(self, acct) -> tuple[set[str], set[str]]:
        uuids: set[str] = set()
        rrids: set[str] = set()
        uid_s = str(getattr(acct, 'uid', '') or '')
        unid_raw = getattr(acct, 'unid', None)
        unid_s = str(unid_raw) if unid_raw not in (None, '') else 'default'
        raw = f'{uid_s}__{unid_s}'
        safe = ''.join(c if c.isalnum() or c in '-_.' else '_' for c in raw)
        path = os.path.join(self.store_dir, 'submission_receipts', safe + '.json')
        data = _read_json(path, {})
        if isinstance(data, dict):
            for k, v in data.items():
                if k:
                    uuids.add(str(k))
                if isinstance(v, dict):
                    if v.get('uuid'):
                        uuids.add(str(v['uuid']))
                    if v.get('rrid') not in (None, ''):
                        rrids.add(str(v['rrid']))
        return uuids, rrids

    def _rm_campus(self, acct) -> dict:
        campus: dict = {'center': [], 'fence_hash': ''}
        d = self._read_session()
        if self._center_ok(d.get('lat'), d.get('lon')):
            campus['center'] = [round(float(d['lat']), 6), round(float(d['lon']), 6)]
        elif self._center_ok(getattr(acct, 'center_lat', 0), getattr(acct, 'center_lng', 0)):
            campus['center'] = [
                round(float(acct.center_lat), 6),
                round(float(acct.center_lng), 6),
            ]
        return campus

    def _rm_run_learn(self, acct, limit_n: int, campus: dict) -> dict:
        from ..application import readonly as RO
        from ..application import route_memory as RM
        from ..domain import safety as S
        from ..transport import realclient as R

        try:
            rc = R.RealClient()
            listing = RO.recent_records(rc, acct, limit=limit_n)
            if not isinstance(listing, dict) or not listing.get('ok'):
                raw = (listing.get('error') or listing.get('message')
                       if isinstance(listing, dict) else '')
                err_msg = self._rm_error_text('记录列表查询失败', raw, acct)
                self._rm_set_progress(
                    running=False, phase='error',
                    error=err_msg,
                    finished_at_ms=int(time.time() * 1000),
                )
                return _fail(err_msg)

            rows = list(listing.get('records') or [])
            total = len(rows)
            self._rm_set_progress(phase='detail', done=0, total=total)

            receipt_uuids, receipt_rrids = self._rm_tool_receipt_ids(acct)
            tracks: list[dict] = []
            skipped: list[dict] = []

            for idx, row in enumerate(rows):
                if idx > 0:
                    time.sleep(0.15)
                row_rrid = row.get('rrid') if isinstance(row, dict) else None
                row_uuid = (row.get('uuid') or '') if isinstance(row, dict) else ''
                try:
                    detail = RO.record_detail(
                        rc, acct, row_rrid,
                        uuid=str(row_uuid) if row_uuid else None)
                except Exception as e:  # noqa: BLE001
                    if len(skipped) < 50:
                        skipped.append({
                            'rrid': str(row_rrid or ''),
                            'reason': S.redact_text(str(e))[:160],
                        })
                    self._rm_set_progress(done=idx + 1)
                    continue

                if not isinstance(detail, dict) or not detail.get('ok'):
                    reason = (detail.get('error')
                              if isinstance(detail, dict) else '') or '详情读取失败'
                    if len(skipped) < 50:
                        skipped.append({
                            'rrid': str(row_rrid or ''),
                            'reason': S.redact_text(str(reason))[:160],
                        })
                    self._rm_set_progress(done=idx + 1)
                    continue

                d = detail.get('detail') or {}
                detail_rrid = d.get('rrid') if d.get('rrid') not in (None, '') else row_rrid
                detail_uuid = d.get('uuid') or row_uuid or ''
                is_tool = (
                    (bool(detail_uuid) and str(detail_uuid) in receipt_uuids)
                    or (bool(row_uuid) and str(row_uuid) in receipt_uuids)
                    or (detail_rrid not in (None, '') and str(detail_rrid) in receipt_rrids)
                    or (row_rrid not in (None, '') and str(row_rrid) in receipt_rrids)
                )
                dist_m = d.get('distance_m')
                if dist_m is None and isinstance(row, dict):
                    dist_m = row.get('distance_m', 0.0)
                dur_s = d.get('duration_s')
                if dur_s is None and isinstance(row, dict):
                    dur_s = row.get('duration_s', 0)
                start_ms = d.get('start_ms')
                if start_ms is None and isinstance(row, dict):
                    start_ms = row.get('start_ms', 0)
                complete = d.get('complete')
                if complete is None and isinstance(row, dict):
                    complete = row.get('complete', False)

                tracks.append({
                    'rrid': str(detail_rrid or ''),
                    'line': RM.track_from_detail(detail),
                    'distance_m': dist_m if dist_m is not None else 0.0,
                    'duration_s': dur_s if dur_s is not None else 0,
                    'start_ms': start_ms if start_ms is not None else 0,
                    'is_cheat': d.get('is_cheat', 0),
                    'complete': bool(complete),
                    'source': 'tool' if is_tool else 'server',
                })
                self._rm_set_progress(done=idx + 1)

            self._rm_set_progress(phase='learning')
            owner = f'{acct.uid}:{acct.unid}'
            doc = RM.learn_from_tracks(tracks, owner, campus=campus)
            doc['records_seen'] = len(rows)
            doc['records_skipped'] = max(0, len(rows) - int(doc.get('records_readable') or 0))

            self._rm_set_progress(phase='saving')
            RM.save(self.store_dir, acct.uid, acct.unid, doc)
            # The memory revision changed, so every cached suggestion/apply
            # result is stale even if the file signature happens to collide.
            self._rm_cache_clear()
            summary = RM.summarize(doc)
            progress = self._rm_set_progress(
                running=False,
                phase='done',
                done=total,
                total=total,
                error='',
                finished_at_ms=int(time.time() * 1000),
            )
            templates = [dict(t) for t in (summary.get('templates_detail') or [])]
            return _ok({
                'summary': summary,
                'templates': templates,
                'skipped': skipped,
                'cached': False,
                'progress': progress,
            })
        except Exception as e:  # noqa: BLE001
            err_msg = self._rm_error_text('路线学习失败', e, acct)
            self._rm_set_progress(
                running=False,
                phase='error',
                error=err_msg,
                finished_at_ms=int(time.time() * 1000),
            )
            return _fail(err_msg)

    def real_route_memory_status(self):
        from ..application import route_memory as RM

        acct, err = self._rm_require_account()
        if err is not None:
            return err
        doc = RM.load(self.store_dir, acct.uid, acct.unid)
        summary = RM.summarize(doc)
        templates = [dict(t) for t in (summary.get('templates_detail') or [])]
        return _ok({
            'summary': summary,
            'templates': templates,
            'progress': self._rm_progress_for(f'{acct.uid}:{acct.unid}'),
            'cache': {
                'suggest': self._rm_suggest_cache_obj().stats(),
                'apply': self._rm_apply_cache_obj().stats(),
                'fence': self._rm_fence_cache_obj().stats(),
            },
        })

    def real_route_memory_learn(self, limit=50, force=False, wait=True):
        from ..application import route_memory as RM

        acct, err = self._rm_require_account()
        if err is not None:
            return err

        try:
            limit_n = int(limit)
        except (TypeError, ValueError):
            limit_n = 50
        limit_n = max(1, min(100, limit_n))

        lock = self._rm_ensure_state()
        existing = None
        with lock:
            if self._rm_progress.get('running'):
                if not wait:
                    return _ok({'started': False, 'progress': dict(self._rm_progress)})
                existing = getattr(self, '_rm_thread', None)
        if existing is not None and existing.is_alive():
            existing.join(timeout=120.0)
            if existing.is_alive():
                return _fail('路线学习仍在进行，请稍后再看状态')

        doc = RM.load(self.store_dir, acct.uid, acct.unid)
        now_ms = int(time.time() * 1000)
        owner = f'{acct.uid}:{acct.unid}'
        if (not force and doc.get('templates') and doc.get('updated_at_ms')
                and (now_ms - int(doc['updated_at_ms'])) < 24 * 3600 * 1000):
            summary = RM.summarize(doc)
            templates = [dict(t) for t in (summary.get('templates_detail') or [])]
            payload = {
                'summary': summary,
                'templates': templates,
                'skipped': [],
                'cached': True,
                'progress': self._rm_progress_for(owner),
            }
            if not wait:
                payload['started'] = False
            return _ok(payload)

        campus = self._rm_campus(acct)
        with lock:
            if self._rm_progress.get('running'):
                if not wait:
                    return _ok({'started': False,
                                'progress': self._rm_progress_for(owner)})
            self._rm_set_progress(
                running=True,
                phase='listing',
                done=0,
                total=0,
                error='',
                owner=owner,
                started_at_ms=now_ms,
                finished_at_ms=0,
            )
            if not wait:
                t = threading.Thread(
                    target=self._rm_run_learn_guarded,
                    args=(acct, limit_n, campus),
                    daemon=True,
                )
                self._rm_thread = t
                t.start()
                return _ok({'started': True,
                            'progress': self._rm_progress_for(owner)})

        return self._rm_run_learn_guarded(acct, limit_n, campus)

    def real_route_memory_suggest(self, km=2.5, points=None, fence=None,
                                  run_area_id=None):
        from ..application import agent_cache
        from ..application import route_memory as RM

        acct, err = self._rm_require_account()
        if err is not None:
            return err
        owner = f'{acct.uid}:{acct.unid}'

        try:
            km_val = float(km)
        except (TypeError, ValueError):
            km_val = 2.5
        if not math.isfinite(km_val):
            km_val = 2.5
        km_val = max(0.5, min(20.0, km_val))

        cps = self._rm_normalise_points(points)
        fence_norm = self._rm_normalise_fence(fence) if fence is not None else None
        fence_center = None
        if fence is None:
            # The caller usually passes LIVE.fences.  When it does not, try the
            # same fence endpoint the map uses; a failure simply means "no
            # fence filter", never an error.  A short-lived fence cache keeps
            # repeated suggest calls from re-hitting the network.
            fence_cache = self._rm_fence_cache_obj()
            fence_key = f'{owner}|{run_area_id}'
            cached_fence = fence_cache.get(fence_key)
            if isinstance(cached_fence, dict):
                fence_norm = cached_fence.get('fence')
                cached_center = cached_fence.get('center')
                if (isinstance(cached_center, (list, tuple))
                        and len(cached_center) == 2):
                    fence_center = (float(cached_center[0]),
                                    float(cached_center[1]))
            else:
                got = None
                try:
                    got = self.real_fetch_fence()
                except Exception:  # noqa: BLE001 - offline or policy unavailable
                    got = None
                data = (got.get('data') if isinstance(got, dict)
                        and got.get('code') == 200 else None)
                if isinstance(data, dict):
                    fence_norm = self._rm_normalise_fence(
                        data.get('polygons') if data.get('polygons') is not None
                        else data.get('polygon'))
                    c = data.get('center')
                    if isinstance(c, dict) and self._center_ok(c.get('lat'), c.get('lon')):
                        fence_center = (float(c['lat']), float(c['lon']))
                    if fence_norm is not None or fence_center is not None:
                        fence_cache.put(fence_key, {
                            'fence': fence_norm,
                            'center': list(fence_center) if fence_center else None,
                        })

        center = None
        if cps:
            center = (cps[0]['lat'], cps[0]['lon'])
        elif fence_center is not None:
            center = fence_center
        else:
            d = self._read_session()
            if self._center_ok(d.get('lat'), d.get('lon')):
                center = (float(d['lat']), float(d['lon']))
            elif self._center_ok(getattr(acct, 'center_lat', 0),
                                 getattr(acct, 'center_lng', 0)):
                center = (float(acct.center_lat), float(acct.center_lng))

        # The geometry is deterministic for a given memory revision and input
        # set.  The cache key therefore includes the memory file signature so
        # learning a new route invalidates every old answer.
        cache_key = agent_cache.stable_hash({
            'owner': owner,
            'doc': agent_cache.file_signature(
                RM.memory_path(self.store_dir, acct.uid, acct.unid)),
            'km': round(km_val, 3),
            'points': cps,
            'fence': fence_norm,
            'center': center,
        })
        cache = self._rm_suggest_cache_obj()
        cached = cache.get(cache_key)
        if isinstance(cached, dict):
            data = dict(cached)
            data['progress'] = self._rm_progress_for(owner)
            data['cached'] = True
            return _ok(data, message=data.get('message') or 'success')

        doc = RM.load(self.store_dir, acct.uid, acct.unid)
        summary = RM.summarize(doc)
        status = summary.get('status') or 'empty'
        candidates = RM.suggest(doc, km_val, cps, fence=fence_norm, center=center)

        msg = ''
        if status == 'empty':
            msg = '还没有学习路线，请先从历史跑步记录学习'
        elif status == 'weak':
            usable = summary.get('usable', 0)
            details = summary.get('templates_detail') or []
            max_samples = max((t.get('samples', 0) for t in details), default=0)
            worst_dist = max((t.get('medoid_distance_m', 0.0) for t in details), default=0.0)
            if usable < 5:
                msg = f'可用跑步记录不足 5 条（当前 {usable} 条），暂不提供学习路线'
            elif max_samples < 3:
                msg = '单条路线重复次数不足 3 次，暂不提供学习路线'
            elif worst_dist > 60.0:
                msg = '历史路线聚合偏差过大，暂不提供学习路线'
            else:
                msg = '记录还不够，暂不提供学习路线'
        elif not candidates:
            msg = '当前围栏或必经点位条件下没有匹配的学习路线'

        data = {
            'candidates': candidates,
            'templates': summary.get('templates', 0),
            'target_m': round(km_val * 1000.0, 1),
            'status': status,
            'progress': self._rm_progress_for(owner),
            'cached': False,
        }
        if msg:
            data['message'] = msg
        if status == 'ok' and candidates:
            cache.put(cache_key, {
                key: value for key, value in data.items() if key != 'progress'
            })
        return _ok(data, message=msg or 'success')

    def real_route_memory_apply(self, template_id, km=2.5, points=None):
        from ..application import agent_cache
        from ..application import route_memory as RM

        acct, err = self._rm_require_account()
        if err is not None:
            return err

        try:
            km_val = float(km)
        except (TypeError, ValueError):
            km_val = 2.5
        if not math.isfinite(km_val):
            km_val = 2.5
        km_val = max(0.5, min(20.0, km_val))

        cps = self._rm_normalise_points(points)
        owner = f'{acct.uid}:{acct.unid}'
        cache_key = agent_cache.stable_hash({
            'owner': owner,
            'doc': agent_cache.file_signature(
                RM.memory_path(self.store_dir, acct.uid, acct.unid)),
            'template_id': str(template_id or ''),
            'km': round(km_val, 3),
            'points': cps,
        })
        cache = self._rm_apply_cache_obj()
        cached = cache.get(cache_key)
        if isinstance(cached, dict):
            out = dict(cached)
            out['cached'] = True
            return _ok(out)

        doc = RM.load(self.store_dir, acct.uid, acct.unid)
        res = RM.apply_template(doc, str(template_id or ''), km_val, points=cps)
        if not res.get('ok'):
            return _fail(res.get('error') or '模板应用失败')
        out = dict(res)
        out['cached'] = False
        cache.put(cache_key, {k: v for k, v in out.items() if k != 'cached'})
        return _ok(out)

    def real_route_memory_clear(self):
        from ..application import route_memory as RM

        acct, err = self._rm_require_account()
        if err is not None:
            return err
        RM.clear(self.store_dir, acct.uid, acct.unid)
        self._rm_cache_clear()
        self._rm_set_progress(**self._rm_default_progress())
        return _ok({'cleared': True})

    def real_route_memory_save(self, template_id):
        from ..application import route_memory as RM
        from ..domain import safety as S

        acct, err = self._rm_require_account()
        if err is not None:
            return err
        doc = RM.load(self.store_dir, acct.uid, acct.unid)
        tpl = next(
            (t for t in (doc.get('templates') or [])
             if isinstance(t, dict) and t.get('id') == template_id),
            None,
        )
        if tpl is None:
            return _fail('模板不存在')

        today_bj = S.fmt_beijing(S.now_ms(), '%Y-%m-%d')
        candidate = _clean_route({
            'name': tpl.get('name') or '学习路线',
            'type': 'learned',
            'line': tpl.get('line'),
            'closed': bool(tpl.get('closed')),
            'saved_at': today_bj,
        })
        if candidate is None:
            return _fail('模板不存在')

        raw = _read_json(self._routes_path(), {})
        rows = raw.get('routes') if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            rows = []

        out: list[dict] = []
        seen_keys: set[tuple] = set()
        for r in rows:
            c = _clean_route(r)
            if c is None:
                continue
            p0 = c['line'][0]
            key = (c['name'], round(p0[0], 6), round(p0[1], 6))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out.append(c)

        np0 = candidate['line'][0]
        new_key = (candidate['name'], round(np0[0], 6), round(np0[1], 6))
        if new_key in seen_keys:
            out = [
                candidate
                if (r['name'], round(r['line'][0][0], 6), round(r['line'][0][1], 6)) == new_key
                else r
                for r in out
            ]
        else:
            out.append(candidate)
        out = out[-300:]

        try:
            _write_json(self._routes_path(), {'version': 1, 'routes': out})
        except Exception as e:  # noqa: BLE001
            return _fail('路线保存失败: %s' % e)
        return _ok({'routes': out, 'count': len(out)})

    # -- route-memory agent ------------------------------------------------
    def _agent_owner(self) -> str:
        d = self._read_session()
        uid = str(d.get('uid') or '')
        unid = str(d.get('unid') or '')
        return f'{uid}:{unid}' if (uid or unid) else ''

    def _agent_build_tools(self) -> dict:
        def _llm_choose(candidates, target_m):
            try:
                from ..application import agent_cache, llm_client, llm_settings
            except Exception:  # noqa: BLE001
                return None
            settings = llm_settings.effective(self.store_dir)
            if settings is None:
                return None

            # Choosing a candidate is a classification task, not creative
            # writing.  Temperature 0 makes the same prompt reproducible and
            # lets the local cache return the same decision safely.
            settings = dict(settings)
            settings['temperature'] = 0.0
            model = str(settings.get('model') or '')
            messages = llm_settings.build_choice_prompt(candidates, target_m)
            cache_key = agent_cache.stable_hash({
                'protocol': settings.get('protocol'),
                'base_url': settings.get('base_url'),
                'model': model,
                'temperature': 0.0,
                'messages': messages,
            })

            cache = self._agent_choice_cache()
            hit = cache.get(cache_key)
            if isinstance(hit, dict):
                cached_id = str(hit.get('template_id') or '')
                if any(
                    isinstance(c, dict)
                    and str(c.get('template_id') or '') == cached_id
                    for c in (candidates or [])
                ):
                    return {
                        'template_id': cached_id,
                        'model': model,
                        'error': '',
                        'cached': True,
                        'prompt_sha256': cache_key,
                    }

            proxy = (os.environ.get('JHP_PROXY') or '').strip()
            proxies = {'http': proxy, 'https': proxy} if proxy else None
            res = llm_client.chat(settings, messages, proxies=proxies)
            if not isinstance(res, dict) or not res.get('ok'):
                err = (res.get('error') if isinstance(res, dict) else '') or 'LLM 调用失败'
                raise RuntimeError(err)
            picked = llm_settings.parse_choice(res.get('text') or '', candidates)
            if not picked:
                return {
                    'template_id': '',
                    'model': model,
                    'error': '模型返回无效候选序号',
                    'cached': False,
                    'prompt_sha256': cache_key,
                }
            cache.put(cache_key, picked, model)
            return {
                'template_id': picked,
                'model': model,
                'error': '',
                'cached': False,
                'prompt_sha256': cache_key,
            }

        return {
            'route_memory_status': self.real_route_memory_status,
            'route_memory_learn': self.real_route_memory_learn,
            'route_memory_suggest': self.real_route_memory_suggest,
            'route_memory_apply': self.real_route_memory_apply,
            'preview_route': self.real_preview_model,
            'audit_run': self.real_audit_run,
            'llm_choose': _llm_choose,
        }

    def _agent_get_or_create_session(self):
        from ..application.agent import AgentSession

        lock = getattr(self, '_agent_lock', None)
        if lock is None:
            lock = threading.RLock()
            self._agent_lock = lock
        with lock:
            owner = self._agent_owner()
            session = getattr(self, '_agent_session', None)
            if session is None or getattr(session, 'owner', None) != owner:
                if session is not None:
                    try:
                        session.cancel()
                    except Exception:  # noqa: BLE001
                        pass
                session = AgentSession(self._agent_build_tools(), self.store_dir, owner)
                self._agent_session = session
            return session

    def real_agent_start(self, text='', km=None, points=None, fence=None,
                         run_area_id=None):
        session = self._agent_get_or_create_session()
        return _ok(session.start(
            text=text,
            km=km,
            points=points,
            fence=fence,
            run_area_id=run_area_id,
        ))

    def _agent_current_session(self):
        """Return the session only while it still belongs to the active account."""
        lock = getattr(self, '_agent_lock', None)
        if lock is None:
            lock = threading.RLock()
            self._agent_lock = lock
        with lock:
            session = getattr(self, '_agent_session', None)
            if session is None:
                return None
            if getattr(session, 'owner', '') != self._agent_owner():
                try:
                    session.cancel()
                except Exception:  # noqa: BLE001
                    pass
                self._agent_session = None
                return None
            return session

    def real_agent_status(self):
        from ..application.agent import _idle_status

        session = self._agent_current_session()
        if session is None:
            return _ok(_idle_status())
        return _ok(session.status())

    def real_agent_message(self, text):
        session = self._agent_current_session()
        if session is None:
            return _fail('Agent 还没有开始')
        return _ok(session.message(text))

    def real_agent_confirm(self):
        session = self._agent_current_session()
        if session is None:
            return _fail('Agent 还没有开始')
        res = session.confirm()
        if not isinstance(res, dict) or res.get('ok') is False or 'preview_args' not in res:
            err = res.get('error') if isinstance(res, dict) else '当前状态不可确认'
            return _fail(err or '当前状态不可确认', data=res)
        return _ok(res)

    def real_agent_cancel(self):
        from ..application.agent import _idle_status

        session = self._agent_current_session()
        if session is None:
            return _ok(_idle_status())
        return _ok(session.cancel())



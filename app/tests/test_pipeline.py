"""Critical-path tests for the one-shot pipeline, with no network.

The pipeline is the only code that decides whether a record is submitted, so
these tests drive `pipeline.main()` with a fake client and check the decisions
it reaches — not the wire format (that is `validate_sim.py`).

Run:  python tests/test_pipeline.py
"""

import contextlib
import io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from replica.transport import netsec, obs
from replica import pipeline   # noqa: E402
from replica.domain import stadium   # noqa: E402

try:                                    # pytest is optional for the script runner
    import pytest
except ImportError:                     # pragma: no cover
    pytest = None

PASSED = []
FAILED = []


def check(name, cond, detail=''):
    (PASSED if cond else FAILED).append(name)
    print('  %-46s %s   %s' % (name, 'PASS' if cond else 'FAIL', detail))


def biz(error, data=None, message=''):
    return {'status': 200, 'raw_len': 10, 'plain': None, 'error': None,
            'json': {'error': error, 'message': message, 'data': data}}


class FakeClient:
    backend = 'netsec'
    proxy = None

    def __init__(self, policy=None, points=None, submit=None, fence=None,
                 records=None):
        self.policy = policy
        self.points = points
        self.submit = submit
        self.fence = fence
        self.records = records
        self.calls = []
        # points.build_body needs a netsec session to seal the request
        self._ns = netsec.EnvelopeSession()

    def request(self, acct, path, payload=None, method='POST', timeout=None,
                extra_headers=None, retries=0):
        self.calls.append(path)
        if 'runModePolicy' in path:
            if self.policy is None:
                return biz(401, message='Unauthorized')
            return biz(10000, data=self.policy)
        if 'distance' in path:
            if self.points is None:
                return biz(401, message='Unauthorized')
            return biz(10000, data={'pointsResModels': self.points})
        if 'runnings/records' in path:
            return biz(10000, data={'list': self.records or []})
        if 'save/record' in path:
            return self.submit or biz(10000, data={'rrid': 1333336472,
                                                   'uuid': 'U-1'})
        if 'get_one_record' in path:
            return biz(10000, data={'isCheat': 0, 'complete': True,
                                    'reasonList': []})
        if 'getGeoFenceForRun' in path:
            if not self.fence:
                return biz(10000, data={'geoFences': []})
            return biz(10000, data={'geoFences': [
                {'name': '学校', 'points': [{'glat': a, 'glon': b}
                                            for a, b in self.fence]}]})
        return biz(10000, data={})

    def submit_run(self, acct, run, ent, signed, policy_ts=None):
        """Mirror RealClient.submit_run's decision fields."""
        res = self.submit or biz(10000, data={'rrid': 1333336472,
                                              'uuid': 'U-1'})
        data = res.get('json') or {}
        payload = data.get('data') if isinstance(data.get('data'), dict) else {}
        return {'verdict': 'accepted' if data.get('error') == 10000 else 'x',
                'status': res.get('status'),
                'error': data.get('error'),
                'message': data.get('message'),
                'rrid': payload.get('rrid'),
                'uuid': payload.get('uuid'),
                'runes': '', 'runef': ''}


POLICY_OK = {'timestamp': 1792500000000, 'policy': 1,
             'runRuleModel': {'minDistance': 2000, 'validTime': 600},
             'mustPoints': [{'glat': 22.4109, 'glon': 112.9407}]}
POLICY_BAD = {'timestamp': 1, 'policy': 1, 'mustPoints': [{}, {'lat': 1}]}
POINTS_OK = [
    {'lat': 22.4115, 'lon': 112.9402, 'isMustPoint': True,
     'pointName': 'A', 'id': 11, 'isFixed': 1, 'position': 1},
    {'lat': 22.4111, 'lon': 112.9411, 'isMustPoint': False,
     'pointName': 'B', 'id': 12, 'isFixed': 0, 'position': 2},
    {'lat': 22.4101, 'lon': 112.9405, 'isMustPoint': False,
     'pointName': 'C', 'id': 13, 'isFixed': 0, 'position': 3},
]

#: Pin the backfill date.  "Today 09:00-22:00" is impossible to fit when the
#: suite runs between 00:00 and 09:00 (or after 22:00), which used to make the
#: pytest run fail while the non-asserting check() helper still reported green.
BASE_ARGV = ['--token', 'T', '--uid', '99999999', '--km', '2.0',
             '--pace', '6.0', '--no-preflight', '--no-history', '--no-limit',
             '--days-ago', '1']


def _ensure_data_dir():
    """Give every runner (script or pytest) its own data dir.

    The script runner sets these in ``main()``; pytest never calls ``main()``,
    so the first test that touches the store would otherwise write into the
    real ``data/`` directory.
    """
    tmp = os.environ.get('JHP_DATA_DIR')
    if not tmp:
        tmp = tempfile.mkdtemp(prefix='jhp_pipe_')
        os.environ['JHP_DATA_DIR'] = tmp
    os.environ.setdefault('JHP_POINTS_CACHE', os.path.join(tmp, 'points.json'))
    return tmp


_ensure_data_dir()


if pytest is not None:
    @pytest.fixture(autouse=True)
    def _isolated_pipeline_store(monkeypatch):
        """Make pytest behave like the script runner.

        The script resets receipts/limits and stubs OBS before every test;
        without this, pytest shares state and hits the real OBS client.  That
        was why the suite could report green while the script failed.
        """
        tmp = _ensure_data_dir()
        _reset_store(tmp)
        monkeypatch.setattr(obs, 'upload_obs', lambda *a, **k: {
            'uploaded': 2, 'verified': 2, 'of': 2,
            'keys': [], 'bytes': 1, 'errors': []})
        yield


def run(client, argv):
    """Run main() and capture (exit_code, stdout)."""
    out = io.StringIO()
    code = None
    try:
        with contextlib.redirect_stdout(out):
            code = pipeline.main(argv,
                                 client_factory=lambda proxy=None: client)
    except SystemExit as e:                       # do_forge raises SystemExit
        code = e.code if isinstance(e.code, int) else 1
    return code, out.getvalue()


def test_dry_run():
    c = FakeClient(policy=POLICY_OK, points=POINTS_OK)
    code, out = run(c, BASE_ARGV + ['--dry-run'])
    check('dry-run exits 0', code == 0, code)
    check('policy timestamp reached the log', 'timestamp=1792500000000' in out)
    check('minDistance from policy', 'minDistance=2000' in out)
    check('must-point count logged', '必经点=1' in out)
    check('address is the campus city', 'address=江门市' in out)
    check('policy value from server', 'policy=1' in out)
    check('route includes the policy must-point',
          '策略必经点 1 个' in out)


def test_must_points_malformed():
    c = FakeClient(policy=POLICY_BAD, points=POINTS_OK)
    code, out = run(c, BASE_ARGV + ['--dry-run'])
    check('malformed must-points abort', code == 1, code)
    check('abort message is explicit', '策略必经点无法解析' in out)

    c2 = FakeClient(policy=POLICY_BAD, points=POINTS_OK)
    code2, out2 = run(c2, BASE_ARGV + ['--dry-run', '--ignore-must-points'])
    check('--ignore-must-points continues', code2 == 0, code2)
    check('and says so', '已忽略策略必经点' in out2)


def test_dry_run_without_policy():
    """Token expired: policy and points both 401 -> plain loop, still valid."""
    c = FakeClient()
    code, out = run(c, BASE_ARGV + ['--dry-run'])
    check('dry-run survives no policy', code == 0, code)
    check('falls back to a plain loop', '使用本地环形路线' in out)
    check('selDistance falls back to distance', 'selDistance=2000' in out)


def test_submit_accepted():
    c = FakeClient(policy=POLICY_OK, points=POINTS_OK)
    code, out = run(c, BASE_ARGV)
    check('accepted submit exits 0', code == 0, code)
    check('rrid reported', '1333336472' in out)


def test_submit_rejected_then_retry_allowed():
    rejected = biz(10121, message='此账号已在其他设备登录')
    c = FakeClient(policy=POLICY_OK, points=POINTS_OK, submit=rejected)
    code, out = run(c, BASE_ARGV)
    check('rejection exits 2', code == 2, code)
    check('rejection is labelled 被拒', '提交被拒' in out)

    c2 = FakeClient(policy=POLICY_OK, points=POINTS_OK, submit=rejected)
    code2, out2 = run(c2, BASE_ARGV)
    check('rejected window allows retry', code2 == 2, code2)


def test_submit_unknown_locks_window():
    unknown = {'status': 200, 'raw_len': 0, 'plain': None,
               'error': 'decode failed', 'json': None}
    # Pin the clock: two random 09:00-22:00 starts can be disjoint, in which
    # case the guard correctly allows the second run and the test is flaky.
    argv = BASE_ARGV + ['--time', '10:30']
    c = FakeClient(policy=POLICY_OK, points=POINTS_OK, submit=unknown)
    code, out = run(c, argv)
    check('unknown outcome exits 2', code == 2, code)
    check('unknown is labelled 结果未知', '结果未知' in out)

    c2 = FakeClient(policy=POLICY_OK, points=POINTS_OK)
    code2, out2 = run(c2, argv)
    check('unknown outcome blocks the next run', code2 == 3, code2)
    check('block message mentions the receipt', '回执' in out2)


def test_stadium_fits_checkpoints():
    ring = stadium.stadium_route(22.410787, 112.940804, 400.0,
                                 bearing_deg=28.0, n_points=360)
    pts = [{'glat': ring[i][0], 'glon': ring[i][1], 'isMustPoint': i == 0,
            'pointName': 'P%d' % i, 'id': 100 + i, 'isFixed': 1 if i == 0 else 0,
            'position': i} for i in (0, 90, 180, 270)]
    c = FakeClient(policy=POLICY_OK, points=pts)
    code, out = run(c, BASE_ARGV + ['--dry-run', '--stadium', '400'])
    check('stadium fit exits 0', code == 0, code)
    check('fit message shown', '拟合跑道' in out)
    check('all real checkpoints kept', '经过全部 4 个点位' in out)


def test_stadium_rejects_wrong_size():
    # A 628 m circle cannot be a 400 m track: every point sits ~45 m off.
    circle = stadium.stadium_route(22.410787, 112.940804, 628.0, n_points=24)
    pts = [{'glat': circle[i][0], 'glon': circle[i][1], 'isMustPoint': False,
            'pointName': 'C%d' % i, 'id': 200 + i}
           for i in range(0, 24, 4)]
    c = FakeClient(policy=POLICY_OK, points=pts)
    code, out = run(c, BASE_ARGV + ['--dry-run', '--stadium', '400'])
    check('wrong-size preset aborts', code == 1, code)
    check('abort says 不匹配', '不匹配' in out)


def test_local_gate_daily_cap():
    """The gate runs before the receipt guard and is fail-closed."""
    # --allow-night: this case is about the daily cap. Without it the test
    # depends on the wall clock and fails whenever the suite runs after 23:00.
    argv = [a for a in BASE_ARGV if a != '--no-limit'] + [
        '--max-per-day', '1', '--allow-night']
    c = FakeClient(policy=POLICY_OK, points=POINTS_OK)
    code, out = run(c, argv)
    check('first run of the day passes the gate', code == 0, code)

    c2 = FakeClient(policy=POLICY_OK, points=POINTS_OK)
    code2, out2 = run(c2, argv)
    check('second run blocked by daily cap', code2 == 4, code2)
    check('gate message names the override', '--no-limit' in out2)

    c3 = FakeClient(policy=POLICY_OK, points=POINTS_OK)
    code3, out3 = run(c3, argv + ['--no-limit'])
    check('--no-limit bypasses the cap', code3 in (0, 3), code3)


def test_backfill_time():
    """--days-ago/--time stamps the record in the chosen 09:00-22:00 slot."""
    from replica.domain import safety as S

    y = S.now_ms() - 24 * 3600 * 1000
    ydate = S.to_beijing(y).strftime('%Y-%m-%d')
    ymd = S.to_beijing(y).strftime('%m-%d')

    c = FakeClient(policy=POLICY_OK, points=POINTS_OK)
    code, out = run(c, BASE_ARGV + ['--dry-run', '--days-ago', '1',
                                    '--time', '10:30'])
    check('backfill dry-run exits 0', code == 0, code)
    check('backfill logs the chosen slot', '[time]' in out and ydate in out, out[-120:])
    check('backfill record starts yesterday 10:30', (ymd + ' 10:30') in out, ymd)

    c2 = FakeClient(policy=POLICY_OK, points=POINTS_OK)
    code2, out2 = run(c2, BASE_ARGV + ['--dry-run', '--time', '08:00'])
    check('08:00 is rejected', code2 == 1, code2)
    check('rejection explains the 09:00 window', '09:00–22:00' in out2)

    c3 = FakeClient(policy=POLICY_OK, points=POINTS_OK)
    code3, out3 = run(c3, BASE_ARGV + ['--dry-run', '--days-ago', '4'])
    check('4 days ago is rejected', code3 == 1, code3)
    check('rejection explains the 3-day range', '3 天前' in out3)

    c4 = FakeClient(policy=POLICY_OK, points=POINTS_OK)
    code4, out4 = run(c4, BASE_ARGV + ['--dry-run', '--days-ago', '3',
                                       '--time', '21:31', '--km', '5',
                                       '--pace', '6'])
    check('run ending after 22:00 is rejected', code4 == 1, code4)
    check('rejection names 22:00', '22:00' in out4)


def test_backfill_daily_reminder_from_server():
    """A record already on the selected date produces a reminder, not a block."""
    tmp = _ensure_data_dir()
    rules = {
        'min_km': 2.0, 'max_km': 5.0, 'pace_min': 3.0, 'pace_max': 10.0,
        'min_minutes': 10, 'max_minutes': 90, 'max_per_day': 1,
        'window_start': '06:30', 'window_end': '22:00',
    }
    path = os.path.join(tmp, 'school_rules.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(rules, f)
    from replica.domain import safety as S
    y_ms = S.now_ms() - 24 * 3600 * 1000
    try:
        c = FakeClient(policy=POLICY_OK, points=POINTS_OK, records=[
            {'rrid': 1, 'startTime': y_ms, 'totalDis': 2500,
             'totalTime': 900, 'totalSteps': 2580, 'complete': True}])
        code, out = run(c, BASE_ARGV + ['--dry-run', '--days-ago', '1',
                                        '--time', '10:30'])
        check('server daily count is only a reminder', code == 0, code)
        check('reminder names 当日次数', '当日次数' in out, out[-160:])
        check('reminder says 仅提醒', '仅提醒' in out)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _write_route(tmpdir, pts, name='r.json'):
    path = os.path.join(tmpdir, name)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(pts, f)
    return path


def test_route_file_without_checkpoints():
    """A drawing that misses the campus points must be refused."""
    tmp = _ensure_data_dir()
    lat0, lon0 = 22.4046, 112.9342
    # three tight checkpoints; a 60 m circle around them never comes within 40 m
    tight = [{'glat': lat0 + 0.00005 * i, 'glon': lon0 + 0.00003 * i,
              'isMustPoint': i == 0, 'pointName': 'P%d' % i,
              'id': 300 + i, 'isFixed': 1 if i == 0 else 0, 'position': i}
             for i in range(3)]
    import math as _m
    r_lat = 60.0 / 110540.0
    r_lng = 60.0 / (111320.0 * _m.cos(_m.radians(lat0)))
    circle = [[lat0 + r_lat * _m.sin(2 * _m.pi * k / 36),
               lon0 + r_lng * _m.cos(2 * _m.pi * k / 36)] for k in range(36)]
    route = _write_route(tmp, circle)

    c = FakeClient(policy=POLICY_OK, points=tight)
    code, out = run(c, BASE_ARGV + ['--dry-run', '--route-file', route,
                                    '--smooth', '0'])
    check('route missing points aborts', code == 1, code)
    check('message counts the misses', '没经过' in out)
    check('message names --snap-route', '--snap-route' in out)

    c2 = FakeClient(policy=POLICY_OK, points=tight)
    code2, out2 = run(c2, BASE_ARGV + ['--dry-run', '--route-file', route,
                                       '--smooth', '0', '--snap-route'])
    check('--snap-route repairs the route', code2 == 0, code2)
    check('snap is logged', '插入路线' in out2)


def test_route_file_passing_all_points():
    tmp = _ensure_data_dir()
    # a ~100 m square: long enough to be a real loop, with every corner a
    # checkpoint (a 2-point back-and-forth is rejected as degenerate now)
    lat0, lon0 = 22.4046, 112.9342
    sq = [(lat0, lon0), (lat0 + 0.0009, lon0),
          (lat0 + 0.0009, lon0 + 0.0009), (lat0, lon0 + 0.0009)]
    pts = [{'glat': a, 'glon': b, 'isMustPoint': i == 0,
            'pointName': 'Q%d' % i, 'id': 500 + i,
            'isFixed': 1 if i == 0 else 0, 'position': i}
           for i, (a, b) in enumerate(sq)]
    route = _write_route(tmp, [list(p) for p in sq])
    c = FakeClient(policy=POLICY_OK, points=pts, fence=[
        (lat0 - 0.0015, lon0 - 0.0015), (lat0 - 0.0015, lon0 + 0.0025),
        (lat0 + 0.0025, lon0 + 0.0025), (lat0 + 0.0025, lon0 - 0.0015)])
    code, out = run(c, BASE_ARGV + ['--dry-run', '--route-file', route,
                                    '--route-close'])
    check('route through points passes', code == 0, code)
    check('reports full coverage', '经过全部 4 个点位' in out)


def test_geofence_warning():
    """Points outside the campus fence are reported, not silently accepted."""
    lat0, lon0 = 22.4046, 112.9342
    h = 0.00045                              # 100 m square half-side

    def square(la, lo, half=h):
        return [[la - half, lo - half], [la - half, lo + half],
                [la + half, lo + half], [la + half, lo - half]]

    sq = square(lat0, lon0)
    # checkpoints sit on three corners, so the route really does pass them
    pts = [{'glat': la, 'glon': lo, 'isMustPoint': i == 0,
            'pointName': 'P%d' % i, 'id': 400 + i,
            'isFixed': 1 if i == 0 else 0, 'position': i}
           for i, (la, lo) in enumerate(sq[:3])]
    tmp = _ensure_data_dir()
    route = _write_route(tmp, sq, 'sq.json')

    # tight fence: the square's corners stick out of it
    tight = [(lat0 - 0.0003, lon0 - 0.0003), (lat0 - 0.0003, lon0 + 0.0003),
             (lat0 + 0.0003, lon0 + 0.0003), (lat0 + 0.0003, lon0 - 0.0003)]
    c = FakeClient(policy=POLICY_OK, points=pts, fence=tight)
    code, out = run(c, BASE_ARGV + ['--dry-run', '--route-file', route,
                                    '--smooth', '0'])
    check('outside-fence route still runs (warning only)', code == 0, code)
    check('fence violation reported', '在围栏外' in out)
    check('fence check mentions the farthest point', '最远' in out)

    # generous fence: same route, fully inside
    wide = [(lat0 - 0.002, lon0 - 0.002), (lat0 - 0.002, lon0 + 0.002),
            (lat0 + 0.002, lon0 + 0.002), (lat0 + 0.002, lon0 - 0.002)]
    c2 = FakeClient(policy=POLICY_OK, points=pts, fence=wide)
    code2, out2 = run(c2, BASE_ARGV + ['--dry-run', '--route-file', route,
                                       '--smooth', '0'])
    check('inside route passes the fence check', code2 == 0, code2)
    check('fence check reports full containment', '全部在围栏内' in out2)


def main():
    tmp = tempfile.mkdtemp(prefix='jhp_pipe_')
    os.environ['JHP_DATA_DIR'] = tmp
    os.environ['JHP_POINTS_CACHE'] = os.path.join(tmp, 'points.json')
    real_upload = obs.upload_obs
    obs.upload_obs = lambda *a, **k: {'uploaded': 2, 'verified': 2, 'of': 2,
                                      'keys': [], 'bytes': 1, 'errors': []}
    try:
        # a fresh receipt store per test so windows do not bleed across cases
        for fn in (test_dry_run, test_must_points_malformed,
                   test_dry_run_without_policy, test_submit_accepted,
                   test_submit_rejected_then_retry_allowed,
                   test_submit_unknown_locks_window,
                   test_stadium_fits_checkpoints,
                   test_stadium_rejects_wrong_size,
                   test_route_file_without_checkpoints,
                   test_route_file_passing_all_points,
                   test_geofence_warning,
                   test_backfill_time,
                   test_backfill_daily_reminder_from_server,
                   test_local_gate_daily_cap):
            print('\n=== %s ===' % fn.__name__)
            _reset_store(tmp)
            fn()
    finally:
        obs.upload_obs = real_upload
        os.environ.pop('JHP_DATA_DIR', None)
        os.environ.pop('JHP_POINTS_CACHE', None)

    total = len(PASSED) + len(FAILED)
    print('\n' + '=' * 56)
    print('  %d passed, %d failed (of %d)' % (len(PASSED), len(FAILED), total))
    if FAILED:
        print('  FAILED: %s' % ', '.join(FAILED))
    print('RESULT:', 'PASS' if not FAILED else 'FAIL')
    return 0 if not FAILED else 1


def _reset_store(tmp):
    import shutil
    for sub in ('submission_receipts', 'limits'):
        path = os.path.join(tmp, sub)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    # the checkpoint cache is keyed by campus, so it would leak between tests
    cache = os.environ.get('JHP_POINTS_CACHE')
    if cache and os.path.exists(cache):
        try:
            os.remove(cache)
        except OSError:
            pass


def test_no_pipeline_failures():
    """Turn the module's PASSED/FAILED ledger into a real pytest assertion."""
    assert not FAILED, 'pipeline checks failed: %s' % ', '.join(FAILED)


if __name__ == '__main__':
    sys.exit(main())

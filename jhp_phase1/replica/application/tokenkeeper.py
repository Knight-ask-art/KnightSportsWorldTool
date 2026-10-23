"""Token keep-alive for the real session.

The official API has no documented refresh-token endpoint.  There are only
two things this module can prove:

1. a cheap authenticated request (``GET user/info``) still works -> the token
   is alive; update ``last_ok_at``;
2. a fresh password login returns a new token -> the only reliable renewal.
   It is opt-in, uses credentials supplied at runtime (never written to the
   session file), and is skipped for accounts marked ``disabled``.

So "keep-alive" here is: periodic probe + early expiry detection + optional
re-login.  It does **not** pretend to extend a fixed server-side TTL; if the
server expires tokens regardless of activity, only re-login helps.

Security rules:
* never write a password to disk;
* never include a token/password/uid in a status payload or exception text;
* a ``disabled`` session is never probed or re-logged-in.

The module is deliberately dependency-free (stdlib only) and accepts injected
``probe``/``relogin`` callables, so the test suite never touches the network.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time

DEFAULT_INTERVAL_S = 6 * 3600
MIN_INTERVAL_S = 60
MAX_INTERVAL_S = 7 * 24 * 3600

_DEFAULTS = {
    'enabled': False,
    'interval_s': DEFAULT_INTERVAL_S,
    'auto_relogin': False,
    'last_check_at': 0,
    'last_ok_at': 0,
    'next_check_at': 0,
    'fail_count': 0,
    'last_error': '',
    'token_updated_at': 0,
    'token_expired': False,
}


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: str, data: dict) -> None:
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


def _clamp_interval(value) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = DEFAULT_INTERVAL_S
    return max(MIN_INTERVAL_S, min(MAX_INTERVAL_S, value))


def ensure_state(session: dict) -> dict:
    """Return the mutable ``keepalive`` dict, filling missing fields."""
    state = session.get('keepalive')
    if not isinstance(state, dict):
        state = {}
        session['keepalive'] = state
    for key, value in _DEFAULTS.items():
        state.setdefault(key, value)
    state['interval_s'] = _clamp_interval(state.get('interval_s'))
    for key in ('last_check_at', 'last_ok_at', 'next_check_at', 'fail_count',
                'token_updated_at'):
        try:
            state[key] = max(0, int(state.get(key) or 0))
        except (TypeError, ValueError):
            state[key] = 0
    state['enabled'] = bool(state.get('enabled'))
    state['auto_relogin'] = bool(state.get('auto_relogin'))
    state['token_expired'] = bool(state.get('token_expired'))
    state['last_error'] = str(state.get('last_error') or '')[:300]
    return state


def safe_error(text) -> str:
    """Error text that is safe to store/display (no token/password)."""
    text = str(text or '')
    try:
        from ..domain.safety import redact_text
        text = redact_text(text)
    except Exception:  # noqa: BLE001 - a missing masker must not break keepalive
        pass
    return text.replace('\n', ' ')[:300]


def _token_from_payload(payload) -> str:
    """Extract a rotated token if the server ever includes one."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return ''
    if not isinstance(payload, dict):
        return ''
    for key in ('token', 'newToken', 'new_token', 'refreshToken',
                'refresh_token', 'accessToken', 'access_token'):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ''


def probe_real_session(session: dict, proxy: str | None = None,
                       client_factory=None) -> dict:
    """Default real-server probe: ``GET api/v70100/user/info``.

    Returns a small dict (never the raw response):
      ``ok``           token accepted
      ``auth_failed``  server explicitly rejected the session (401/403 or
                       business error other than 10000)
      ``status``       HTTP status, if known
      ``message``      safe one-line description
      ``new_token``    only if the server returned a rotated token
    """
    if session.get('disabled'):
        return {'ok': False, 'auth_failed': False, 'message': '账号已禁用'}
    token = session.get('token') or ''
    if not token:
        return {'ok': False, 'auth_failed': False, 'message': '未配置 token'}

    from ..transport.realclient import Account, RealClient

    try:
        acct = Account(
            name=session.get('name', 'emu'),
            uid=str(session.get('uid') or ''),
            token=token,
            schoolName=session.get('schoolName', '广州软件学院'),
            campusName=session.get('campusName', '江门校区'),
            unid=int(session.get('unid') or 47203),
        )
        if session.get('DeviceId'):
            acct.DeviceId = session['DeviceId']
        if session.get('lat') and session.get('lon'):
            acct.center_lat = float(session['lat'])
            acct.center_lng = float(session['lon'])
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'auth_failed': False,
                'message': '会话字段无效: %s' % e}

    try:
        client = client_factory() if client_factory else RealClient(proxy=proxy)
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'auth_failed': False,
                'message': '客户端初始化失败: %s' % e}

    try:
        res = client.request(acct, 'api/v70100/user/info', method='GET',
                             retries=1)
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'auth_failed': False,
                'message': '网络错误: %s' % e}

    status = res.get('status')
    if status in (401, 403):
        return {'ok': False, 'auth_failed': True, 'status': status,
                'message': '服务端拒绝: HTTP %s' % status}
    # A 5xx (or a missing status) is a server/transport failure, not an
    # authentication verdict.  Marking it auth_failed would tell the keeper
    # the token is dead and trigger an unnecessary re-login.
    if status is None or (isinstance(status, int) and status >= 500):
        return {'ok': False, 'auth_failed': False, 'status': status,
                'message': '服务端异常: HTTP %s' % status}
    if res.get('error'):
        return {'ok': False, 'auth_failed': False, 'status': status,
                'message': '解密失败: %s' % res.get('error')}

    obj = res.get('json')
    if not isinstance(obj, dict) or not obj:
        return {'ok': False, 'auth_failed': False, 'status': status,
                'message': '响应为空或无法解析'}

    err = obj.get('error')
    if err in (401, 403, 10121):
        return {'ok': False, 'auth_failed': True, 'status': status,
                'message': '服务端拒绝: error=%s %s'
                           % (err, obj.get('message') or '')}
    if obj.get('status') is False:
        return {'ok': False, 'auth_failed': True, 'status': status,
                'message': '服务端拒绝: %s' % (obj.get('message') or 'status=false')}
    if err not in (None, 10000):
        return {'ok': False, 'auth_failed': False, 'status': status,
                'message': '服务端业务错误: error=%s %s'
                           % (err, obj.get('message') or '')}

    data = obj.get('data')
    new_token = _token_from_payload(data)
    if not new_token and isinstance(data, dict):
        # Some builds nest the profile under data; only look one level.
        new_token = _token_from_payload(data.get('session'))
    return {'ok': True, 'auth_failed': False, 'status': status,
            'message': 'accepted', 'new_token': new_token or ''}


def relogin_real(username: str, password: str) -> dict:
    """Default re-login hook.  Calls the same pure-protocol login flow."""
    from .login_flow import login as do_login
    sess = do_login(username, password, log=lambda *_: None)
    if not sess.get('token'):
        raise RuntimeError('登录响应没有 token')
    return {
        'uid': str(sess.get('uid') or ''),
        'token': sess['token'],
        'unid': sess.get('unid'),
        'profile': sess.get('profile') or {},
    }


class TokenKeeper:
    """Periodic token probe with optional in-memory-only re-login."""

    def __init__(self, session_path: str, probe, relogin=None,
                 interval_s: int = DEFAULT_INTERVAL_S,
                 auto_relogin: bool = False, username: str | None = None,
                 password: str | None = None, now=None):
        self.session_path = str(session_path)
        self.probe = probe
        self.relogin = relogin
        self.interval_s = _clamp_interval(interval_s)
        self.auto_relogin = bool(auto_relogin)
        self._username = username or ''
        self._password = password or ''
        self._now = now or time.time
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- state -----------------------------------------------------------
    def _load(self) -> dict:
        return _read_json(self.session_path)

    def _save(self, session: dict) -> None:
        _write_json(self.session_path, session)

    def _status_locked(self, session: dict | None = None,
                       now: int | None = None) -> dict:
        session = session if session is not None else self._load()
        state = ensure_state(session)
        now = int(self._now()) if now is None else now
        has_creds = bool(self._username and self._password)
        auto_relogin = bool(state['auto_relogin'] or self.auto_relogin)
        return {
            'enabled': bool(state['enabled']),
            # A thread can outlive `stop()` for a moment; it is not a running
            # keeper unless the persisted switch is still on.
            'running': bool(state['enabled'] and self._thread
                            and self._thread.is_alive()),
            'interval_s': int(state['interval_s']),
            'auto_relogin': auto_relogin,
            'can_auto_relogin': bool(auto_relogin and has_creds
                                     and not session.get('disabled')),
            'last_check_at': int(state['last_check_at']),
            'last_ok_at': int(state['last_ok_at']),
            'next_check_at': int(state['next_check_at']),
            'fail_count': int(state['fail_count']),
            'last_error': str(state['last_error']),
            'token_present': bool(session.get('token')),
            'token_expired': bool(state['token_expired']),
            'disabled': bool(session.get('disabled')),
            'token_updated_at': int(state['token_updated_at']),
            'checked_at': now,
        }

    def status(self) -> dict:
        with self._lock:
            return self._status_locked()

    def configure(self, interval_s: int | None = None,
                  auto_relogin: bool | None = None,
                  username: str | None = None,
                  password: str | None = None) -> dict:
        with self._lock:
            if interval_s is not None:
                self.interval_s = _clamp_interval(interval_s)
            if auto_relogin is not None:
                self.auto_relogin = bool(auto_relogin)
            if username is not None:
                self._username = str(username or '')
            if password is not None:
                self._password = str(password or '')
            session = self._load()
            state = ensure_state(session)
            state['interval_s'] = self.interval_s
            state['auto_relogin'] = self.auto_relogin
            self._save(session)
            return self._status_locked(session)

    def _can_relogin(self) -> bool:
        return bool(self.relogin and self._username and self._password)

    def _result(self, ok: bool, action: str, message: str, session: dict,
                now: int, **extra) -> dict:
        result = {'ok': bool(ok), 'action': action, 'message': message,
                  'status': self._status_locked(session, now)}
        result.update(extra)
        return result

    def run_once(self, force: bool = False) -> dict:
        """Probe once.  Safe to call from a thread or a CLI."""
        with self._lock:
            session = self._load()
            state = ensure_state(session)
            if self.auto_relogin:
                state['auto_relogin'] = True
            now = int(self._now())

            if session.get('disabled'):
                return self._result(False, 'skipped', '账号已禁用，跳过保活',
                                    session, now)
            if not session.get('token'):
                return self._result(False, 'skipped', '未配置 token',
                                    session, now)
            if (state['token_expired'] and not force
                    and not (state['auto_relogin'] and self._can_relogin())):
                return self._result(False, 'expired',
                                    'token 已标记失效，等待重新登录',
                                    session, now)

            try:
                probe = self.probe(session) or {}
            except Exception as e:  # noqa: BLE001
                probe = {'ok': False, 'auth_failed': False,
                         'message': '探测异常: %s' % e}
            if not isinstance(probe, dict):
                probe = {'ok': False, 'auth_failed': False,
                         'message': '探测返回值无效'}

            if probe.get('ok'):
                state['last_check_at'] = now
                state['last_ok_at'] = now
                state['fail_count'] = 0
                state['last_error'] = ''
                state['token_expired'] = False
                new_token = probe.get('new_token')
                if isinstance(new_token, str) and new_token \
                        and new_token != session.get('token'):
                    session['token'] = new_token
                    state['token_updated_at'] = now
                state['next_check_at'] = now + int(state['interval_s'])
                self._save(session)
                return self._result(True, 'probe', 'token 有效', session, now)

            auth_failed = bool(probe.get('auth_failed'))
            state['last_check_at'] = now
            state['fail_count'] = int(state['fail_count']) + 1
            state['last_error'] = safe_error(probe.get('message')
                                             or '探测失败')
            if auth_failed:
                state['token_expired'] = True
                state['next_check_at'] = now + int(state['interval_s'])
            else:
                # Network/decrypt hiccups are retried sooner than the normal
                # interval, but they must NOT mark the token dead.
                state['next_check_at'] = now + min(300,
                                                   int(state['interval_s']))
            self._save(session)

            if auth_failed and state['auto_relogin'] and self._can_relogin():
                return self._relogin_locked(session, state, now,
                                            reason=state['last_error'])
            return self._result(
                False, 'probe', state['last_error'], session, now,
                auth_failed=auth_failed)

    def _relogin_locked(self, session: dict, state: dict, now: int,
                        reason: str = '') -> dict:
        try:
            fresh = self.relogin(self._username, self._password) or {}
        except Exception as e:  # noqa: BLE001
            state['last_error'] = safe_error('自动重登失败: %s' % e)
            state['next_check_at'] = now + int(state['interval_s'])
            self._save(session)
            return self._result(False, 'relogin', state['last_error'],
                                session, now, auth_failed=True)
        token = fresh.get('token') if isinstance(fresh, dict) else ''
        if not isinstance(token, str) or not token:
            state['last_error'] = '自动重登失败: 响应没有 token'
            state['next_check_at'] = now + int(state['interval_s'])
            self._save(session)
            return self._result(False, 'relogin', state['last_error'],
                                session, now, auth_failed=True)
        session['token'] = token
        if fresh.get('uid'):
            session['uid'] = str(fresh['uid'])
        profile = fresh.get('profile') or {}
        if isinstance(profile, dict):
            for src, dst in (('name', 'name'), ('campusName', 'campusName'),
                             ('schoolName', 'schoolName')):
                if profile.get(src):
                    session[dst] = profile[src]
        if fresh.get('unid'):
            session['unid'] = fresh['unid']
        state['token_updated_at'] = now
        state['token_expired'] = False
        state['last_check_at'] = now
        state['last_ok_at'] = now
        state['fail_count'] = 0
        state['last_error'] = ''
        state['next_check_at'] = now + int(state['interval_s'])
        self._save(session)
        return self._result(True, 'relogin', '已自动重登并更新 token',
                            session, now, previous_error=safe_error(reason))

    # -- background loop --------------------------------------------------
    def start(self) -> dict:
        with self._lock:
            session = self._load()
            state = ensure_state(session)
            now = int(self._now())
            if session.get('disabled'):
                return self._result(False, 'skipped', '账号已禁用，不能开启保活',
                                    session, now)
            if not session.get('token'):
                return self._result(False, 'skipped', '未配置 token',
                                    session, now)
            if self._thread and self._thread.is_alive():
                return self._result(False, 'starting',
                                    '上一次保活线程仍在退出，请稍后重试',
                                    session, now)
            state['enabled'] = True
            state['interval_s'] = self.interval_s
            state['auto_relogin'] = self.auto_relogin
            if not state['next_check_at']:
                state['next_check_at'] = now + self.interval_s
            self._save(session)
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name='jhp-token-keeper', daemon=True)
            self._thread.start()
            return self._result(True, 'started', 'token 保活已开启', session, now)

    def _loop(self) -> None:
        while not self._stop.is_set():
            result = None
            try:
                result = self.run_once()
            except Exception as e:  # noqa: BLE001
                # Persist a safe error if possible; never kill the thread.
                try:
                    with self._lock:
                        session = self._load()
                        state = ensure_state(session)
                        state['last_error'] = safe_error('保活循环异常: %s' % e)
                        state['next_check_at'] = int(self._now()) + 300
                        self._save(session)
                except Exception:  # noqa: BLE001
                    pass
            # Honor a short retry after a transient failure, but never wait
            # longer than the configured interval.  A skipped result (no
            # token/disabled) has no next_check_at; without this branch the
            # loop would spin at 1 s forever.
            action = (result or {}).get('action')
            status = (result or {}).get('status') or {}
            next_at = int(status.get('next_check_at') or 0)
            if action == 'skipped':
                delay = self.interval_s
            elif next_at:
                delay = max(1, min(self.interval_s,
                                   next_at - int(self._now())))
            else:
                delay = self.interval_s
            if self._stop.wait(delay):
                break

    def stop(self, clear_credentials: bool = True) -> dict:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)
        with self._lock:
            session = self._load()
            state = ensure_state(session)
            state['enabled'] = False
            state['next_check_at'] = 0
            self._save(session)
            if clear_credentials:
                self._username = ''
                self._password = ''
            # Do not drop the reference while the old thread is still alive:
            # a quick stop/start would otherwise create two loops.
            self._thread = thread if thread and thread.is_alive() else None
            return self._result(True, 'stopped', 'token 保活已停止',
                                session, int(self._now()))

    def run_forever(self) -> dict:
        """Blocking loop for the CLI; Ctrl+C returns the last status."""
        last = {'ok': False, 'action': 'skipped', 'message': '未运行'}
        try:
            while True:
                last = self.run_once()
                print(json.dumps(last, ensure_ascii=False, indent=1), flush=True)
                if self._stop.wait(self.interval_s):
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self.stop(clear_credentials=False)
        return last

    def __repr__(self) -> str:
        return ('TokenKeeper(session=%r, interval_s=%d, auto_relogin=%s)'
                % (self.session_path, self.interval_s, self.auto_relogin))


# --------------------------------------------------------------------------
# self test (offline)
# --------------------------------------------------------------------------

def self_test() -> list[tuple[str, bool, str]]:
    import tempfile

    out = []
    now = 1_800_000_000
    with tempfile.TemporaryDirectory(prefix='jhp_keeper_') as tmp:
        path = os.path.join(tmp, 'accounts_emu.json')
        _write_json(path, {'uid': 'u-test', 'token': 'tok-old',
                           'keepalive': {}})

        calls = {'probe': 0, 'relogin': 0}

        def probe(session):
            calls['probe'] += 1
            return {'ok': True, 'auth_failed': False, 'message': 'accepted'}

        def relogin(user, password):
            calls['relogin'] += 1
            return {'uid': 'u-test', 'token': 'tok-new',
                    'profile': {'name': 'test'}}

        k = TokenKeeper(path, probe=probe, relogin=relogin, interval_s=3600,
                        now=lambda: now)
        st = k.status()
        out.append(('initial probe count 0', calls['probe'] == 0, '0'))
        out.append(('token present status', st['token_present'] is True, 'ok'))
        out.append(('initial not expired', st['token_expired'] is False, 'ok'))

        r = k.run_once()
        session = _read_json(path)
        out.append(('probe accepted', r['ok'] and r['action'] == 'probe', 'ok'))
        out.append(('last_ok updated', session['keepalive']['last_ok_at'] == now, 'ok'))
        out.append(('no relogin on ok', calls['relogin'] == 0, '0'))

        def dead(session):
            calls['probe'] += 1
            return {'ok': False, 'auth_failed': True, 'status': 401,
                    'message': '服务端拒绝: error=401'}

        k2 = TokenKeeper(path, probe=dead, relogin=relogin, interval_s=3600,
                         now=lambda: now + 10)
        r = k2.run_once()
        session = _read_json(path)
        out.append(('401 marks expired', r['ok'] is False
                    and session['keepalive']['token_expired'] is True, 'ok'))
        out.append(('401 increments fail', session['keepalive']['fail_count'] == 1, '1'))
        out.append(('no auto relogin without opt-in', calls['relogin'] == 0, '0'))

        secret = 'S3cret-keepalive-pass'
        k3 = TokenKeeper(path, probe=dead, relogin=relogin, interval_s=3600,
                         auto_relogin=True, username='u', password=secret,
                         now=lambda: now + 20)
        r = k3.run_once(force=True)
        session = _read_json(path)
        out.append(('auto relogin action', r['ok'] and r['action'] == 'relogin', 'ok'))
        out.append(('new token stored', session['token'] == 'tok-new', 'ok'))
        out.append(('expired cleared', session['keepalive']['token_expired'] is False, 'ok'))
        out.append(('password not stored', secret not in json.dumps(session), 'ok'))
        out.append(('status hides token', 'tok-' not in json.dumps(k3.status()), 'ok'))

        _write_json(path, {'uid': 'u-test', 'token': 'tok-old',
                           'disabled': True, 'keepalive': {}})
        k4 = TokenKeeper(path, probe=probe, relogin=relogin, now=lambda: now)
        before = calls['probe']
        r = k4.run_once()
        out.append(('disabled skipped', r['action'] == 'skipped'
                    and calls['probe'] == before, 'skipped'))
        out.append(('disabled status', k4.status()['disabled'] is True, 'ok'))
        out.append(('disabled cannot start',
                    k4.start()['status']['enabled'] is False, 'disabled'))

        _write_json(path, {'uid': 'u-test', 'token': '', 'keepalive': {}})
        k5 = TokenKeeper(path, probe=probe, relogin=relogin, now=lambda: now)
        out.append(('missing token skipped',
                    k5.run_once()['action'] == 'skipped', 'skipped'))

        # ---- probe classification (no network) --------------------------
        session = {'uid': 'u-test', 'token': 'tok-old'}

        class _ProbeClient:
            def __init__(self, res):
                self.res = res

            def request(self, *a, **k):
                return self.res

        def classify(res):
            return probe_real_session(
                session, client_factory=lambda: _ProbeClient(res))

        r = classify({'status': 200, 'json': {'status': False,
                                              'message': '未登录'},
                      'plain': '', 'error': None})
        out.append(('probe status:false is auth failure',
                    r['ok'] is False and r['auth_failed'] is True, r))
        r = classify({'status': 500, 'json': {'error': 500},
                      'plain': '{"error":500}', 'error': None})
        out.append(('probe 5xx is not auth failure',
                    r['ok'] is False and r['auth_failed'] is False, r))
        r = classify({'status': 200, 'json': None, 'plain': None,
                      'error': 'decode failed'})
        out.append(('probe decode error is not auth failure',
                    r['ok'] is False and r['auth_failed'] is False, r))
        r = classify({'status': 200, 'json': {'error': 10000,
                                              'data': {'uid': 'u-test'}},
                      'plain': '{}', 'error': None})
        out.append(('probe accepted', r['ok'] is True, r))
        r = classify({'status': 200, 'json': {'error': 10121,
                                              'message': 'other device'},
                      'plain': '{}', 'error': None})
        out.append(('probe 10121 is auth failure',
                    r['ok'] is False and r['auth_failed'] is True, r))

    return out

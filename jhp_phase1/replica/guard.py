"""Write-ahead submission receipts.

Behaviour ported from YanamiNeko/NekoSportsWorldTool `src/api/submission_guard.rs`
plus `docs/violation-checks.md`.  Rationale: a lost response must never cause a
second upload of the same run, and a half-known state must block new uploads
until it is reconciled read-only.

Guarantees
----------
1. The receipt is written AND fsync'ed BEFORE the network call goes out.
2. Overlapping [start,stop] windows for the same account+school are refused once
   a previous submission was accepted (or is in an unknown state).
3. A lock file serialises GUI/CLI processes sharing the data directory.
4. Receipts never contain tokens, passwords or coordinates.

States
------
    pending    written, request in flight (or crashed mid-flight)
    accepted   server returned 10000 and a record id
    rejected   server explicitly refused -> the user may retry manually
    unknown    transport/parse failure -> must be reconciled read-only
"""

from __future__ import annotations

import contextlib
import json
import os
import time

try:                     # Windows
    import msvcrt
    _HAVE_MSVCRT = True
except Exception:        # pragma: no cover
    _HAVE_MSVCRT = False

try:                     # POSIX
    import fcntl
    _HAVE_FCNTL = True
except Exception:        # pragma: no cover
    _HAVE_FCNTL = False


class GuardError(RuntimeError):
    pass


class AlreadySubmitted(GuardError):
    pass


class UnknownPending(GuardError):
    pass


def _default_dir() -> str:
    base = os.environ.get('JHP_DATA_DIR')
    if not base:
        base = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
    return os.path.join(base, 'submission_receipts')


@contextlib.contextmanager
def _file_lock(path: str, timeout: float = 10.0):
    """Exclusive lock across processes; degrades to a best-effort spin."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, 'a+b')
    deadline = time.time() + timeout
    locked = False
    try:
        while True:
            try:
                if _HAVE_MSVCRT:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                if _HAVE_FCNTL:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                break                      # no locking primitive: proceed
            except OSError:
                if time.time() > deadline:
                    raise GuardError('could not lock %s within %.0fs'
                                     % (path, timeout))
                time.sleep(0.05)
        yield
    finally:
        if locked:
            try:
                if _HAVE_MSVCRT:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                elif _HAVE_FCNTL:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


class ReceiptStore:
    def __init__(self, root: str | None = None):
        self.root = root or _default_dir()
        os.makedirs(self.root, exist_ok=True)

    # ---- paths ----
    def _key(self, account: str, school: str = '') -> str:
        raw = '%s__%s' % (account, school or 'default')
        safe = ''.join(c if c.isalnum() or c in '-_.' else '_' for c in raw)
        return os.path.join(self.root, safe + '.json')

    def _lock_path(self, account: str, school: str = '') -> str:
        return self._key(account, school) + '.lock'

    # ---- io ----
    def _load(self, account: str, school: str = '') -> dict:
        path = self._key(account, school)
        try:
            with open(path, encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as exc:
            # a corrupted receipt must stop submission, not be silently ignored
            raise GuardError('receipt file is corrupt: %s (%s)' % (path, exc))

    def _save(self, account: str, school: str, data: dict) -> None:
        path = self._key(account, school)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    # ---- API ----
    def reserve(self, account: str, uuid: str, start_ms: int, stop_ms: int,
                school: str = '', distance_m: int = 0) -> dict:
        """Claim a time window. Raises if it conflicts with a prior submission."""
        with _file_lock(self._lock_path(account, school)):
            data = self._load(account, school)

            for rec in data.values():
                state = rec.get('state')
                if state not in ('accepted', 'unknown', 'pending'):
                    continue
                if rec.get('uuid') == uuid:
                    continue
                if _overlap(start_ms, stop_ms, rec.get('start'), rec.get('stop')):
                    if state == 'unknown':
                        raise UnknownPending(
                            'a previous submission (%s) has an unknown outcome; '
                            'reconcile it before submitting again'
                            % rec.get('uuid', '?')[:8])
                    raise AlreadySubmitted(
                        'this time window overlaps an already %s run (%s..%s)'
                        % (state, _fmt(rec.get('start')), _fmt(rec.get('stop'))))

            data[uuid] = {
                'uuid': uuid,
                'start': int(start_ms),
                'stop': int(stop_ms),
                'distance_m': int(distance_m or 0),
                'state': 'pending',
                'rrid': None,
                'created': int(time.time() * 1000),
            }
            self._save(account, school, data)
            return data[uuid]

    def mark(self, account: str, uuid: str, state: str,
             rrid=None, school: str = '', note: str = '') -> None:
        if state not in ('pending', 'accepted', 'rejected', 'unknown'):
            raise ValueError('bad state %r' % state)
        with _file_lock(self._lock_path(account, school)):
            data = self._load(account, school)
            rec = data.get(uuid)
            if rec is None:
                raise GuardError('no receipt for uuid %s' % uuid)
            rec['state'] = state
            rec['rrid'] = rrid
            rec['updated'] = int(time.time() * 1000)
            if note:
                rec['note'] = note[:200]
            self._save(account, school, data)

    def get(self, account: str, uuid: str, school: str = '') -> dict | None:
        return self._load(account, school).get(uuid)

    def list(self, account: str, school: str = '') -> list[dict]:
        return list(self._load(account, school).values())


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _overlap(a1, a2, b1, b2) -> bool:
    if None in (a1, a2, b1, b2):
        return False
    return int(a1) < int(b2) and int(b1) < int(a2)


def _fmt(ms) -> str:
    if ms is None:
        return '?'
    # Beijing time, not the machine's (PR #82): a box on UTC would print a
    # window 8 hours off, and this string is how a conflict is recognised.
    try:
        from .domain import safety
        return safety.fmt_beijing(int(ms), '%m-%d %H:%M')
    except Exception:  # noqa: BLE001
        return time.strftime('%m-%d %H:%M', time.localtime(int(ms) / 1000))


def account_key(uid, unid) -> tuple[str, str]:
    return (str(uid), str(unid))


# --------------------------------------------------------------------------
# self test
# --------------------------------------------------------------------------

def self_test() -> list[tuple[str, bool, str]]:
    import tempfile
    out = []
    root = tempfile.mkdtemp(prefix='jhp_receipts_')
    store = ReceiptStore(root)
    a = '99999999'
    s = '47203'

    u1 = 'uuid-aaaa'
    store.reserve(a, u1, 1_000_000, 1_900_000, s, 2500)
    out.append(('receipt written', store.get(a, u1, s)['state'] == 'pending', 'pending'))

    store.mark(a, u1, 'accepted', rrid=1333336472, school=s)
    out.append(('state accepted', store.get(a, u1, s)['state'] == 'accepted', 'ok'))

    try:
        store.reserve(a, 'uuid-bbbb', 1_500_000, 2_000_000, s)
        out.append(('overlap refused', False, 'no exception'))
    except AlreadySubmitted:
        out.append(('overlap refused', True, 'AlreadySubmitted'))

    # non-overlapping window is fine
    store.reserve(a, 'uuid-cccc', 2_000_000, 2_500_000, s)
    out.append(('adjacent window ok', True, 'ok'))

    # unknown outcome blocks the same window
    store.mark(a, 'uuid-cccc', 'unknown', school=s)
    try:
        store.reserve(a, 'uuid-dddd', 2_100_000, 2_400_000, s)
        out.append(('unknown blocks', False, 'no exception'))
    except UnknownPending:
        out.append(('unknown blocks', True, 'UnknownPending'))

    # rejected does not block a retry
    store.mark(a, 'uuid-cccc', 'rejected', school=s)
    try:
        store.reserve(a, 'uuid-eeee', 2_100_000, 2_400_000, s)
        out.append(('rejected allows retry', True, 'ok'))
    except GuardError as exc:
        out.append(('rejected allows retry', False, str(exc)))

    # receipts carry no secrets
    blob = json.dumps(store.list(a, s), ensure_ascii=False).lower()
    leaked = [w for w in ('token', 'password', 'lat', 'lng', 'sign') if w in blob]
    out.append(('no secrets in receipts', not leaked, leaked or 'clean'))

    return out


if __name__ == '__main__':
    ok = True
    for name, passed, detail in self_test():
        print('%-30s %s   %s' % (name, 'PASS' if passed else 'FAIL', detail))
        ok = ok and passed
    print('\nRESULT:', 'PASS' if ok else 'FAIL')
    raise SystemExit(0 if ok else 1)

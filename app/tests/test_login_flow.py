"""Login flow: error taxonomy, uuid binding, and the manual-ticket path.

Every test here is offline; `RealClient` is replaced with a scripted fake so
the only thing under test is the decision logic.
"""

import pytest

from replica.application import login_flow as LF
from replica.transport import gt4


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.acct = None

    def request(self, acct, path, body, **kw):
        self.acct = acct
        self.calls.append((path, body, kw))
        if not self.replies:
            raise AssertionError('unexpected request to %s' % path)
        return {'status': 200, 'json': self.replies.pop(0)}


def _install(monkeypatch, replies):
    fake = FakeClient(replies)
    monkeypatch.setattr(LF, 'RealClient', lambda: fake)
    return fake


def _solver():
    calls = []

    def solve(captcha_id=None, log=None):
        calls.append(captcha_id)
        return {'lotNumber': 'lot-1', 'captchaOutput': 'co',
                'passToken': 'pt', 'genTime': '1'}

    solve.calls = calls
    return solve


def _login_ok():
    return {'error': 10000,
            'data': {'uid': 123, 'token': 'tok', 'unid': 47203,
                     'name': '张三'}}


def test_no_captcha_skips_the_solver(monkeypatch):
    client = _install(monkeypatch, [
        {'error': 10000, 'data': True},
        _login_ok(),
    ])
    solver = _solver()
    sess = LF.login('13800000000', 'pw', solver=solver, log=lambda *_: None)
    assert sess['token'] == 'tok'
    assert solver.calls == []
    assert client.acct.name == '13800000000'
    assert client.acct.uid == '-1'
    assert [c[0] for c in client.calls] == [LF.GEE_CHECK, LF.LOGIN]


def test_uuid_is_shared_by_check_validate_and_login(monkeypatch):
    client = _install(monkeypatch, [
        {'error': 10000, 'data': False},
        {'error': 10000},
        _login_ok(),
    ])
    LF.login('13800000000', 'pw', solver=_solver(), log=lambda *_: None)

    uuids = [body.get('uuid') for _, body, _ in client.calls]
    assert len(uuids) == 3
    assert len(set(uuids)) == 1
    assert uuids[0]


def test_10007_is_risk_control_and_is_not_retried(monkeypatch):
    client = _install(monkeypatch, [
        {'error': 10000, 'data': False},
        {'error': 10007, 'message': '风控'},
    ])
    solver = _solver()
    with pytest.raises(LF.RiskControlError) as exc:
        LF.login('13800000000', 'pw', solver=solver, log=lambda *_: None)
    assert '10007' in str(exc.value)
    assert len(solver.calls) == 1
    assert [c[0] for c in client.calls] == [LF.GEE_CHECK, LF.GEE_VALIDATE]


def test_one_low_trust_ticket_falls_back_to_manual(monkeypatch):
    _install(monkeypatch, [
        {'error': 10000, 'data': False},
        {'error': 10003, 'message': '验证失败'},
    ])
    solver = _solver()
    with pytest.raises(LF.CaptchaFailed) as exc:
        LF.login('13800000000', 'pw', solver=solver, log=lambda *_: None)
    # One solve cycle only.  Burning more tickets from the same IP is what
    # turns a captcha miss into a risk-control block.
    assert len(solver.calls) == 1
    assert '手动' in str(exc.value)


def test_rounds_can_be_raised_explicitly(monkeypatch):
    _install(monkeypatch, [
        {'error': 10000, 'data': False},
        {'error': 10003},
        {'error': 10000},
        _login_ok(),
    ])
    solver = _solver()
    sess = LF.login('13800000000', 'pw', solver=solver,
                    max_captcha_rounds=2, log=lambda *_: None)
    assert sess['uid'] == '123'
    assert len(solver.calls) == 2


def test_unexpected_geevalidate_code_is_not_swallowed(monkeypatch):
    _install(monkeypatch, [
        {'error': 10000, 'data': False},
        {'error': 11307, 'message': '参数不正确'},
    ])
    with pytest.raises(LF.LoginError) as exc:
        LF.login('13800000000', 'pw', solver=_solver(), log=lambda *_: None)
    assert '11307' in str(exc.value)


def test_solver_rejection_offers_the_manual_path(monkeypatch):
    _install(monkeypatch, [{'error': 10000, 'data': False}])

    def solver(captcha_id=None, log=None):
        raise gt4.GeeRejected('verify failed', status='fail')

    with pytest.raises(LF.CaptchaFailed) as exc:
        LF.login('13800000000', 'pw', solver=solver, log=lambda *_: None)
    assert '手动' in str(exc.value)


def test_login_rejection_carries_the_server_code(monkeypatch):
    _install(monkeypatch, [
        {'error': 10000, 'data': True},
        {'error': 10175, 'message': '密码错误'},
    ])
    with pytest.raises(LF.LoginError) as exc:
        LF.login('13800000000', 'pw', solver=_solver(), log=lambda *_: None)
    assert '10175' in str(exc.value)


def test_login_with_ticket_needs_all_four_credentials():
    with pytest.raises(LF.LoginError):
        LF.login_with_ticket('13800000000', 'pw',
                             {'lotNumber': 'x', 'captchaOutput': '',
                              'passToken': '', 'genTime': ''})


def test_login_with_ticket_posts_the_ticket(monkeypatch):
    client = _install(monkeypatch, [
        {'error': 10000, 'data': False},
        {'error': 10000},
        _login_ok(),
    ])
    sess = LF.login_with_ticket(
        '13800000000', 'pw',
        {'lotNumber': 'lot', 'captchaOutput': 'co',
         'passToken': 'pt', 'genTime': '1'},
        log=lambda *_: None)

    assert sess['token'] == 'tok'
    body = client.calls[1][1]
    assert body['lotNumber'] == 'lot'
    assert body['passToken'] == 'pt'
    assert body['businessType'] == 0
    assert client.calls[2][0] == LF.LOGIN


def test_login_with_ticket_skips_validation_when_not_needed(monkeypatch):
    client = _install(monkeypatch, [
        {'error': 10000, 'data': True},
        _login_ok(),
    ])
    sess = LF.login_with_ticket(
        '13800000000', 'pw',
        {'lotNumber': 'lot', 'captchaOutput': 'co',
         'passToken': 'pt', 'genTime': '1'},
        log=lambda *_: None)
    assert sess['token'] == 'tok'
    # A ticket is already in hand; posting it to an endpoint that says "no
    # captcha needed" can trip a parameter check, so go straight to login.
    assert [c[0] for c in client.calls] == [LF.GEE_CHECK, LF.LOGIN]


def test_login_with_ticket_reports_risk_control(monkeypatch):
    _install(monkeypatch, [
        {'error': 10000, 'data': False},
        {'error': 10007},
    ])
    with pytest.raises(LF.RiskControlError):
        LF.login_with_ticket(
            '13800000000', 'pw',
            {'lotNumber': 'lot', 'captchaOutput': 'co',
             'passToken': 'pt', 'genTime': '1'},
            log=lambda *_: None)

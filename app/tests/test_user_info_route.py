"""`user/info` moved from v70100 to v70400 in 7.4.0.

The probe is the only thing that tells us whether a stored token is still
alive, so it has to keep working across that move without ever mistaking a
missing route for a dead token.
"""
from replica.transport import routes


class FakeClient:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def request(self, acct, path, **kwargs):
        self.calls.append((path, kwargs))
        return self.answers.get(path, {'status': 500})


def test_new_route_is_tried_first():
    fake = FakeClient({'api/v70400/user/info': {'status': 200, 'json': {}}})
    res = routes.request_user_info(fake, object(), method='GET', retries=1)
    assert res['status'] == 200
    assert [c[0] for c in fake.calls] == ['api/v70400/user/info']
    assert fake.calls[0][1] == {'method': 'GET', 'retries': 1}


def test_missing_route_falls_back_to_the_old_one():
    fake = FakeClient({'api/v70400/user/info': {'status': 404},
                       'api/v70100/user/info': {'status': 200, 'json': {}}})
    res = routes.request_user_info(fake, object(), method='GET')
    assert res['status'] == 200
    assert [c[0] for c in fake.calls] == ['api/v70400/user/info',
                                          'api/v70100/user/info']


def test_405_also_counts_as_not_served():
    fake = FakeClient({'api/v70400/user/info': {'status': 405},
                       'api/v70100/user/info': {'status': 200}})
    assert routes.request_user_info(fake, object())['status'] == 200
    assert len(fake.calls) == 2


def test_a_rejected_token_is_not_retried():
    # 401 is the server's verdict on the token, not on the path.  Retrying it
    # would double every keepalive probe for a dead session.
    fake = FakeClient({'api/v70400/user/info': {'status': 401}})
    assert routes.request_user_info(fake, object())['status'] == 401
    assert [c[0] for c in fake.calls] == ['api/v70400/user/info']


def test_server_error_is_not_retried():
    fake = FakeClient({'api/v70400/user/info': {'status': 503}})
    assert routes.request_user_info(fake, object())['status'] == 503
    assert len(fake.calls) == 1


def test_both_routes_missing_returns_the_last_answer():
    fake = FakeClient({'api/v70400/user/info': {'status': 404},
                       'api/v70100/user/info': {'status': 404}})
    assert routes.request_user_info(fake, object())['status'] == 404
    assert len(fake.calls) == 2


def test_empty_response_does_not_crash():
    class Silent:
        def request(self, acct, path, **kwargs):
            return None

    assert routes.request_user_info(Silent(), object()) == {}

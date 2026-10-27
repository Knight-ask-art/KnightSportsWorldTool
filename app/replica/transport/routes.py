"""Endpoint versions, pinned to the official client.

``user/info`` is the one route that moved in 7.4.0: the 7.4.0 dex only carries
``api/v70400/user/info``, while 7.2.70 through 7.3.70 carry ``api/v70100``
(evidence: dex string pools, see ``archive/reference/PROTO_DIFF_7.4.0.md``).
Everything else on the submission path is unchanged across those three
releases, so it stays hard-coded at its call site.

The old path is kept as a fallback because the server still answers it today
and there is no published deprecation.  The fallback fires on a *missing
route* only: a 401/403 is the server's verdict about the token, and a 5xx is
a server fault.  Retrying either against the older path would turn one clear
answer into two ambiguous ones.
"""
from __future__ import annotations

USER_INFO_PATHS = ('api/v70400/user/info', 'api/v70100/user/info')

#: statuses that mean "this route is not served", not "this request failed"
_NOT_SERVED = (404, 405)


def request_user_info(client, acct, **kwargs) -> dict:
    """GET ``user/info``, preferring the version the current app ships."""
    last: dict = {}
    for path in USER_INFO_PATHS:
        res = client.request(acct, path, **kwargs) or {}
        last = res
        if res.get('status') not in _NOT_SERVED:
            return res
    return last

"""Diagnostics bundle: the redaction filter must not eat safe sections.

``collector.collect`` drops any top-level key whose name contains a sensitive
substring, and the list includes the two-letter ``ip``.  ``last_receipt``
contains it ("rece*ip*t"), which is why the field is named
``last_submission`` -- a rename back would silently empty that section.
"""

from replica.diagnostics import collector as D


def test_last_submission_survives_redaction():
    info = D.collect(extra={
        'last_submission': {'state': 'accepted', 'distance_m': 4100},
        'client': {'platform': 'ios', 'user_agent': 'SWCampus/7.3.40'},
    })
    assert info['last_submission']['state'] == 'accepted'
    assert info['client']['platform'] == 'ios'
    text = D.render(info, 'text')
    assert 'last_submission' in text
    assert 'accepted' in text


def test_secrets_are_still_dropped():
    info = D.collect(extra={'token': 'abc', 'uid': 42, 'lat': 22.4,
                            'note': 'kept'})
    assert 'token' not in info and 'uid' not in info and 'lat' not in info
    assert info['note'] == 'kept'

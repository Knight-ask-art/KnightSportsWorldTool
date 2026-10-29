"""Backfill start time must not sit on a whole minute.

Every downstream timestamp (point ``flag``, 10 s window boundaries,
``gainTime``) is derived from the start, so a ``:00.000`` start puts the whole
run on a visible grid.  The reference client uses ``System.currentTimeMillis()``
and cannot produce one.
"""

import datetime as _dt

from replica.domain import safety as S

BEIJING = _dt.timezone(_dt.timedelta(hours=8))


def _now(y=2026, m=10, d=23, hh=15, mm=0):
    return int(_dt.datetime(y, m, d, hh, mm, tzinfo=BEIJING).timestamp() * 1000)


def test_random_start_has_sub_minute_precision():
    now = _now()
    picks = [S.resolve_start_ms(date='2026-10-22', duration_s=1800, now=now)
             for _ in range(40)]
    assert any(ms % 60000 for ms in picks), 'every start landed on :00.000'
    assert any((ms // 1000) % 60 for ms in picks), 'every start landed on :00 s'
    for ms in picks:
        local = S.to_beijing(ms)
        assert local.date().isoformat() == '2026-10-22'
        assert local.hour >= 9
        assert local.hour <= 21


def test_explicit_minute_keeps_the_minute_and_still_jitters():
    now = _now()
    picks = [S.resolve_start_ms(date='2026-10-22', hhmm='10:30',
                                duration_s=1800, now=now)
             for _ in range(20)]
    for ms in picks:
        local = S.to_beijing(ms)
        assert (local.hour, local.minute) == (10, 30)
    assert any(ms % 60000 for ms in picks), 'explicit slot was minute-aligned'


def test_jitter_never_pushes_the_run_past_the_deadline():
    # 14:45 + 30 min would end at 15:15, i.e. after "now" (15:00): the shared
    # slot has to stop at 14:30, jitter included.
    now = _now(hh=15, mm=0)
    for _ in range(20):
        start = S.resolve_start_ms(date='2026-10-23', duration_s=1800, now=now)
        assert start + 1800 * 1000 <= now


def test_jitter_stays_inside_the_22_00_window():
    # A yesterday run has the whole day available; it must still end by 22:00.
    now = _now(hh=23, mm=0)
    for _ in range(20):
        start = S.resolve_start_ms(date='2026-10-22', duration_s=3600, now=now)
        end = S.to_beijing(start + 3600 * 1000)
        assert end.date().isoformat() == '2026-10-22'
        assert (end.hour, end.minute) <= (22, 0)

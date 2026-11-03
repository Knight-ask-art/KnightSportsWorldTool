"""Local statistics: windowing, averages, de-duplication, failure ranking."""

import datetime as _dt

from replica.application import stats as S
from replica.domain import safety

DAY = 86_400_000
#: A Wednesday, so "this week" spans Mon..Sun with three past days.
NOW = int(_dt.datetime(2026, 11, 4, 12, 0, tzinfo=safety.BEIJING)
          .timestamp() * 1000)


def ms(days_ago=0, hour=8):
    d = safety.to_beijing(NOW).date() - _dt.timedelta(days=days_ago)
    return int(_dt.datetime(d.year, d.month, d.day, hour,
                            tzinfo=safety.BEIJING).timestamp() * 1000)


def run(uuid, start, km, state='accepted', **extra):
    row = {'uuid': uuid, 'start': start, 'stop': start + 1_500_000,
           'distance_m': km * 1000, 'state': state}
    row.update(extra)
    return row


def test_empty_is_honest_about_source():
    res = S.summarise([], weeks=4, now_ms=NOW)
    assert res['source'] == 'local'
    assert res['totals']['runs'] == 0
    assert res['totals']['avg_pace_min_per_km'] is None
    assert res['submission']['success_rate'] is None
    assert res['totals']['by_week'] == []


def test_weekly_buckets_start_on_monday():
    rows = [run('a', ms(0), 2.5), run('b', ms(2), 3.0), run('c', ms(9), 4.0)]
    res = S.summarise(rows, weeks=4, now_ms=NOW)
    weeks = res['totals']['by_week']
    assert [w['week_start'] for w in weeks] == ['2026-10-26', '2026-11-02']
    assert weeks[-1]['distance_km'] == 5.5
    assert weeks[-1]['runs'] == 2
    assert weeks[-1]['active_days'] == 2
    assert weeks[0]['distance_km'] == 4.0


def test_window_drops_old_rows_and_future_rows():
    rows = [run('old', ms(60), 5.0), run('future', NOW + 2 * DAY, 5.0),
            run('fresh', ms(1), 1.0)]
    res = S.summarise(rows, weeks=4, now_ms=NOW)
    assert res['totals']['runs'] == 1
    assert res['totals']['distance_km'] == 1.0


def test_average_pace_is_total_time_over_total_distance():
    rows = [run('a', ms(1), 1.0, duration_s=480),     # 8:00 / km
            run('b', ms(2), 5.0, duration_s=1800)]    # 6:00 / km
    res = S.summarise(rows, weeks=4, now_ms=NOW)
    # (480 + 1800) / 60 / 6 km = 6.33, not the 7.0 mean of the two paces.
    assert res['totals']['avg_pace_min_per_km'] == 6.33


def test_prefers_server_duration_over_receipt_window():
    rows = [run('a', ms(1), 2.0, duration_s=600)]
    res = S.summarise(rows, weeks=4, now_ms=NOW)
    assert res['totals']['avg_pace_min_per_km'] == 5.0   # 10 min / 2 km


def test_receipt_falls_back_to_start_stop_for_duration():
    rows = [run('a', ms(1), 2.5)]                # stop - start = 1500 s
    res = S.summarise(rows, weeks=4, now_ms=NOW)
    assert res['totals']['avg_pace_min_per_km'] == 10.0


def test_success_rate_and_failure_top_list():
    rows = [run('a', ms(1), 2.5),
            run('b', ms(2), 0, 'rejected', reason='11016'),
            run('c', ms(3), 0, 'rejected', reason='11016'),
            run('d', ms(4), 0, 'rejected', reason='10003'),
            run('e', ms(5), 0, 'rejected', error='请求超时'),
            run('f', ms(6), 0, 'pending'),
            run('g', ms(7), 0, 'unknown')]
    res = S.summarise(rows, weeks=4, now_ms=NOW)
    sub = res['submission']
    assert sub['attempts'] == 7 and sub['accepted'] == 1
    assert sub['rejected'] == 4 and sub['open'] == 2
    assert sub['success_rate'] == 14.3
    assert sub['top_failures'][0] == {'reason': '11016', 'count': 2}
    assert [(f['reason'], f['count']) for f in sub['top_failures']] == [
        ('11016', 2), ('10003', 1), ('请求超时', 1)]


def test_unfinished_rows_do_not_count_as_km():
    rows = [run('a', ms(1), 2.5, 'pending'), run('b', ms(2), 1.0, 'unknown')]
    res = S.summarise(rows, weeks=4, now_ms=NOW)
    assert res['totals']['runs'] == 0
    assert res['submission']['attempts'] == 2


def test_records_win_over_receipts_for_the_same_uuid():
    rows = [run('same', ms(1), 2.5),
            dict(run('same', ms(1), 2.5), _source='record', complete=True,
                 duration_s=900)]
    res = S.summarise(rows, weeks=4, now_ms=NOW)
    assert res['submission']['attempts'] == 1
    assert res['totals']['avg_pace_min_per_km'] == 6.0


def test_record_without_state_is_classified_by_completeness():
    rows = [dict(_source='record', uuid='r1', start_ms=ms(1),
                 distance_m=2000, complete=True),
            dict(_source='record', uuid='r2', start_ms=ms(2),
                 distance_m=2000, complete=False, statusInfo='运动数据有问题')]
    res = S.summarise(rows, weeks=4, now_ms=NOW)
    assert res['submission']['accepted'] == 1
    assert res['submission']['rejected'] == 1
    assert res['submission']['top_failures'][0]['reason'] == '运动数据有问题'


def test_month_and_weekday_breakdown():
    rows = [run('a', NOW - 3_600_000, 2.5),      # Wednesday
            run('b', ms(2), 1.5),                # Monday
            run('c', ms(40), 3.0)]               # September
    res = S.summarise(rows, weeks=12, now_ms=NOW)
    assert res['month']['label'] == '2026-11'
    assert res['month']['distance_km'] == 4.0
    assert res['weekday_km'][0] == 1.5           # Monday
    assert res['weekday_km'][2] == 2.5           # Wednesday


def test_cadence_prefers_reported_value_then_steps():
    rows = [run('a', ms(1), 2.0, avg_step_freq=152, steps=3000),
            run('b', ms(2), 2.0, avg_step_freq=180, steps=3600)]
    res = S.summarise(rows, weeks=4, now_ms=NOW)
    assert res['totals']['avg_cadence'] == 166.0
    # No reported value at all: steps / time, not a mix of both methods.
    steps_only = [run('c', ms(3), 2.0, steps=3600, duration_s=1200)]
    res = S.summarise(steps_only, weeks=4, now_ms=NOW)
    assert res['totals']['avg_cadence'] == 180.0


def test_weeks_argument_is_clamped():
    assert S.summarise([], weeks=0, now_ms=NOW)['weeks'] == 1
    assert S.summarise([], weeks=10_000, now_ms=NOW)['weeks'] == S.MAX_WEEKS
    assert S.summarise([], weeks='x', now_ms=NOW)['weeks'] == S.DEFAULT_WEEKS


def test_junk_rows_are_ignored():
    res = S.summarise([None, 'x', {}, {'start': 'not-a-number'}], now_ms=NOW)
    assert res['submission']['attempts'] == 0


def test_load_for_account_reads_receipts_and_marks_source(tmp_path):
    from replica.application import guard
    store = guard.ReceiptStore(str(tmp_path))
    store.reserve('1001', 'u-1', ms(1), ms(1) + 1_500_000,
                  school='47203', distance_m=2500)
    store.mark('1001', 'u-1', 'accepted', rrid=9, school='47203')
    local = S.load_for_account(str(tmp_path), '1001', '47203', weeks=4,
                               now_ms=NOW)
    assert local['source'] == 'local'
    assert local['receipts_used'] == 1
    assert local['totals']['distance_km'] == 2.5
    both = S.load_for_account(str(tmp_path), '1001', '47203', weeks=4,
                              now_ms=NOW, records=[], record_error='x')
    assert both['source'] == 'local'
    assert both['record_error'] == 'x'


def test_load_for_account_survives_a_corrupt_receipt_file(tmp_path):
    (tmp_path / '1001__default.json').write_text('{broken', encoding='utf-8')
    res = S.load_for_account(str(tmp_path), '1001', '', now_ms=NOW)
    assert res['source'] == 'local'
    assert '回执读取失败' in res['record_error']


def test_self_test_passes():
    assert all(ok for _, ok, _ in S.self_test())

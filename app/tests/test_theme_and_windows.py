"""Multi-band school windows (issue #11) and the appearance skin contract."""

import datetime as dt
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from replica.adapters import http_server
from replica.adapters.bridge_api import ReplicaApi
from replica.application import policy, qualification
from replica.domain import safety

BEIJING = dt.timezone(dt.timedelta(hours=8))
BANDS = [
    {'start': '06:00', 'end': '07:59'},
    {'start': '09:01', 'end': '11:59'},
    {'start': '13:01', 'end': '13:59'},
    {'start': '15:01', 'end': '22:00'},
]


def _now():
    return int(dt.datetime.now(BEIJING).timestamp() * 1000)


def _day_ago_key():
    return (dt.datetime.now(BEIJING)
            - dt.timedelta(days=1)).strftime('%Y-%m-%d')


def test_normalize_windows_accepts_server_shapes():
    assert safety.normalize_windows(
        [{'start': '06:00:00', 'end': '07:59:00'}]) == [
        {'start': '06:00', 'end': '07:59'}]
    assert safety.normalize_windows(
        '06:00-07:59, 09:01-11:59') == BANDS[:2]
    assert safety.normalize_windows(json.dumps(BANDS)) == BANDS
    assert safety.normalize_windows([['15:01', '22:00']]) == [BANDS[-1]]
    assert safety.normalize_windows(
        ['06:00-07:59', '09:01-11:59']) == BANDS[:2]
    assert safety.normalize_windows(
        ['06:00', '07:59', '09:01', '11:59']) == BANDS[:2]
    assert safety.normalize_windows(['06：00-07：59']) == [BANDS[0]]


def test_policy_maps_every_band_and_writes_an_envelope():
    mapped = policy.rules_from_policy(
        {'runRuleModel': {'validTime': [
            {'start': '06:00:00', 'end': '07:59:00'},
            {'start': '09:01:00', 'end': '11:59:00'},
            {'start': '13:01:00', 'end': '13:59:00'},
            {'start': '15:01:00', 'end': '22:00:00'},
        ]}})
    rules = mapped['rules']
    assert rules['windows'] == BANDS
    # Old clients only understand one pair; the envelope accepts all bands.
    assert rules['window_start'] == '06:00'
    assert rules['window_end'] == '22:00'
    assert mapped['meta']['window_count'] == 4


def test_pause_reserve_is_included_in_the_window_fit():
    from replica.domain import rundata
    yesterday = dt.datetime.now(BEIJING) - dt.timedelta(days=1)
    start = yesterday.replace(hour=21, minute=45, second=0, microsecond=0)
    run = rundata.generate_run_data(
        2.5, 6.0, 150, center_lat=22.4108, center_lng=112.9408,
        start_ms=int(start.timestamp() * 1000), pause_profile='auto')
    assert run['pause_reserve_s'] == 180
    model = qualification.evaluate(
        run, {'min_km': 2.0, 'windows': BANDS}, daily_runs=0)
    check = [c for c in model['checks'] if c['id'] == 'window'][0]
    # 15 min active fits 21:45-22:00, but the submit path reserves 18 min
    # (15 active + up to 3 min pause), which ends at 22:03.
    assert check['status'] == 'fail'
    assert '预留' in check['detail']


def test_manual_rules_are_not_overwritten_by_a_single_server_band():
    cached = {'auto_fetch': False, 'windows': BANDS, 'min_km': 2.0}
    merged, changed = policy.merge_rules(
        cached, {'runRuleModel': {'validTime': [
            {'start': '15:01:00', 'end': '22:00:00'}]}})
    assert changed is False
    assert merged['windows'] == BANDS


def test_qualification_accepts_a_later_band():
    rules = {'min_km': 2.0, 'windows': BANDS}
    yesterday = dt.datetime.now(BEIJING) - dt.timedelta(days=1)
    late = yesterday.replace(hour=18, minute=0, second=0, microsecond=0)
    run = {
        'juli': 2500, 'sportTime': 900, 'total_steps': 1300,
        'locPoints': [{'flag': int(late.timestamp() * 1000)}],
        'fivePoint': [],
    }
    model = qualification.evaluate(run, rules, daily_runs=0)
    check = [c for c in model['checks'] if c['id'] == 'window'][0]
    assert check['status'] == 'pass', check

    gap = yesterday.replace(hour=8, minute=30, second=0, microsecond=0)
    run['locPoints'][0]['flag'] = int(gap.timestamp() * 1000)
    model = qualification.evaluate(run, rules, daily_runs=0)
    check = [c for c in model['checks'] if c['id'] == 'window'][0]
    assert check['status'] == 'fail'
    assert '15:01–22:00' in check['detail']

    # The backend requires the whole run inside one band; the model must not
    # say "pass" for a start that leaves only 10 minutes in 15:01-22:00.
    late = yesterday.replace(hour=21, minute=50, second=0, microsecond=0)
    run['locPoints'][0]['flag'] = int(late.timestamp() * 1000)
    run['sportTime'] = 40 * 60
    model = qualification.evaluate(run, rules, daily_runs=0)
    check = [c for c in model['checks'] if c['id'] == 'window'][0]
    assert check['status'] == 'fail'
    assert '放不进' in check['detail']


def test_resolve_start_uses_windows_and_rejects_gaps():
    now = _now()
    start = safety.resolve_start_ms(date=_day_ago_key(), hhmm='18:00',
                                    duration_s=1200, now=now,
                                    windows=BANDS)
    local = safety.to_beijing(start)
    assert (local.hour, local.minute) == (18, 0)
    try:
        safety.resolve_start_ms(date=_day_ago_key(), hhmm='08:30',
                                duration_s=1200, now=now, windows=BANDS)
    except ValueError as e:
        assert '有效时段' in str(e)
    else:
        raise AssertionError('08:30 should be rejected between bands')


def test_empty_random_stays_inside_a_band():
    now = _now()
    for _ in range(40):
        start = safety.resolve_start_ms(date=_day_ago_key(), duration_s=600,
                                        now=now, windows=BANDS)
        hhmm = safety.to_beijing(start).strftime('%H:%M')
        assert safety.in_windows(hhmm, BANDS), hhmm


def test_bad_window_rows_are_rejected_not_silently_deleted():
    rules, errors = qualification.normalize_rules({
        'windows': [{'start': '06:00'}, {'start': '09:01', 'end': '11:59'}]
    })
    assert errors
    assert '缺少结束时间' in '；'.join(errors)
    # The valid row is still parsed for preview, but save_rules must refuse.
    assert rules['windows'] == [{'start': '09:01', 'end': '11:59'}]


def test_save_rejects_bad_window_rows(tmp_path, monkeypatch):
    monkeypatch.setenv('JHP_DATA_DIR', str(tmp_path))
    try:
        qualification.save_rules({'windows': [{'start': '06:00'}]})
    except ValueError as exc:
        assert '缺少结束时间' in str(exc)
    else:
        raise AssertionError('malformed window row was saved silently')


def test_rules_roundtrip_keeps_windows(tmp_path, monkeypatch):
    monkeypatch.setenv('JHP_DATA_DIR', str(tmp_path))
    saved = qualification.save_rules({
        'min_km': 2.0, 'windows': BANDS,
        'auto_fetch': True,
    })
    assert saved['windows'] == BANDS
    assert saved['window_start'] == '06:00'
    assert saved['window_end'] == '22:00'
    assert qualification.load_rules()['windows'] == BANDS


def test_ui_state_skin_roundtrip(tmp_path):
    api = ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                     str(tmp_path))
    assert api.real_get_ui_state()['data']['skin'] == 'paper'
    assert api.real_set_ui_state(skin='poster')['code'] == 200
    assert api.real_get_ui_state()['data']['skin'] == 'poster'
    assert api.real_set_ui_state(skin='not-a-skin')['code'] != 200
    assert api.real_get_ui_state()['data']['skin'] == 'poster'


def test_theme_boot_reads_ui_state(tmp_path):
    with open(tmp_path / 'ui_state.json', 'w', encoding='utf-8') as fh:
        json.dump({'skin': 'archive'}, fh)
    script = http_server.theme_boot_js(str(tmp_path))
    assert 'data-skin' in script and '"archive"' in script
    assert "data-skin-source" in script


def test_index_loads_skin_system_before_styles():
    with open(os.path.join(ROOT, 'frontend', 'index.html'),
              encoding='utf-8') as fh:
        index = fh.read()
    assert '/theme-boot.js' in index
    assert index.index('/theme-boot.js') < index.index('css/app.css')
    assert 'css/skins.css' in index
    assert 'id="skinGrid"' in index
    assert 'id="windowRows"' in index


def _luminance(hex_color):
    text = hex_color.lstrip('#')
    channels = [int(text[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    def linear(c):
        return c / 12.92 if c <= .03928 else ((c + .055) / 1.055) ** 2.4
    return (0.2126 * linear(channels[0]) +
            0.7152 * linear(channels[1]) +
            0.0722 * linear(channels[2]))


def _contrast(a, b):
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + .05) / (lo + .05)


def test_new_skins_keep_small_text_above_aa_contrast():
    with open(os.path.join(ROOT, 'frontend', 'css', 'skins.css'),
              encoding='utf-8') as fh:
        css = fh.read()
    blocks = dict(re.findall(r'\[data-skin="([a-z]+)"\]\{(.*?)\}',
                             css, re.S))
    for skin in ('poster', 'minimal', 'archive'):
        block = blocks[skin]
        def token(name):
            match = re.search(r'--%s:\s*(#[0-9A-Fa-f]{6})' % name, block)
            assert match, (skin, name)
            return match.group(1)
        assert _contrast(token('faint'), token('paper-2')) >= 4.5, skin
        assert _contrast(token('sidebar-muted'), token('sidebar-bg')) >= 4.5, skin
        assert _contrast(token('amber'), token('paper-2')) >= 4.5, skin

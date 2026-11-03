"""Named run presets: whitelisting, upsert, cap and corrupt-file fallback."""

import json

import pytest

from replica.application import presets as P


def _fields(tmp, name='早跑'):
    row = P.get_preset(str(tmp), name)
    return row['data']


def test_save_and_get_roundtrip(tmp_path):
    res = P.save_preset(str(tmp_path), '早跑', {
        'km': 2.5, 'pace': 6.0, 'cadence': 150, 'altitude': 25,
        'closed': True, 'altitude_mode': 'range', 'altitude_range': [20, 50],
        'km_times': [360, 360, 180],
    })
    assert res['saved'] is True and res['replaced'] is False
    assert res['count'] == 1
    data = _fields(tmp_path)
    assert data['km'] == 2.5 and data['altitude_range'] == [20, 50]
    assert data['km_times'] == [360, 360, 180]


def test_unknown_and_secret_fields_are_dropped(tmp_path):
    P.save_preset(str(tmp_path), '敏感', {
        'km': 3, 'pace': 6.5,
        'token': 'secret-token', 'password': 'hunter2',
        'obs_signature': 'sig', 'whatever': {'a': 1},
        'altitude_profile': [10, 11, 12],
    })
    saved = json.loads((tmp_path / P.FILENAME).read_text(encoding='utf-8'))
    text = json.dumps(saved)
    for forbidden in ('secret-token', 'hunter2', 'obs_signature', 'whatever'):
        assert forbidden not in text
    assert saved['presets'][0]['data']['altitude_profile'] == [10, 11, 12]


def test_wrong_types_are_rejected_not_coerced(tmp_path):
    P.save_preset(str(tmp_path), '类型', {
        'km': '2.5', 'pace': True, 'cadence': 150,
        'closed': 'yes', 'km_times': ['360'],
    })
    data = _fields(tmp_path, '类型')
    assert 'km' not in data
    assert 'pace' not in data
    assert 'closed' not in data
    assert 'km_times' not in data
    assert data['cadence'] == 150


def test_same_name_replaces_in_place_and_keeps_created_at(tmp_path):
    P.save_preset(str(tmp_path), '晨跑', {'km': 2})
    first = P.get_preset(str(tmp_path), '晨跑')
    res = P.save_preset(str(tmp_path), '晨跑', {'km': 3})
    second = P.get_preset(str(tmp_path), '晨跑')
    assert res['replaced'] is True and res['count'] == 1
    assert second['data'] == {'km': 3}
    assert second['created_at'] == first['created_at']
    assert second['updated_at'] > first['updated_at']
    assert P.list_presets(str(tmp_path))['presets'][0]['name'] == '晨跑'


def test_cap_drops_the_oldest(tmp_path, monkeypatch):
    clock = [1000.0]

    def tick():
        clock[0] += 1
        return clock[0]

    monkeypatch.setattr(P.time, 'time', tick)
    for i in range(P.MAX_PRESETS + 2):
        P.save_preset(str(tmp_path), 'p%02d' % i, {'km': 1 + i / 100})
    res = P.list_presets(str(tmp_path))
    assert res['count'] == P.MAX_PRESETS
    names = [r['name'] for r in res['presets']]
    assert 'p00' not in names and 'p01' not in names
    assert names[0] == 'p%02d' % (P.MAX_PRESETS + 1)   # newest first


def test_delete(tmp_path):
    P.save_preset(str(tmp_path), 'a', {'km': 1})
    P.save_preset(str(tmp_path), 'b', {'km': 2})
    res = P.delete_preset(str(tmp_path), 'a')
    assert res['deleted'] is True and res['count'] == 1
    again = P.delete_preset(str(tmp_path), 'a')
    assert again['deleted'] is False


def test_corrupt_file_reads_as_empty_and_is_recoverable(tmp_path):
    (tmp_path / P.FILENAME).write_text('{not json', encoding='utf-8')
    assert P.list_presets(str(tmp_path)) == {'presets': [], 'count': 0,
                                             'max': P.MAX_PRESETS}
    assert P.get_preset(str(tmp_path), 'x') is None
    P.save_preset(str(tmp_path), 'x', {'km': 1})
    assert P.list_presets(str(tmp_path))['count'] == 1
    # The temporary file from the atomic write must not linger.
    assert not [p for p in tmp_path.iterdir() if p.name.startswith('.presets-')]


def test_oversized_file_reads_as_empty(tmp_path):
    (tmp_path / P.FILENAME).write_text('x' * (P.MAX_BYTES + 10),
                                       encoding='utf-8')
    assert P.list_presets(str(tmp_path))['count'] == 0


def test_validation_errors():
    with pytest.raises(P.PresetError):
        P.clean_name('   ')
    with pytest.raises(P.PresetError):
        P.clean_name('名' * (P.MAX_NAME_LEN + 1))
    with pytest.raises(P.PresetError):
        P.clean_data(['not', 'a', 'dict'])
    with pytest.raises(P.PresetError):
        P.handle('', 'save', 'x', {})
    with pytest.raises(P.PresetError):
        P.handle('', 'explode')
    with pytest.raises(P.PresetError):
        P.handle('', 'get', 'missing')
    with pytest.raises(P.PresetError):
        P.handle('', 'delete', '  ')


def test_handle_actions(tmp_path):
    res = P.handle(str(tmp_path), 'save', '周末', {'km': 5, 'pace': 7})
    assert res['saved'] and res['count'] == 1
    got = P.handle(str(tmp_path), 'get', '周末')
    assert got['preset']['data']['km'] == 5
    listed = P.handle(str(tmp_path), 'list')
    assert listed['count'] == 1
    assert P.handle(str(tmp_path), 'delete', '周末')['deleted'] is True


def test_self_test_passes(tmp_path):
    assert all(ok for _, ok, _ in P.self_test())

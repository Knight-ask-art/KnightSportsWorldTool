"""OBS-backed record detail and the semester-policy rule read.

Two server behaviors that the UI depends on:

* records uploaded with ``jsonFromObs=1`` keep ``allLocJson`` empty in the
  database; the track must be read from the OBS object;
* ``runModePolicy`` only returns ``runRuleModel`` for ``operateType=1``; the
  start-run request (``operateType=0``) returns ``policy`` only.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from replica.application import readonly  # noqa: E402
from replica.transport import obs  # noqa: E402
from replica.adapters import bridge_api as RA  # noqa: E402
from replica.transport import realclient  # noqa: E402

from urllib.error import HTTPError  # noqa: E402


class _Acct:
    uid = 15663170
    unid = 47203
    center_lat = 22.4108
    center_lng = 112.9408


def _obs_body():
    run_data = {
        'allLocJson': json.dumps([
            {'gLat': 22.4113343, 'gLng': 112.9410163, 'bdA': 25.0,
             'gainTime': '2026-10-21 20:00:00'},
        ]),
        'useZip': False,
    }
    five = {
        'fivePointJson': json.dumps([
            {'glat': 22.411344, 'glon': 112.941001, 'pointName': '点位2',
             'isPass': True, 'position': 0},
        ]),
        'useZip': False,
    }
    return json.dumps({
        'run_data': obs.gz_json(run_data),
        'fixed_point_json': obs.gz_str(json.dumps(five)),
        'segment_json': obs.gz_str('[]'),
    }).encode()


class _DetailClient:
    def __init__(self, key_sink):
        self.key_sink = key_sink

    def request(self, acct, path, payload=None, retries=2):
        if path == obs.OBS_SIGN_PATH:
            self.key_sink.append(payload.get('objectKey'))
            return {'json': {'error': 10000,
                             'data': {'signedUrl': 'https://signed.invalid/x'}}}
        return {'json': {'error': 10000, 'data': {
            'rrid': 123,
            'uuid': 'u-123',
            'startTime': 1792584000000,
            'stopTime': 1792586220000,
            'totalDis': 4100,
            'totalTime': 2214,
            'validDis': 4100,
            'validTime': 2214,
            'jsonFromObs': 1,
            'allLocJson': None,
            'fivePointJson': '',
            'segmentJson': None,
            'reasonList': [],
            'complete': True,
            'status': 0,
            'statusInfo': '',
            'normalRun': None,
        }}}


def test_record_detail_reads_the_obs_track_when_the_row_is_a_stub(monkeypatch):
    keys = []
    monkeypatch.setattr(obs, 'get_object_bytes',
                        lambda url, timeout=60, user_agent='': (200, _obs_body()))
    r = readonly.record_detail(_DetailClient(keys), _Acct(), 123)
    assert r['ok'] is True
    assert len(keys) == 1 and keys[0].startswith('run_data/')
    assert keys[0].endswith('/u-123.json'), keys
    assert len(r['track']) == 1
    assert r['track'][0]['lat'] == 22.4113343
    assert len(r['five_point']) == 1
    assert r['stats']['has_obs_track'] is True
    assert r['detail']['json_from_obs'] is True
    assert r['detail']['obs_error'] == ''


def test_obs_failure_keeps_the_summary_instead_of_failing(monkeypatch):
    def explode(url, timeout=60, user_agent=''):
        raise OSError('network down')

    monkeypatch.setattr(obs, 'get_object_bytes', explode)
    r = readonly.record_detail(_DetailClient([]), _Acct(), 123)
    assert r['ok'] is True
    assert r['track'] == []
    assert r['detail']['json_from_obs'] is True
    assert 'network down' in r['detail']['obs_error']
    assert r['stats']['track_source'] == 'none'


def test_obs_falls_back_to_the_rrid_key(monkeypatch):
    """Old rows only exist at the rrid key; the hour/uuid key 404s."""
    keys = []
    calls = {'n': 0}

    def get(url, timeout=60, user_agent=''):
        calls['n'] += 1
        if calls['n'] == 1:
            raise HTTPError(url, 404, 'Not Found', None, None)
        return 200, _obs_body()

    monkeypatch.setattr(obs, 'get_object_bytes', get)
    r = readonly.record_detail(_DetailClient(keys), _Acct(), 123)
    assert r['ok'] is True
    assert len(keys) == 2
    assert keys[0].startswith('run_data/') and keys[0].endswith('/u-123.json')
    assert keys[1] == 'run_data/0/123.json'
    assert len(r['track']) == 1
    assert r['detail']['obs_error'] == ''
    assert r['stats']['track_source'] == 'obs'
    assert r['stats']['has_obs_track'] is True


def test_inline_track_does_not_fetch_obs(monkeypatch):
    """An inline track is enough; an empty segment_json is not a reason."""
    keys = []

    def explode(url, timeout=60):
        raise AssertionError('OBS must not be read when the row has a track')

    class _Inline(_DetailClient):
        def request(self, acct, path, payload=None, retries=2):
            out = super().request(acct, path, payload, retries)
            if path != obs.OBS_SIGN_PATH:
                out['json']['data']['allLocJson'] = json.dumps([
                    {'gLat': 22.4113343, 'gLng': 112.9410163, 'bdA': 25.0,
                     'gainTime': '2026-10-21 20:00:00'},
                ])
            return out

    monkeypatch.setattr(obs, 'get_object_bytes', explode)
    r = readonly.record_detail(_Inline(keys), _Acct(), 123)
    assert r['ok'] is True
    assert keys == []
    assert len(r['track']) == 1
    assert r['detail']['obs_error'] == ''
    assert r['stats']['track_source'] == 'inline'


def test_detail_uses_step_windows_when_points_have_no_steps(monkeypatch):
    """7.4.0 points dropped `steps`; the chart still needs step_freq_json."""
    class _Inline(_DetailClient):
        def request(self, acct, path, payload=None, retries=2):
            out = super().request(acct, path, payload, retries)
            if path != obs.OBS_SIGN_PATH:
                d = out['json']['data']
                d['jsonFromObs'] = 0
                d['allLocJson'] = json.dumps([
                    {'gLat': 22.4110, 'gLng': 112.9410,
                     'totalDis': 0, 'totalTime': 0},
                    {'gLat': 22.4120, 'gLng': 112.9420,
                     'totalDis': 500, 'totalTime': 300},
                    {'gLat': 22.4130, 'gLng': 112.9430,
                     'totalDis': 1000, 'totalTime': 600},
                ])
                d['step_freq_json'] = json.dumps([
                    {'beginTime': 1792584000000,
                     'endTime': 1792584300000, 'stepsNum': 750},
                    {'beginTime': 1792584300000,
                     'endTime': 1792584600000, 'stepsNum': 750},
                ])
            return out

    monkeypatch.setattr(obs, 'get_object_bytes',
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError('inline track must not read OBS')))
    r = readonly.record_detail(_Inline([]), _Acct(), 123)
    assert r['ok'] is True
    assert r['analysis'] is not None
    assert r['analysis']['avg_cadence'] is not None, r['analysis']
    assert 140.0 <= r['analysis']['avg_cadence'] <= 160.0, r['analysis']


def test_missing_obs_object_is_reported_as_404(monkeypatch):
    def missing(url, timeout=60):
        raise HTTPError(url, 404, 'Not Found', None, None)

    monkeypatch.setattr(obs, 'get_object_bytes', missing)
    r = readonly.record_detail(_DetailClient([]), _Acct(), 123)
    assert r['ok'] is True
    assert r['track'] == []
    assert '404' in r['detail']['obs_error']
    assert r['stats']['track_source'] == 'none'


class _RuleClient:
    def __init__(self):
        self.seen = []

    def request(self, acct, path, body=None, retries=2):
        self.seen.append(dict(body or {}))
        model = None
        if (body or {}).get('operateType') == 1:
            model = {
                'minDistance': 1000,
                'minRunTime': 300,
                'speedTop': 2.17,
                'speedBottom': 10.0,
                'stepTop': 300,
                'stepBottom': 60,
                'validTime': [{'start': '06:30:00', 'end': '22:00:00'}],
            }
        return {'json': {'error': 10000, 'data': {
            'runMode': (body or {}).get('runMode'),
            'policy': 1 if model else 0,
            'runRuleModel': model,
        }}}


def test_rules_read_uses_the_semester_operate_type(monkeypatch, tmp_path):
    client = _RuleClient()
    monkeypatch.setattr(realclient, 'RealClient', lambda: client)
    api = RA.ReplicaApi('http://127.0.0.1:1', 'http://127.0.0.1:1', b'',
                        str(tmp_path))
    monkeypatch.setattr(api, '_real_account', lambda: _Acct())
    out = api.real_fetch_school_rules(1)
    assert out['code'] == 200, out
    assert out['data']['operate_type'] == 1
    assert out['data']['rules']['min_km'] == 1.0
    assert out['data']['rules']['min_minutes'] == 5.0
    assert out['data']['rules']['step_min'] == 60
    assert client.seen[0]['operateType'] == 1


def test_parse_track_drops_the_740_minus_one_placeholder():
    """A 7.4.0 point with no gLat/gLng must not be converted as BD-09."""
    assert readonly.parse_track([{'lat': -1.0, 'lng': -1.0}]) == []
    assert readonly.parse_track([
        {'gLat': 22.4110, 'gLng': 112.9410, 'lat': -1.0, 'lng': -1.0},
    ])[0]['crs'] == 'gcj02'
    # A -1 GCJ placeholder with a valid BD pair must fall back to BD.
    assert readonly.parse_track([
        {'gLat': -1.0, 'gLng': -1.0, 'lat': 22.4110, 'lng': 112.9410},
    ])[0]['crs'] == 'bd09->gcj02'

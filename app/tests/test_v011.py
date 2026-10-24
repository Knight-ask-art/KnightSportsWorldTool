"""v0.1.1 protocol/UI-adjacent unit tests (no network)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from replica.adapters import bridge_api as RA
from replica.application import points, policy, readonly, semester_goal
from replica.domain import safety


class PointsPathTest(unittest.TestCase):
    def test_distance_is_in_the_path(self):
        self.assertTrue(points.points_path(2500).endswith('/distance/2500'))
        self.assertTrue(points.points_path(2000.4).endswith('/distance/2000'))
        self.assertTrue(points.points_path(0).endswith('/distance/1'))
        self.assertTrue(points.points_path('bad').endswith('/distance/2000'))

    def test_cache_key_is_distance_scoped(self):
        a = points.cache_key('u', '1', None, 22.4, 112.9, 2000)
        b = points.cache_key('u', '1', None, 22.4, 112.9, 3000)
        self.assertNotEqual(a, b)
        self.assertIn('|d2000', a)
        self.assertIn('|d3000', b)


class SemesterViewTest(unittest.TestCase):
    def test_personal_fields_are_normalised(self):
        end = safety.now_ms() + 30 * 86400000
        view = RA.semester_view(
            {'semesterDis': 60000, 'semesterValidDis': 12000,
             'semesterCount': 30, 'semesterValidCount': 8},
            {'sname': '2026 秋', 'curValidDis': 12345, 'semesterGoal': 60,
             'semesterGoalCount': 30, 'semesterValidCount': 12,
             'endDate': end})
        self.assertEqual(view['sname'], '2026 秋')
        self.assertEqual(view['goal_km'], 60)
        self.assertEqual(view['done_km'], 12.35)
        self.assertEqual(view['remaining_km'], 47.65)
        self.assertEqual(view['goal_count'], 30)
        self.assertEqual(view['valid_count'], 12)
        self.assertEqual(view['remaining_count'], 18)
        self.assertIn(view['days_left'], (29, 30))
        self.assertEqual(view['source'], 'personal')

    def test_missing_values_stay_none(self):
        view = RA.semester_view({}, {})
        self.assertIsNone(view['goal_km'])
        self.assertIsNone(view['done_km'])
        self.assertIsNone(view['remaining_km'])
        self.assertIsNone(view['completed_pct'])
        self.assertIsNone(view['source'])

    def test_1000_metres_is_one_km(self):
        view = RA.semester_view({}, {'semesterGoal': 1000,
                                      'curValidDis': 1000})
        self.assertEqual(view['goal_km'], 1.0)
        self.assertEqual(view['done_km'], 1.0)

    def test_second_timestamps_are_normalised(self):
        # Some builds return startDate/endDate in seconds while others use ms.
        start_s = int((safety.now_ms() - 86400000) / 1000)
        end_s = int((safety.now_ms() + 30 * 86400000) / 1000)
        view = RA.semester_view({}, {'startDate': start_s, 'endDate': end_s})
        self.assertEqual(view['start_ms'], start_s * 1000)
        self.assertEqual(view['end_ms'], end_s * 1000)
        self.assertIn(view['days_left'], (29, 30))

    def test_distance_goal_uses_metres_even_below_one_km(self):
        view = RA.semester_view({}, {'semesterGoal': 500,
                                      'validTotalDis': 250,
                                      'goalMethod': 0})
        self.assertEqual(view['goal_unit'], 'km')
        self.assertEqual(view['goal_km'], 0.5)
        self.assertEqual(view['done_km'], 0.25)
        self.assertEqual(view['remaining_km'], 0.25)
        self.assertIsNone(view['goal_count'])

    def test_indoor_goal_is_kept_out_of_the_semester_goal(self):
        # Live shape: personal says indoor 0/1 次, summary says 10.24 km valid.
        view = RA.semester_view(
            {'semesterDis': 10667, 'semesterValidDis': 10240,
             'sname': '2026-2027-1体能训练Ⅲ',
             'semesterCount': 0, 'semesterValidCount': 0},
            {'sname': '2026-2027-1体能训练Ⅲ', 'semesterGoal': 1,
             'semesterValid': 0, 'goalMethod': 2})
        self.assertEqual(view['goal_kind'], 'indoor')
        self.assertEqual(view['goal_unit'], 'indoor')
        self.assertIsNone(view['goal_km'])
        self.assertIsNone(view['goal_count'])
        self.assertIsNone(view['valid_count'])
        self.assertEqual(view['outdoor_km'], 10.24)
        self.assertEqual(view['outdoor_total_km'], 10.67)
        # the real mileage must survive as the headline number
        self.assertEqual(view['done_km'], 10.24)
        self.assertEqual(view['indoor_goal'], 1)
        self.assertEqual(view['indoor_valid'], 0)
        self.assertEqual(view['indoor_remaining'], 1)

    def test_blank_summary_does_not_erase_the_mileage(self):
        # The old code called the summary endpoint without `sid`; it answered
        # an all-zero record and the card read "0.0%".
        view = RA.semester_view(
            {'semesterDis': 0, 'semesterValidDis': 0, 'sname': '',
             'semesterCount': 0, 'semesterValidCount': 0},
            {'sname': '2026-2027-1体能训练Ⅲ', 'semesterGoal': 1,
             'semesterValid': 0, 'goalMethod': 2})
        self.assertEqual(view['outdoor_km'], 0.0)
        self.assertEqual(view['sname'], '2026-2027-1体能训练Ⅲ')

    def test_semester_summary_sends_the_semester_id(self):
        class Client:
            def __init__(self):
                self.path = self.payload = None

            def request(self, acct, path, payload=None, retries=2):
                self.path, self.payload = path, payload
                return {'json': {'error': 10000, 'data': {'sname': 'x'}}}

        client = Client()
        out = readonly.semester_summary(client, object(), sid=9382)
        self.assertTrue(out['ok'])
        self.assertEqual(client.path, readonly.SEMESTER_SUMMARY_PATH)
        self.assertEqual(client.payload, {'sid': 9382})

    def test_semester_summary_omits_a_missing_semester_id(self):
        class Client:
            def __init__(self):
                self.payload = None

            def request(self, acct, path, payload=None, retries=2):
                self.payload = payload
                return {'json': {'error': 10000, 'data': {}}}

        client = Client()
        readonly.semester_summary(client, object())
        self.assertEqual(client.payload, {})


class SemesterGoalTest(unittest.TestCase):
    def test_local_target_fills_a_missing_outdoor_goal(self):
        view = RA.semester_view(
            {'semesterDis': 10667, 'semesterValidDis': 10240},
            {'sname': '体能训练', 'semesterGoal': 1, 'semesterValid': 0,
             'goalMethod': 2})
        RA.apply_semester_goal(view, 60)
        self.assertEqual(view['goal_km'], 60.0)
        self.assertEqual(view['goal_kind'], 'distance')
        self.assertEqual(view['done_km'], 10.24)
        self.assertEqual(view['remaining_km'], 49.76)
        self.assertEqual(view['goal_source'], 'local')
        # the indoor counter stays available for the note
        self.assertEqual(view['indoor_goal'], 1)

    def test_server_goal_beats_the_local_target(self):
        view = RA.semester_view({}, {'semesterGoal': 30000,
                                     'curValidDis': 12000,
                                     'goalMethod': 0})
        RA.apply_semester_goal(view, 60)
        self.assertEqual(view['goal_km'], 30.0)
        self.assertEqual(view['goal_source'], 'server')

    def test_no_target_and_no_goal_stays_unset(self):
        view = RA.semester_view({}, {'goalMethod': 2, 'semesterGoal': 1,
                                     'semesterValid': 0})
        RA.apply_semester_goal(view, None)
        self.assertIsNone(view['goal_km'])
        self.assertIsNone(view['goal_source'])

    def test_store_roundtrip_and_clear(self):
        with tempfile.TemporaryDirectory() as d:
            semester_goal.save(d, '60.5', sid=9382)
            got = semester_goal.load(d)
            self.assertEqual(got['target_km'], 60.5)
            self.assertEqual(got['sid'], 9382)
            semester_goal.save(d, '')
            self.assertIsNone(semester_goal.load(d)['target_km'])

    def test_target_from_another_semester_is_ignored(self):
        view = RA.semester_view(
            {'semesterValidDis': 10240},
            {'sid': 9382, 'semesterGoal': 1, 'semesterValid': 0,
             'goalMethod': 2})
        RA.apply_semester_goal(view, 60, target_sid=1111)
        self.assertIsNone(view['goal_km'])
        self.assertIsNone(view['goal_source'])

    def test_target_for_the_same_semester_applies(self):
        view = RA.semester_view(
            {'semesterValidDis': 10240},
            {'sid': 9382, 'semesterGoal': 1, 'semesterValid': 0,
             'goalMethod': 2})
        RA.apply_semester_goal(view, 60, target_sid='9382')
        self.assertEqual(view['goal_km'], 60.0)
        self.assertEqual(view['goal_source'], 'local')

    def test_store_rejects_nonsense(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                semester_goal.save(d, 'abc')
            with self.assertRaises(ValueError):
                semester_goal.save(d, 0)
            with self.assertRaises(ValueError):
                semester_goal.save(d, 99999)

    def test_corrupt_file_means_no_target(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'semester_goal.json'), 'w',
                      encoding='utf-8') as f:
                f.write('{not json')
            self.assertIsNone(semester_goal.load(d)['target_km'])

    def test_count_goal_method_one_uses_goal_count_fields(self):
        view = RA.semester_view({}, {'semesterGoalCount': 30,
                                      'validTotalCount': 12,
                                      'goalMethod': 1})
        self.assertEqual(view['goal_unit'], 'count')
        self.assertEqual(view['goal_count'], 30)
        self.assertEqual(view['valid_count'], 12)
        self.assertEqual(view['remaining_count'], 18)
        self.assertIsNone(view['goal_km'])

    def test_count_goal_method_one_prefers_personal_counts(self):
        view = RA.semester_view({'semesterValidCount': 99},
                                {'semesterGoalCount': 30,
                                 'validTotalCount': 12,
                                 'goalMethod': 1})
        self.assertEqual(view['goal_count'], 30)
        self.assertEqual(view['valid_count'], 12)
        self.assertEqual(view['remaining_count'], 18)


class RecordDetailFlagsTest(unittest.TestCase):
    def test_anti_cheat_flags_are_exposed(self):
        obj = {
            'rrid': 1, 'totalDis': 4000, 'totalTime': 1440, 'status': 0,
            'statusInfo': 'ok', 'complete': True, 'isCheat': 1, 'policy': 0,
            'stepFreqStatus': 1, 'speedTop': 2.17, 'speedBottom': 10.0,
            'stepBottom': 60, 'stepTop': 300, 'appealStatus': 4,
            'appealStatusInfo': '申诉未通过', 'reasonList': [],
        }

        class Client:
            def request(self, acct, path, payload=None, retries=2):
                return {'json': {'error': 10000, 'data': obj}}

        detail = readonly.record_detail(Client(), object(), 1)['detail']
        self.assertEqual(detail['is_cheat'], 1)
        self.assertEqual(detail['policy'], 0)
        self.assertEqual(detail['step_freq_status'], 1)
        self.assertEqual(detail['pace_fast'], 2.17)
        self.assertEqual(detail['pace_slow'], 10.0)
        self.assertEqual(detail['step_min'], 60)
        self.assertEqual(detail['step_max'], 300)
        self.assertEqual(detail['appeal_status'], 4)
        self.assertEqual(detail['appeal_info'], '申诉未通过')


class PlanHorizonTest(unittest.TestCase):
    def test_plan_days_are_clamped(self):
        self.assertEqual(
            RA.ReplicaApi.real_plan(object(), days=999)['data']['days'], 60)
        self.assertEqual(
            RA.ReplicaApi.real_plan(object(), days=0)['data']['days'], 1)
        self.assertEqual(
            RA.ReplicaApi.real_plan(object(), days='oops')['data']['days'], 14)

    def test_plan_slots_match_the_requested_horizon(self):
        out = RA.ReplicaApi.real_plan(object(), days=7, seed=1)['data']
        self.assertEqual(len(out['slots']), 7)


class ReceiptMatchTest(unittest.TestCase):
    def test_start_and_distance_match(self):
        rec = {'rrid': 9, 'start_ms': 1_000_000, 'distance_m': 2480}
        got = RA._receipt_match(
            {'rrid': None, 'start': 1_060_000, 'distance_m': 2500}, [rec])
        self.assertIs(got, rec)

    def test_distance_mismatch_is_not_a_match(self):
        rec = {'rrid': 9, 'start_ms': 1_000_000, 'distance_m': 1500}
        got = RA._receipt_match(
            {'rrid': None, 'start': 1_000_000, 'distance_m': 2500}, [rec])
        self.assertIsNone(got)

    def test_explicit_rrid_wins(self):
        rec = {'rrid': 42, 'start_ms': 1, 'distance_m': 1}
        got = RA._receipt_match(
            {'rrid': 42, 'start': 999999, 'distance_m': 2500}, [rec])
        self.assertIs(got, rec)


class RouteCleanTest(unittest.TestCase):
    def test_line_is_validated(self):
        got = RA._clean_route({'name': 'x', 'type': 'auto',
                               'line': [[22.4, 112.9], {'lat': 22.5, 'lon': 113.0}]})
        self.assertEqual(len(got['line']), 2)
        self.assertEqual(got['type'], 'auto')

    def test_bad_line_is_dropped(self):
        self.assertIsNone(RA._clean_route({'line': [[22.4, 200]]}))
        self.assertIsNone(RA._clean_route({'line': []}))


class ReceiptsBridgeTest(unittest.TestCase):
    def test_receipts_are_read_from_the_store(self):
        from replica.application.guard import ReceiptStore
        old = os.environ.get('JHP_DATA_DIR')
        tmp = tempfile.mkdtemp(prefix='jhp_v011_')
        try:
            api = RA.ReplicaApi('http://example.invalid', 'http://example.invalid',
                                b'', tmp)
            with open(os.path.join(tmp, 'accounts_emu.json'), 'w',
                      encoding='utf-8') as f:
                json.dump({'uid': '1', 'token': 'test', 'unid': 47203,
                           'schoolName': '广州软件学院',
                           'campusName': '江门校区'}, f)
            store = ReceiptStore(os.path.join(tmp, 'submission_receipts'))
            store.reserve('1', 'uuid-1', 1_000_000, 1_600_000,
                          school='47203', distance_m=2500)
            store.mark('1', 'uuid-1', 'unknown', school='47203')
            got = api.real_receipts()
            self.assertEqual(got['code'], 200)
            self.assertEqual(len(got['data']['receipts']), 1)
            self.assertEqual(got['data']['receipts'][0]['state'], 'unknown')
            self.assertEqual(got['data']['receipts'][0]['distance_m'], 2500)

            with mock.patch('replica.transport.realclient.RealClient',
                            return_value=object()), \
                 mock.patch.object(readonly, 'recent_records',
                                   return_value={'ok': True, 'records': [{
                                       'rrid': 99, 'start_ms': 1_000_000,
                                       'distance_m': 2500,
                                   }]}):
                rec = api.real_reconcile_receipts()
            self.assertEqual(rec['code'], 200)
            self.assertEqual(len(rec['data']['matched']), 1)
            self.assertEqual(api.real_receipts()['data']['receipts'][0]['state'],
                             'accepted')
        finally:
            if old is None:
                os.environ.pop('JHP_DATA_DIR', None)
            else:
                os.environ['JHP_DATA_DIR'] = old


class BridgeIntegrationTest(unittest.TestCase):
    def _api(self):
        old = os.environ.get('JHP_DATA_DIR')
        self.addCleanup(lambda: (os.environ.__setitem__('JHP_DATA_DIR', old)
                                 if old is not None else
                                 os.environ.pop('JHP_DATA_DIR', None)))
        tmp = tempfile.mkdtemp(prefix='jhp_v011_bridge_')
        api = RA.ReplicaApi('http://example.invalid', 'http://example.invalid',
                            b'', tmp)
        with open(os.path.join(tmp, 'accounts_emu.json'), 'w',
                  encoding='utf-8') as f:
            json.dump({'uid': '1', 'token': 'test', 'unid': 47203,
                       'schoolName': '广州软件学院',
                       'campusName': '江门校区'}, f)
        return api

    def test_semester_bridge(self):
        api = self._api()
        with mock.patch.object(readonly, 'semester_summary',
                               return_value={'ok': True, 'data': {
                                   'semesterDis': 60000,
                                   'semesterValidDis': 12000}}), \
             mock.patch.object(readonly, 'semester_personal',
                               return_value={'ok': True, 'data': {
                                   'sname': '2026 秋', 'curValidDis': 12345,
                                   'semesterGoal': 60, 'semesterGoalCount': 30,
                                   'semesterValidCount': 12}}):
            got = api.real_semester()
        self.assertEqual(got['code'], 200)
        self.assertEqual(got['data']['view']['sname'], '2026 秋')
        self.assertEqual(got['data']['view']['done_km'], 12.35)

    def test_profile_bridge_merges_user_info_and_semester(self):
        api = self._api()
        with mock.patch.object(RA.ReplicaApi, 'real_check_session',
                               return_value=RA._ok({
                                   'uid': '1', 'name': '张三',
                                   'nickname': '小张',
                                   'schoolName': '广州软件学院',
                                   'campusName': '江门校区',
                                   'gradeClass': '2024 级 1 班',
                                   'unid': 47203, 'height': 175,
                                   'weight': 68})), \
             mock.patch.object(readonly, 'semester_summary',
                               return_value={'ok': True, 'data': {
                                   'semesterDis': 60000}}), \
             mock.patch.object(readonly, 'semester_personal',
                               return_value={'ok': True, 'data': {
                                   'sname': '2026 秋', 'curValidDis': 12345,
                                   'semesterGoal': 60}}):
            got = api.real_profile()
        self.assertEqual(got['code'], 200)
        p = got['data']['profile']
        self.assertEqual(p['gradeClass'], '2024 级 1 班')
        self.assertTrue(p['verified'])
        self.assertEqual(got['data']['semester']['view']['done_km'], 12.35)

    def test_profile_falls_back_to_session_when_user_info_fails(self):
        api = self._api()
        session = {'uid': '1', 'token': 'test', 'unid': 47203,
                   'name': '本地名', 'schoolName': '广州软件学院',
                   'campusName': '江门校区'}
        with mock.patch.object(api, '_read_session', return_value=session), \
             mock.patch.object(RA.ReplicaApi, 'real_check_session',
                               return_value=RA._fail('HTTP 500')), \
             mock.patch.object(readonly, 'semester_summary',
                               return_value={'ok': False, 'error': 'boom'}), \
             mock.patch.object(readonly, 'semester_personal',
                               return_value={'ok': False, 'error': 'boom'}):
            got = api.real_profile()
        self.assertEqual(got['code'], 200)
        self.assertFalse(got['data']['profile']['verified'])
        self.assertEqual(got['data']['profile']['name'], '本地名')
        self.assertIsNone(got['data']['semester'])
        self.assertIn('学期', ' '.join(got['data']['errors']))

    def test_violations_bridge_exposes_unknown_and_staleness(self):
        api = self._api()
        with mock.patch.object(readonly, 'check_violations',
                               return_value={'state': 'unknown',
                                             'message': '缺字段',
                                             'records': [],
                                             'checked_at': 0}):
            got = api.real_violations()
        self.assertEqual(got['code'], 200)
        self.assertEqual(got['data']['state'], 'unknown')
        self.assertTrue(got['data']['stale'])

    def test_violations_bridge_lists_records(self):
        api = self._api()
        rec = {'id': 1, 'rrid': 42, 'reason': '原因',
               'time': 1761000000000}
        with mock.patch.object(readonly, 'check_violations',
                               return_value={'state': 'violations',
                                             'message': '存在违规记录',
                                             'records': [rec],
                                             'checked_at': safety.now_ms()}):
            got = api.real_violations()
        self.assertEqual(got['code'], 200)
        self.assertEqual(got['data']['count'], 1)
        self.assertEqual(got['data']['records'][0]['rrid'], 42)
        self.assertFalse(got['data']['stale'])

    def test_school_rules_bridge_maps_run_rule_model(self):
        api = self._api()
        data = {'runRuleModel': {
            'minDistance': 2000, 'minRunTime': 600, 'maxRunTime': 5400,
            'speedTop': 5.5, 'speedBottom': 8.0,
            'stepTop': 200, 'stepBottom': 150,
            'validTime': [{'start': '06:30:00', 'end': '22:00:00'}],
        }}
        with mock.patch('replica.transport.realclient.RealClient',
                        return_value=object()), \
             mock.patch.object(policy, 'fetch_policy', return_value=data):
            got = api.real_fetch_school_rules()
        self.assertEqual(got['code'], 200)
        self.assertEqual(got['data']['run_mode'], 1)
        self.assertEqual(got['data']['rules']['min_km'], 2.0)
        self.assertEqual(got['data']['rules']['step_min'], 150)
        self.assertEqual(got['data']['rules']['step_max'], 200)
        self.assertEqual(got['data']['rules']['window_start'], '06:30')
        self.assertEqual(got['data']['rules']['window_end'], '22:00')

    def test_school_rules_bridge_falls_back_to_second_mode(self):
        api = self._api()
        data = {'runRuleModel': {'minDistance': 1500,
                                 'validTime': [{'start': '07:00',
                                                'end': '21:00'}]}}

        def fake_fetch(client, acct, run_mode=1, **kw):
            return {} if run_mode == 1 else data

        with mock.patch('replica.transport.realclient.RealClient',
                        return_value=object()), \
             mock.patch.object(policy, 'fetch_policy', side_effect=fake_fetch):
            got = api.real_fetch_school_rules()
        self.assertEqual(got['code'], 200)
        self.assertEqual(got['data']['run_mode'], 2)
        self.assertEqual(got['data']['rules']['min_km'], 1.5)

    def test_empty_semester_is_a_failure(self):
        api = self._api()
        with mock.patch.object(readonly, 'semester_summary',
                               return_value={'ok': True, 'data': {}}), \
             mock.patch.object(readonly, 'semester_personal',
                               return_value={'ok': True, 'data': {}}):
            got = api.real_semester()
        self.assertEqual(got['code'], 500)
        self.assertIn('学期数据读取失败', got['message'])

    def test_daily_counts_bridge(self):
        api = self._api()
        today = safety.to_beijing(safety.now_ms()).date()
        day = today.isoformat()
        with mock.patch.object(readonly, 'daily_counts',
                               return_value={'ok': True, 'source': 'server',
                                             'counts': {day: 2}}):
            got = api.real_daily_counts([day])
        self.assertEqual(got['code'], 200)
        self.assertEqual(got['data']['dates'][0]['date'], day)
        self.assertEqual(got['data']['dates'][0]['runs'], 2)


class DailyCountsTest(unittest.TestCase):
    def test_counts_group_one_response(self):
        rows = [
            {'date': '2026-10-23'}, {'date': '2026-10-23'},
            {'date': '2026-10-22'}, {'date': '2026-10-20'},
        ]
        with mock.patch.object(readonly, 'recent_records',
                               return_value={'ok': True, 'records': rows}):
            got = readonly.daily_counts(None, None, [
                '2026-10-23', '2026-10-22', '2026-10-21', '2026-10-20'])
        self.assertTrue(got['ok'])
        self.assertEqual(got['counts']['2026-10-23'], 2)
        self.assertEqual(got['counts']['2026-10-22'], 1)
        self.assertEqual(got['counts']['2026-10-21'], 0)
        self.assertEqual(got['counts']['2026-10-20'], 1)

    def test_failure_stays_unknown(self):
        with mock.patch.object(readonly, 'recent_records',
                               return_value={'ok': False, 'error': 'x'}):
            got = readonly.daily_counts(None, None, ['2026-10-23'])
        self.assertFalse(got['ok'])
        self.assertIsNone(got['counts']['2026-10-23'])


if __name__ == '__main__':
    unittest.main()

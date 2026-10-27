"""The submit gate must use the server's current band, not a stale cache.

``school_rules.json`` is written once, when the settings page is first opened,
and never refreshed.  If a school changes its pace/step range afterwards, the
qualification gate keeps checking the old numbers -- or, on an install that
never opened that page, checks nothing at all while the payload still claims
``useMobilityTools = 0``.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from replica.application import policy

POLICY = {'policy': 1, 'runRuleModel': {
    'minDistance': 2000, 'minRunTime': 600, 'maxRunTime': 5400,
    'speedTop': 5.5, 'speedBottom': 8.0, 'stepBottom': 150, 'stepTop': 200,
}}


def test_server_band_overwrites_a_stale_cache():
    merged, changed = policy.merge_rules({'pace_min': 4.0, 'pace_max': 9.0},
                                         POLICY)
    assert changed is True
    assert merged['pace_min'] == 5.5
    assert merged['pace_max'] == 8.0
    assert merged['step_min'] == 150
    assert merged['min_km'] == 2.0


def test_manual_fields_the_server_does_not_publish_survive():
    merged, changed = policy.merge_rules({'max_per_day': 2, 'pace_min': 4.0},
                                         POLICY)
    assert changed is True
    assert merged['max_per_day'] == 2


def test_explicit_opt_out_is_respected():
    cached = {'auto_fetch': False, 'pace_min': 4.0}
    merged, changed = policy.merge_rules(cached, POLICY)
    assert changed is False
    assert merged == cached


def test_server_without_a_rule_model_changes_nothing():
    cached = {'pace_min': 6.0}
    merged, changed = policy.merge_rules(cached, {'policy': 1})
    assert changed is False
    assert merged == cached


def test_missing_policy_data_is_not_an_error():
    # policy fetch failed: submission continues on the cached copy.
    cached = {'pace_min': 6.0}
    merged, changed = policy.merge_rules(cached, None)
    assert changed is False
    assert merged == cached

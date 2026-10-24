"""v0.1.2: version drift and the soft plausibility band."""
import re
from pathlib import Path

import pytest

from replica.adapters.bridge_api import (APP_VERSION, PLAUSIBLE_CADENCE_MAX,
                                        PLAUSIBLE_CADENCE_MIN,
                                        PLAUSIBLE_PACE_MAX, PLAUSIBLE_PACE_MIN,
                                        plausibility_warnings)
from replica.domain.rundata import generate_run_data

ROOT = Path(__file__).resolve().parents[1]


def test_frontend_version_matches_app_version():
    html = (ROOT / 'frontend' / 'index.html').read_text(encoding='utf-8')
    assert f'v{APP_VERSION}' in html
    assert 'v0.1.1' not in html
    found = set(re.findall(r'v(\d+\.\d+\.\d+)', html))
    assert found == {APP_VERSION}, found


def test_band_edges_do_not_warn():
    assert plausibility_warnings(PLAUSIBLE_PACE_MIN, PLAUSIBLE_CADENCE_MIN) == []
    assert plausibility_warnings(PLAUSIBLE_PACE_MAX, PLAUSIBLE_CADENCE_MAX) == []
    assert plausibility_warnings(6.0, 172) == []


def test_the_verified_ios_run_would_warn_but_not_fail():
    # The accepted 2026-10-23 run: 9.40 min/km, 117 spm.  It must stay
    # submittable; the band is a reminder, not a gate.
    warnings = plausibility_warnings(9.40, 117)
    assert len(warnings) == 2
    assert '配速' in warnings[0] and '步频' in warnings[1]


def test_invalid_inputs_are_silent():
    assert plausibility_warnings(None, None) == []
    assert plausibility_warnings(0, 0) == []


def test_closed_route_span_larger_than_target_is_rejected():
    wps = [(22.4109, 112.9407), (22.4052, 112.9338),
           (22.4048, 112.9346), (22.4038, 112.9341)]
    with pytest.raises(ValueError, match='超过目标距离'):
        generate_run_data(1.0, 6.0, 172, waypoints=wps, closed=True)


def test_open_route_longer_than_target_is_rejected():
    wps = [(22.4046, 112.9342), (22.4060, 112.9342), (22.4070, 112.9342)]
    with pytest.raises(ValueError, match='超过目标距离'):
        generate_run_data(0.1, 6.0, 172, waypoints=wps, closed=False)

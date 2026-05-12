"""End-to-end check that cross-midnight journeys produce a two-day SOC profile.

Bypasses ``selection.py`` and feeds a synthetic journey directly to
``simulate_coach_journey`` so the multi-day ``_split_trip`` branch and the
``sim_adapter`` day-offset stitching are exercised together.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from mobility.coach.sim_adapter import simulate_coach_journey
from mobility.core.constants import STEPS_PER_DAY_DECISION


STEPS_PER_DAY = STEPS_PER_DAY_DECISION
STEP_PER_HOUR = STEPS_PER_DAY / 24


def _step_index(hour: float) -> int:
    return int(round(hour * STEP_PER_HOUR))


def test_cross_midnight_journey_produces_two_day_continuous_soc() -> None:
    journey = {
        "vehicle_journey_code": "VJ_CROSS",
        "start_h": 22.0,
        "end_h": 26.0,
        "distance_km": 200.0,
        "distance_source": "haversine_x_detour",
    }
    stops = pd.DataFrame({"stop_sequence": [1, 2], "stop_point_ref": ["A", "B"]})
    ev = {"Model": "YUTONG TC12", "Energy_kWh": 281.0, "consumption_kwh_per_km": 0.9}

    result = simulate_coach_journey(
        journey,
        stops,
        ev,
        terminus_charge_kw=50.0,
        soc_init=1.0,
    )

    soc = np.asarray(result["soc"], dtype=float)
    load_kw = np.asarray(result["load_kw"], dtype=float)

    assert len(load_kw) == 2 * STEPS_PER_DAY
    assert len(soc) == 2 * STEPS_PER_DAY

    day0_tail_start = _step_index(22.0)
    day1_head_end = STEPS_PER_DAY + _step_index(2.0)

    day0_drop = soc[day0_tail_start] - soc[STEPS_PER_DAY - 1]
    day1_drop = soc[STEPS_PER_DAY] - soc[day1_head_end - 1]
    assert day0_drop > 0.0, "expected SoC to fall during the day=0 tail of the trip"
    assert day1_drop > 0.0, "expected SoC to fall during the day=1 head of the trip"

    soc_step_diffs = np.abs(np.diff(soc))
    assert soc_step_diffs.max() < 0.25, "SoC trajectory should be continuous (no jumps)"

    seam_jump = abs(soc[STEPS_PER_DAY] - soc[STEPS_PER_DAY - 1])
    assert seam_jump < 0.25, "SoC should be continuous across the day=0 -> day=1 seam"

    schedules = result["schedules"]
    assert [schedule.day for schedule in schedules] == [0, 1]
    assert sum(trip.energy_consumed_kwh for trip in schedules[0].trips) > 0.0
    assert sum(trip.energy_consumed_kwh for trip in schedules[1].trips) > 0.0

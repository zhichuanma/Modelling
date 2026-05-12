"""Task 4 gate: ``soc_init=None`` auto-derives from ``pre_journey_dwell_h``."""
from __future__ import annotations

import pandas as pd

from mobility.coach.sim_adapter import simulate_coach_journey


_STOPS = pd.DataFrame({"stop_sequence": [1, 2], "stop_point_ref": ["A", "B"]})


def _journey() -> dict:
    return {
        "vehicle_journey_code": "VJ_DWELL",
        "start_h": 8.0,
        "end_h": 10.0,
        "distance_km": 60.0,
        "distance_source": "haversine_x_detour",
    }


def test_soc_init_default_derives_from_pre_dwell_window() -> None:
    ev = {"Model": "Coach", "Energy_kWh": 600.0, "consumption_kwh_per_km": 0.9}

    result = simulate_coach_journey(
        _journey(),
        _STOPS,
        ev,
        terminus_charge_kw=50.0,
        pre_journey_dwell_h=6.0,
    )

    expected = 1.0 - (6.0 * 50.0 / 600.0)
    assert result["soc_after_warmup"] == expected


def test_soc_init_default_clips_to_zero_when_dwell_overfills() -> None:
    ev = {"Model": "Coach", "Energy_kWh": 100.0, "consumption_kwh_per_km": 0.9}

    result = simulate_coach_journey(
        _journey(),
        _STOPS,
        ev,
        terminus_charge_kw=50.0,
        pre_journey_dwell_h=6.0,
    )

    assert result["soc_after_warmup"] == 0.0


def test_explicit_soc_init_is_respected() -> None:
    ev = {"Model": "Coach", "Energy_kWh": 281.0, "consumption_kwh_per_km": 0.9}

    result = simulate_coach_journey(
        _journey(),
        _STOPS,
        ev,
        terminus_charge_kw=50.0,
        soc_init=0.4,
    )

    assert result["soc_after_warmup"] == 0.4

from __future__ import annotations

import pandas as pd

from mobility.coach.sim_adapter import simulate_coach_journey


def test_simulate_coach_journey_feasible_case() -> None:
    row = pd.Series(
        {
            "vehicle_journey_code": "VJ1",
            "start_h": 8.0,
            "end_h": 10.0,
            "distance_km": 80.0,
            "distance_source": "haversine_x_detour",
        }
    )
    stops = pd.DataFrame({"stop_sequence": [1, 2], "stop_point_ref": ["A", "B"]})
    ev = {"Model": "YUTONG TC12", "Energy_kWh": 281.0, "consumption_kwh_per_km": 0.9}

    result = simulate_coach_journey(row, stops, ev, terminus_charge_kw=50.0, soc_init=0.4)

    assert result["feasibility"]["feasible_single_charge"] is True
    assert result["soc_min"] > 0.0
    assert result["soc_clamped_to_zero"] is False
    assert result["terminus_kwh"] > 0.0
    assert abs(result["terminus_kwh"] - result["energy_charged_kwh"]) < 1e-6

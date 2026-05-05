from __future__ import annotations

import pandas as pd

from mobility.coach.sim_adapter import simulate_coach_journey


def test_simulate_coach_journey_infeasible_is_explicit() -> None:
    row = pd.Series(
        {
            "vehicle_journey_code": "VJ2",
            "start_h": 8.0,
            "end_h": 14.0,
            "distance_km": 260.0,
            "distance_source": "haversine_x_detour",
        }
    )
    stops = pd.DataFrame({"stop_sequence": [1, 2], "stop_point_ref": ["A", "B"]})
    ev = {"Model": "Small Coach", "Energy_kWh": 100.0, "consumption_kwh_per_km": 1.0}

    result = simulate_coach_journey(row, stops, ev, terminus_charge_kw=0.0)

    assert result["feasibility"]["feasible_single_charge"] is False
    assert result["feasibility"]["shortfall_kwh"] > 0.0
    assert result["soc_clamped_to_zero"] is True
    assert result["soc_floor_hit_h"] is not None

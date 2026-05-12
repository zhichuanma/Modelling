"""Regression coverage for the infeasible-journey failure semantics."""
from __future__ import annotations

import pandas as pd

from mobility.coach.feasibility import journey_feasibility
from mobility.coach.sim_adapter import simulate_coach_journey


def test_simulate_coach_journey_infeasible_failure_semantics() -> None:
    journey_end_h = 20.0
    journey = {
        "vehicle_journey_code": "VJ_INFEASIBLE",
        "start_h": 8.0,
        "end_h": journey_end_h,
        "distance_km": 500.0,
        "distance_source": "haversine_x_detour",
    }
    stops = pd.DataFrame({"stop_sequence": [1, 2], "stop_point_ref": ["A", "B"]})
    ev = {"Model": "Tiny Coach", "Energy_kWh": 50.0, "consumption_kwh_per_km": 1.0}

    result = simulate_coach_journey(
        journey,
        stops,
        ev,
        terminus_charge_kw=0.0,
        soc_init=1.0,
    )

    assert result["soc_clamped_to_zero"] is True
    soc_floor_hit_h = result["soc_floor_hit_h"]
    assert soc_floor_hit_h is not None
    assert soc_floor_hit_h > 0.0
    assert soc_floor_hit_h < journey_end_h

    feasibility = result["feasibility"]
    assert feasibility["feasible_single_charge"] is False
    assert feasibility["min_soc_required"] > 0.0

    direct = journey_feasibility(
        distance_km=float(journey["distance_km"]),
        battery_kwh=float(ev["Energy_kWh"]),
        consumption_kwh_per_km=float(ev["consumption_kwh_per_km"]),
    )
    assert direct["feasible_single_charge"] is False
    assert direct["min_soc_required"] > 0.0

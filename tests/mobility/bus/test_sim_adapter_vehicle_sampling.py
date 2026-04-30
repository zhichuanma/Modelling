from __future__ import annotations

import numpy as np
import pandas as pd

from mobility.bus.sim_adapter import simulate_fleet_blocks
from mobility.core.constants import STEPS_PER_DAY_DECISION


def _block(block_id: str, hour: float) -> dict:
    return {
        "trip_id": f"{block_id}_trip",
        "agency_id": "OP",
        "route_id": "R1",
        "service_id": "S1",
        "direction_id": 0,
        "block_id": block_id,
        "block_source": "native",
        "start_h": hour,
        "end_h": hour + 1.0,
        "distance_km": 5.0,
        "start_stop": f"{block_id}_A",
        "end_stop": f"{block_id}_B",
        "start_lat": 51.0,
        "start_lon": -1.0,
        "end_lat": 51.1,
        "end_lon": -1.1,
        "shape_id": "shape",
    }


def test_simulate_fleet_blocks_can_sample_vehicle_params_per_block() -> None:
    blocks = pd.DataFrame([_block(f"block_{idx}", 8.0 + idx) for idx in range(6)])
    vehicle_params = pd.DataFrame(
        {
            "make": ["A", "B"],
            "gen_model": ["Alpha", "Beta"],
            "stock_2025_q2": [1.0, 1.0],
            "battery_kwh": [200.0, 500.0],
            "consumption_kwh_per_km": [0.7, 1.1],
            "depot_charge_kw": [80.0, 150.0],
        }
    )

    per_block, fleet_load_kw = simulate_fleet_blocks(
        blocks,
        vehicle_params=vehicle_params,
        vehicle_rng=np.random.default_rng(20260430),
    )

    assert fleet_load_kw.shape == (STEPS_PER_DAY_DECISION,)
    assert {"vehicle_make", "vehicle_gen_model", "battery_kwh", "consumption_kwh_per_km"}.issubset(per_block.columns)
    assert per_block["battery_kwh"].nunique() > 1
    assert per_block["total_consumed_kwh"].nunique() > 1

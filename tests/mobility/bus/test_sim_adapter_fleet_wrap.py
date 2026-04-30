from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from mobility.bus.sim_adapter import simulate_block, simulate_fleet_blocks
from mobility.core.constants import STEP_HOURS_DECISION, STEPS_PER_DAY_DECISION


def _block_row(block_id: str, trip_id: str, start_h: float, end_h: float, distance_km: float):
    return {
        "trip_id": trip_id,
        "agency_id": "OP",
        "route_id": "R1",
        "service_id": "S1",
        "direction_id": 0,
        "block_id": block_id,
        "block_source": "native",
        "start_h": start_h,
        "end_h": end_h,
        "distance_km": distance_km,
        "start_stop": f"{trip_id}_A",
        "end_stop": f"{trip_id}_B",
        "start_lat": 51.0,
        "start_lon": -1.0,
        "end_lat": 51.1,
        "end_lon": -1.1,
        "shape_id": "shape",
    }


def _wrap_load(load_kw: np.ndarray) -> np.ndarray:
    wrapped = np.zeros(STEPS_PER_DAY_DECISION, dtype=float)
    for start in range(0, len(load_kw), STEPS_PER_DAY_DECISION):
        chunk = load_kw[start : start + STEPS_PER_DAY_DECISION]
        wrapped[: len(chunk)] += chunk
    return wrapped


def test_fleet_load_wraps_cross_midnight_tail_to_representative_day() -> None:
    df = pd.DataFrame(
        [
            _block_row("single_day", "t0", 8.0, 17.0, 10.0),
            _block_row("cross_midnight", "t1", 23.0, 25.0, 40.0),
        ]
    )
    kwargs = {
        "battery_kwh": 100.0,
        "consumption_kwh_per_km": 1.0,
        "depot_charge_kw": 50.0,
        "soc_init": 1.0,
    }

    per_block, fleet_load_kw = simulate_fleet_blocks(df, **kwargs)
    single = simulate_block(df[df["block_id"] == "single_day"], **kwargs)
    cross = simulate_block(df[df["block_id"] == "cross_midnight"], **kwargs)
    expected_fleet = _wrap_load(single["load_kw"]) + _wrap_load(cross["load_kw"])

    assert fleet_load_kw.shape == (STEPS_PER_DAY_DECISION,)
    assert np.allclose(fleet_load_kw, expected_fleet)
    assert fleet_load_kw[4:8].sum() > 0.0
    assert float(fleet_load_kw.sum() * STEP_HOURS_DECISION) == pytest.approx(
        single["energy_charged_kwh"] + cross["energy_charged_kwh"]
    )
    assert float(per_block["energy_charged_kwh"].sum()) == pytest.approx(
        single["energy_charged_kwh"] + cross["energy_charged_kwh"]
    )

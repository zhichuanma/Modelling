from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from mobility.bus.annual_simulation import simulate_block_year


def _block() -> pd.DataFrame:
    return pd.DataFrame(
        [("t0", "OP", "R1", "S1", 0, "B1", "native", 8.0, 18.0, 100.0, "A", "B", 51.0, -1.0, 51.1, -1.1, "shape")],
        columns=[
            "trip_id", "agency_id", "route_id", "service_id", "direction_id", "block_id",
            "block_source", "start_h", "end_h", "distance_km", "start_stop", "end_stop",
            "start_lat", "start_lon", "end_lat", "end_lon", "shape_id",
        ],
    )


def test_simulate_block_year_threads_soc_and_inactive_charging() -> None:
    result = simulate_block_year(
        _block(),
        [dt.date(2026, 4, 17), dt.date(2026, 4, 19)],
        {"battery_kwh": 120.0, "consumption_kwh_per_km": 1.0, "depot_charge_kw": 60.0},
        "2026-04-17",
        "2026-04-19",
        soc_init=1.0,
    )

    assert result["load_matrix_kw"].shape == (3, 96)
    assert result["active_days"] == 2
    assert result["annual_distance_km"] == pytest.approx(200.0)
    assert result["annual_energy_kwh"] == pytest.approx(200.0)
    assert result["soc_min"] < result["soc_end"]
    assert result["depot_kwh"] > 0.0

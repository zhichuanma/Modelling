from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from mobility.bus.annual_simulation import simulate_fleet_year


def _blocks() -> pd.DataFrame:
    rows = []
    for block_id, service_id, distance in [("B1", "S1", 20.0), ("B2", "S2", 30.0)]:
        rows.append(
            (f"{block_id}_t0", "OP", "R1", service_id, 0, block_id, "native", 8.0, 9.0, distance, "A", "B", 51.0, -1.0, 51.1, -1.1, "shape")
        )
    return pd.DataFrame(
        rows,
        columns=[
            "trip_id", "agency_id", "route_id", "service_id", "direction_id", "block_id",
            "block_source", "start_h", "end_h", "distance_km", "start_stop", "end_stop",
            "start_lat", "start_lon", "end_lat", "end_lon", "shape_id",
        ],
    )


def _vehicle_params() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "make": ["A", "B"],
            "gen_model": ["Alpha", "Beta"],
            "stock_2025_q2": [10.0, 90.0],
            "battery_kwh": [120.0, 200.0],
            "consumption_kwh_per_km": [1.0, 1.2],
            "depot_charge_kw": [50.0, 70.0],
        }
    )


def test_simulate_fleet_year_samples_once_per_block_and_is_seeded() -> None:
    service_dates = {"S1": (dt.date(2026, 4, 17),), "S2": (dt.date(2026, 4, 18),)}
    rng_a = np.random.default_rng(20260505)
    rng_b = np.random.default_rng(20260505)

    per_a, load_a = simulate_fleet_year(
        _blocks(),
        service_dates,
        vehicle_params=_vehicle_params(),
        vehicle_rng=rng_a,
        start_date="2026-04-17",
        end_date="2026-04-19",
    )
    per_b, load_b = simulate_fleet_year(
        _blocks(),
        service_dates,
        vehicle_params=_vehicle_params(),
        vehicle_rng=rng_b,
        start_date="2026-04-17",
        end_date="2026-04-19",
    )

    pd.testing.assert_frame_equal(per_a, per_b)
    assert np.allclose(load_a, load_b)
    assert load_a.shape == (3, 96)
    assert per_a["vehicle_gen_model"].notna().all()

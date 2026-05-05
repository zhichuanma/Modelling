from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from mobility.bus.annual_simulation import simulate_fleet_year


def _blocks() -> pd.DataFrame:
    rows = []
    for block_id in ("B1", "B2", "B3"):
        rows.append(
            (
                f"{block_id}_t0",
                "OP",
                "R1",
                "S1",
                0,
                block_id,
                "native",
                8.0,
                9.0,
                20.0,
                "A",
                "B",
                51.0,
                -1.0,
                51.1,
                -1.1,
                "shape",
            )
        )
    return pd.DataFrame(
        rows,
        columns=[
            "trip_id",
            "agency_id",
            "route_id",
            "service_id",
            "direction_id",
            "block_id",
            "block_source",
            "start_h",
            "end_h",
            "distance_km",
            "start_stop",
            "end_stop",
            "start_lat",
            "start_lon",
            "end_lat",
            "end_lon",
            "shape_id",
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


def test_vehicle_is_sampled_once_per_block_for_long_fleet_run() -> None:
    start = dt.date(2026, 4, 17)
    dates = tuple(start + dt.timedelta(days=offset) for offset in range(100))

    per_block, _ = simulate_fleet_year(
        _blocks(),
        {"S1": dates},
        vehicle_params=_vehicle_params(),
        vehicle_rng=np.random.default_rng(20260505),
        start_date=dates[0],
        end_date=dates[-1],
    )

    assert per_block.index.is_unique
    assert per_block["vehicle_gen_model"].notna().all()
    assert per_block.groupby(level=0)["vehicle_gen_model"].nunique().eq(1).all()

from __future__ import annotations

import datetime as dt

import pandas as pd

from mobility.bus.annual_simulation import simulate_fleet_year


def _cross_midnight_block() -> pd.DataFrame:
    return pd.DataFrame(
        [
            (
                "tail",
                "OP",
                "R1",
                "S1",
                0,
                "B1",
                "native",
                23.0,
                25.0,
                40.0,
                "A",
                "B",
                51.0,
                -1.0,
                51.1,
                -1.1,
                "shape",
            )
        ],
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


def test_cross_midnight_tail_charging_accumulates_on_next_fleet_day() -> None:
    per_block, fleet_load_kw = simulate_fleet_year(
        _cross_midnight_block(),
        {"S1": (dt.date(2026, 4, 17),)},
        battery_kwh=100.0,
        consumption_kwh_per_km=1.0,
        depot_charge_kw=50.0,
        start_date="2026-04-17",
        end_date="2026-04-18",
    )

    assert per_block.loc["B1", "annual_distance_km"] == 40.0
    assert fleet_load_kw[0].sum() == 0.0
    assert fleet_load_kw[1].sum() > 0.0

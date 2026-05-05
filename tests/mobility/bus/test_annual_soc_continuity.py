from __future__ import annotations

import datetime as dt

import pandas as pd

from mobility.bus.annual_simulation import simulate_block_year


def _block() -> pd.DataFrame:
    return pd.DataFrame(
        [
            (
                "t0",
                "OP",
                "R1",
                "S1",
                0,
                "B1",
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


def test_soc_carries_across_consecutive_active_days() -> None:
    dates = [dt.date(2026, 4, 17) + dt.timedelta(days=offset) for offset in range(3)]
    result = simulate_block_year(
        _block(),
        dates,
        {"battery_kwh": 200.0, "consumption_kwh_per_km": 1.0, "depot_charge_kw": 0.0},
        dates[0],
        dates[-1],
        soc_init=1.0,
    )

    soc_days = result["soc"].reshape(3, 96)
    assert soc_days[1, 0] == soc_days[0, -1]
    assert soc_days[2, 0] == soc_days[1, -1]

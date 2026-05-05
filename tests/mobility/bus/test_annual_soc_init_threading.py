from __future__ import annotations

import datetime as dt

import pandas as pd

from mobility.bus.annual_simulation import simulate_block_year


def _dates(n_days: int) -> list[dt.date]:
    start = dt.date(2026, 4, 17)
    return [start + dt.timedelta(days=offset) for offset in range(n_days)]


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
                10.0,
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


def test_soc_init_threads_to_first_active_day_without_warmup() -> None:
    dates = _dates(3)
    result = simulate_block_year(
        _block(),
        dates,
        {"battery_kwh": 100.0, "consumption_kwh_per_km": 1.0, "depot_charge_kw": 0.0},
        dates[0],
        dates[-1],
        soc_init=0.5,
        warm_up_days=0,
    )

    assert result["soc"][0] == 0.5


def test_warmup_overrides_raw_soc_init_for_first_reported_day() -> None:
    dates = _dates(20)
    result = simulate_block_year(
        _block(),
        dates,
        {"battery_kwh": 100.0, "consumption_kwh_per_km": 1.0, "depot_charge_kw": 0.0},
        dates[0],
        dates[-1],
        soc_init=0.5,
        warm_up_days=14,
    )

    assert result["soc"][0] != 0.5

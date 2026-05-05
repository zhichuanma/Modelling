from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from mobility.bus.annual_simulation import simulate_block_year, simulate_fleet_year


def _active_dates(n_days: int) -> list[dt.date]:
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
                10.0,
                80.0,
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


def test_warmup_changes_first_reported_soc_but_preserves_annual_window() -> None:
    block = _block()
    dates = _active_dates(365)
    spec = {"battery_kwh": 120.0, "consumption_kwh_per_km": 1.0, "depot_charge_kw": 2.0}

    cold = simulate_block_year(block, dates, spec, dates[0], dates[-1], warm_up_days=0)
    warmed = simulate_block_year(block, dates, spec, dates[0], dates[-1], warm_up_days=14)

    assert cold["soc"][0] != warmed["soc"][0]
    assert warmed["load_matrix_kw"].shape == cold["load_matrix_kw"].shape == (365, 96)
    assert abs(warmed["energy_charged_kwh"] - cold["energy_charged_kwh"]) / cold["energy_charged_kwh"] < 0.05


def test_simulate_fleet_year_threads_warmup_end_to_end() -> None:
    dates = _active_dates(20)
    per_block, fleet_load_kw = simulate_fleet_year(
        _block(),
        {"S1": tuple(dates)},
        battery_kwh=120.0,
        consumption_kwh_per_km=1.0,
        depot_charge_kw=2.0,
        start_date=dates[0],
        end_date=dates[-1],
        warm_up_days=14,
    )

    assert per_block.loc["B1", "n_active_dates"] == len(dates)
    assert fleet_load_kw.shape == (len(dates), 96)
    assert np.isfinite(fleet_load_kw).all()

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from mobility.bus.annual_simulation import simulate_fleet_year


def _block() -> pd.DataFrame:
    return pd.DataFrame(
        [
            (
                "t0",
                "OP",
                "R1",
                "DOES_NOT_EXIST",
                0,
                "B_missing",
                "native",
                8.0,
                9.0,
                25.0,
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


def test_missing_service_id_warns_instead_of_silent_zero_year() -> None:
    with pytest.warns(UserWarning) as warning_record:
        per_block, fleet_load_kw = simulate_fleet_year(
            _block(),
            {"S1": (dt.date(2026, 4, 17),)},
            battery_kwh=120.0,
            consumption_kwh_per_km=1.0,
            depot_charge_kw=60.0,
            start_date="2026-04-17",
            end_date="2026-04-19",
        )

    messages = [str(warning.message) for warning in warning_record]
    assert any("not present in calendar" in message for message in messages)
    assert per_block.loc["B_missing", "n_active_dates"] == 0
    assert per_block.loc["B_missing", "annual_distance_km"] == 0.0
    assert fleet_load_kw.sum() == 0.0

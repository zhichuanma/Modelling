from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from mobility.bus.annual_simulation import simulate_block_year


def _overlap_block() -> pd.DataFrame:
    return pd.DataFrame(
        [
            (
                "late_tail",
                "OP",
                "R1",
                "S1",
                0,
                "B1",
                "native",
                23.5,
                26.0,
                50.0,
                "A",
                "B",
                51.0,
                -1.0,
                51.1,
                -1.1,
                "shape",
            ),
            (
                "early_trip",
                "OP",
                "R1",
                "S1",
                0,
                "B1",
                "native",
                1.5,
                4.0,
                30.0,
                "B",
                "C",
                51.1,
                -1.1,
                51.2,
                -1.2,
                "shape",
            ),
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


def test_cross_midnight_overlap_warns_and_is_audited() -> None:
    with pytest.warns(UserWarning, match="Trip overlap"):
        result = simulate_block_year(
            _overlap_block(),
            [dt.date(2026, 4, 17), dt.date(2026, 4, 18)],
            {"battery_kwh": 200.0, "consumption_kwh_per_km": 1.0, "depot_charge_kw": 80.0},
            "2026-04-17",
            "2026-04-19",
        )

    assert result["n_overlap_warnings"] >= 1

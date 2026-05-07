from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from mobility.bus.annual_simulation import simulate_block_year
from mobility.bus.annual_simulation import simulate_fleet_year


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


def test_simulate_block_year_can_drop_large_time_series_from_result() -> None:
    result = simulate_block_year(
        _block(),
        [dt.date(2026, 4, 17)],
        {"battery_kwh": 120.0, "consumption_kwh_per_km": 1.0, "depot_charge_kw": 60.0},
        "2026-04-17",
        "2026-04-17",
        keep_soc=False,
        keep_load_matrix=False,
    )

    assert "soc" not in result
    assert "load_kw" not in result
    assert "load_matrix_kw" not in result
    assert result["annual_distance_km"] == pytest.approx(100.0)
    assert result["soc_min"] < result["soc_end"]


def test_simulate_block_year_drops_zero_duration_template_rows() -> None:
    block = pd.DataFrame(
        [
            ("bad", "OP", "R1", "S1", 0, "B1", "native", 7.0, 7.0, 10.0, "A", "B", 51.0, -1.0, 51.05, -1.05, "shape0"),
            ("good", "OP", "R1", "S1", 0, "B1", "native", 8.0, 9.0, 20.0, "B", "C", 51.05, -1.05, 51.1, -1.1, "shape1"),
        ],
        columns=[
            "trip_id", "agency_id", "route_id", "service_id", "direction_id", "block_id",
            "block_source", "start_h", "end_h", "distance_km", "start_stop", "end_stop",
            "start_lat", "start_lon", "end_lat", "end_lon", "shape_id",
        ],
    )

    with pytest.warns(UserWarning, match="Dropping 1 non-positive-duration bus trips"):
        result = simulate_block_year(
            block,
            [dt.date(2026, 4, 17)],
            {"battery_kwh": 120.0, "consumption_kwh_per_km": 1.0, "depot_charge_kw": 60.0},
            "2026-04-17",
            "2026-04-17",
            soc_init=1.0,
        )

    assert result["annual_distance_km"] == pytest.approx(20.0)


def test_simulate_fleet_year_marks_all_invalid_block_as_simulation_error() -> None:
    fleet = pd.DataFrame(
        [
            ("bad", "OP", "R1", "S1", 0, "B_bad", "native", 7.0, 7.0, 10.0, "A", "B", 51.0, -1.0, 51.05, -1.05, "shape0"),
            ("good", "OP", "R1", "S2", 0, "B_good", "native", 8.0, 9.0, 20.0, "B", "C", 51.05, -1.05, 51.1, -1.1, "shape1"),
        ],
        columns=[
            "trip_id", "agency_id", "route_id", "service_id", "direction_id", "block_id",
            "block_source", "start_h", "end_h", "distance_km", "start_stop", "end_stop",
            "start_lat", "start_lon", "end_lat", "end_lon", "shape_id",
        ],
    )

    with pytest.warns(UserWarning, match="Dropping 1 non-positive-duration bus trips"):
        per_block, fleet_load = simulate_fleet_year(
            fleet,
            {
                "S1": (dt.date(2026, 4, 17),),
                "S2": (dt.date(2026, 4, 17),),
            },
            battery_kwh=120.0,
            consumption_kwh_per_km=1.0,
            depot_charge_kw=60.0,
            start_date="2026-04-17",
            end_date="2026-04-17",
        )

    assert fleet_load.shape == (1, 96)
    assert per_block.loc["B_bad", "infeasibility_reason"] == "simulation_error"
    assert "no trips with end_h > start_h" in per_block.loc["B_bad", "simulation_error"]
    assert per_block.loc["B_good", "annual_distance_km"] == pytest.approx(20.0)

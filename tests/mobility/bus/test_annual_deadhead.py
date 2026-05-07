"""Annual-path deadhead injection + audit propagation tests."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from mobility.bus.annual_simulation import simulate_block_year, simulate_fleet_year
from mobility.bus.year_schedule import block_to_year_schedules


_COLUMNS = (
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
)


def _short_deadhead_block() -> pd.DataFrame:
    """Two-trip block: trip 1 ends at (51.0,-1.0); trip 2 starts ~2 km north
    at (51.018,-1.0) with a 1 h gap. Triggers a SHORT deadhead."""
    return pd.DataFrame(
        [
            ("t0", "OP", "R1", "S1", 0, "B1", "native", 8.0, 9.0, 10.0, "A", "B", 51.0, -1.0, 51.0, -1.0, "shape"),
            ("t1", "OP", "R1", "S1", 0, "B1", "native", 10.0, 11.0, 10.0, "C", "D", 51.018, -1.0, 51.05, -1.0, "shape"),
        ],
        columns=list(_COLUMNS),
    )


def _skipped_time_block() -> pd.DataFrame:
    """Two-trip block with ~30 km gap and only 1 h available; the deadhead
    injection should be skipped on time grounds (30/30 + 0.05 dwell > 1 h)."""
    return pd.DataFrame(
        [
            ("t0", "OP", "R1", "S1", 0, "B1", "native", 8.0, 9.0, 10.0, "A", "B", 51.0, -1.0, 51.0, -1.0, "shape"),
            ("t1", "OP", "R1", "S1", 0, "B1", "native", 10.0, 11.0, 10.0, "C", "D", 51.27, -1.0, 51.3, -1.0, "shape"),
        ],
        columns=list(_COLUMNS),
    )


def _no_gap_block() -> pd.DataFrame:
    """Two-trip block where trip 1 end coordinate matches trip 2 start
    coordinate. Should produce zero deadhead audit values."""
    return pd.DataFrame(
        [
            ("t0", "OP", "R1", "S1", 0, "B1", "native", 8.0, 9.0, 10.0, "A", "B", 51.0, -1.0, 51.05, -1.0, "shape"),
            ("t1", "OP", "R1", "S1", 0, "B1", "native", 10.0, 11.0, 10.0, "B", "D", 51.05, -1.0, 51.1, -1.0, "shape"),
        ],
        columns=list(_COLUMNS),
    )


def test_block_to_year_schedules_injects_short_deadhead_for_active_service_date() -> None:
    schedules = block_to_year_schedules(
        _short_deadhead_block(),
        [dt.date(2026, 4, 17)],
        "2026-04-17",
        "2026-04-17",
        consumption_kwh_per_km=1.5,
        depot_charge_kw=80.0,
    )

    assert len(schedules) == 1
    schedule = schedules[0]
    deadheads = [trip for trip in schedule.trips if trip.is_deadhead]
    assert len(deadheads) == 1
    assert deadheads[0].deadhead_class == "short"
    assert deadheads[0].energy_consumed_kwh == pytest.approx(deadheads[0].distance_km * 1.5)
    assert schedule.metadata["deadhead_short_count"] == 1
    assert schedule.metadata["deadhead_long_count"] == 0
    assert schedule.metadata["deadhead_total_km"] > 0.0
    assert schedule.metadata["deadhead_total_kwh"] > 0.0


def test_block_to_year_schedules_credits_each_active_service_date() -> None:
    schedules = block_to_year_schedules(
        _short_deadhead_block(),
        [dt.date(2026, 4, 17), dt.date(2026, 4, 18)],
        "2026-04-17",
        "2026-04-18",
        consumption_kwh_per_km=1.5,
        depot_charge_kw=80.0,
    )

    deadheads = [trip for schedule in schedules for trip in schedule.trips if trip.is_deadhead]
    assert len(deadheads) == 2
    assert all(trip.deadhead_class == "short" for trip in deadheads)
    assert sum(s.metadata["deadhead_short_count"] for s in schedules) == 2
    assert sum(s.metadata["deadhead_total_km"] for s in schedules) > 0.0


def test_block_to_year_schedules_skipped_time_audit() -> None:
    schedules = block_to_year_schedules(
        _skipped_time_block(),
        [dt.date(2026, 4, 17)],
        "2026-04-17",
        "2026-04-17",
        consumption_kwh_per_km=1.0,
        depot_charge_kw=80.0,
    )

    schedule = schedules[0]
    assert all(not trip.is_deadhead for trip in schedule.trips)
    assert schedule.metadata["deadhead_short_count"] == 0
    assert schedule.metadata["deadhead_long_count"] == 0
    assert schedule.metadata["deadhead_skipped_time_count"] == 1
    assert schedule.metadata["deadhead_skipped_time_km"] > 25.0


def test_block_to_year_schedules_inactive_dates_have_zero_deadhead_metadata() -> None:
    schedules = block_to_year_schedules(
        _short_deadhead_block(),
        [dt.date(2026, 4, 17)],
        "2026-04-17",
        "2026-04-19",  # 04-18 and 04-19 are inactive
        consumption_kwh_per_km=1.5,
        depot_charge_kw=80.0,
    )

    inactive = [s for s in schedules if not s.metadata["service_active"]]
    assert len(inactive) == 2
    for schedule in inactive:
        assert schedule.metadata["deadhead_short_count"] == 0
        assert schedule.metadata["deadhead_long_count"] == 0
        assert schedule.metadata["deadhead_total_km"] == 0.0
        assert schedule.metadata["deadhead_total_kwh"] == 0.0
        assert schedule.metadata["deadhead_skipped_time_count"] == 0
        assert schedule.metadata["deadhead_skipped_time_km"] == 0.0
        assert all(not trip.is_deadhead for trip in schedule.trips)


def test_simulate_block_year_exposes_deadhead_audit_fields() -> None:
    result = simulate_block_year(
        _short_deadhead_block(),
        [dt.date(2026, 4, 17), dt.date(2026, 4, 18)],
        {"battery_kwh": 200.0, "consumption_kwh_per_km": 1.5, "depot_charge_kw": 80.0},
        "2026-04-17",
        "2026-04-18",
        soc_init=1.0,
    )

    for key in (
        "deadhead_short_count",
        "deadhead_long_count",
        "deadhead_total_km",
        "deadhead_total_kwh",
        "deadhead_skipped_time_count",
        "deadhead_skipped_time_km",
        "deadhead_skipped_missing_coord_count",
    ):
        assert key in result, f"missing audit key: {key}"
        assert result[key] is not None
    assert result["deadhead_short_count"] == 2  # one per active service date
    assert result["deadhead_total_km"] > 0.0
    assert result["deadhead_total_kwh"] > 0.0
    # Annual energy must include deadhead distance (above the 20 km service total).
    assert result["annual_distance_km"] > 40.0
    assert result["annual_energy_kwh"] > 30.0
    # Feasibility audit must also be present.
    assert "infeasible" in result
    assert "first_floor_hit_h" in result
    assert "shortfall_kwh" in result
    assert "infeasibility_reason" in result


def test_simulate_block_year_zero_audit_for_no_gap_block() -> None:
    result = simulate_block_year(
        _no_gap_block(),
        [dt.date(2026, 4, 17)],
        {"battery_kwh": 200.0, "consumption_kwh_per_km": 1.0, "depot_charge_kw": 80.0},
        "2026-04-17",
        "2026-04-17",
        soc_init=1.0,
    )

    assert result["deadhead_short_count"] == 0
    assert result["deadhead_long_count"] == 0
    assert result["deadhead_total_km"] == 0.0
    assert result["deadhead_total_kwh"] == 0.0
    assert result["deadhead_skipped_time_count"] == 0
    assert result["deadhead_skipped_time_km"] == 0.0
    # No-deadhead block: annual energy should equal service km × consumption.
    assert result["annual_distance_km"] == pytest.approx(20.0)
    assert result["annual_energy_kwh"] == pytest.approx(20.0)


def test_simulate_fleet_year_per_block_carries_audit_columns() -> None:
    deadhead_block = _short_deadhead_block()
    flat_block = _no_gap_block().assign(block_id="B2", service_id="S2")
    fleet = pd.concat([deadhead_block, flat_block], ignore_index=True)
    service_dates = {
        "S1": (dt.date(2026, 4, 17),),
        "S2": (dt.date(2026, 4, 17),),
    }
    per_block, _load = simulate_fleet_year(
        fleet,
        service_dates,
        battery_kwh=200.0,
        consumption_kwh_per_km=1.5,
        depot_charge_kw=80.0,
        start_date="2026-04-17",
        end_date="2026-04-17",
    )

    expected_columns = {
        "deadhead_short_count",
        "deadhead_long_count",
        "deadhead_total_km",
        "deadhead_total_kwh",
        "deadhead_skipped_time_count",
        "deadhead_skipped_time_km",
        "deadhead_skipped_missing_coord_count",
        "infeasible",
        "first_floor_hit_h",
        "first_floor_trip_id",
        "shortfall_kwh",
        "infeasibility_reason",
    }
    missing = expected_columns - set(per_block.columns)
    assert not missing, f"missing audit columns: {missing}"

    assert per_block.loc["B1", "deadhead_short_count"] == 1
    assert per_block.loc["B1", "deadhead_total_km"] > 0.0
    assert per_block.loc["B2", "deadhead_short_count"] == 0
    assert per_block.loc["B2", "deadhead_total_km"] == 0.0
    # Deadhead audit columns must default to zero (not NaN) for blocks with
    # no deadhead. Feasibility hit fields can legitimately be NaN/None when
    # the block is feasible, so they are not checked here.
    deadhead_numeric = [c for c in expected_columns if c.startswith("deadhead_")]
    assert not per_block[deadhead_numeric].isna().any().any()

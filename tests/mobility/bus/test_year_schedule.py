from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from mobility.bus.year_schedule import block_to_year_schedules


def _block() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ("t0", "OP", "R1", "S1", 0, "B1", "native", 8.0, 9.0, 10.0, "A", "B", 51.0, -1.0, 51.1, -1.1, "shape"),
            ("t1", "OP", "R1", "S1", 0, "B1", "native", 10.0, 11.0, 15.0, "B", "C", 51.1, -1.1, 51.2, -1.2, "shape"),
        ],
        columns=[
            "trip_id", "agency_id", "route_id", "service_id", "direction_id", "block_id",
            "block_source", "start_h", "end_h", "distance_km", "start_stop", "end_stop",
            "start_lat", "start_lon", "end_lat", "end_lon", "shape_id",
        ],
    )


def test_non_cross_midnight_block_expands_to_dated_schedules() -> None:
    schedules = block_to_year_schedules(
        _block(),
        [dt.date(2026, 4, 17)],
        "2026-04-17",
        "2026-04-19",
        "bus_B1",
        consumption_kwh_per_km=1.2,
        depot_charge_kw=100.0,
    )

    assert [schedule.day for schedule in schedules] == [0, 1, 2]
    assert [schedule.date.isoformat() for schedule in schedules] == ["2026-04-17", "2026-04-18", "2026-04-19"]
    assert len(schedules[0].trips) == 2
    assert len(schedules[1].trips) == 0
    assert schedules[1].parking_events[0].location_purpose == "depot_terminus"
    assert schedules[1].parking_events[0].duration_hours == 24.0
    energy = sum(trip.energy_consumed_kwh for schedule in schedules for trip in schedule.trips)
    assert energy == pytest.approx(25.0 * 1.2)

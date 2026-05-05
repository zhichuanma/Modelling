from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from mobility.bus.year_schedule import block_to_year_schedules


def _cross_midnight_block() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ("tail", "OP", "R1", "S1", 0, "B1", "native", 23.0, 25.0, 40.0, "A", "B", 51.0, -1.0, 51.1, -1.1, "shape"),
            ("day", "OP", "R1", "S1", 0, "B1", "native", 8.0, 9.0, 20.0, "B", "C", 51.1, -1.1, 51.2, -1.2, "shape"),
        ],
        columns=[
            "trip_id", "agency_id", "route_id", "service_id", "direction_id", "block_id",
            "block_source", "start_h", "end_h", "distance_km", "start_stop", "end_stop",
            "start_lat", "start_lon", "end_lat", "end_lon", "shape_id",
        ],
    )


def test_cross_midnight_tail_lands_on_next_date_without_overlapping_parking() -> None:
    schedules = block_to_year_schedules(
        _cross_midnight_block(),
        [dt.date(2026, 4, 17), dt.date(2026, 4, 18)],
        "2026-04-17",
        "2026-04-19",
        "bus_B1",
        consumption_kwh_per_km=1.0,
        depot_charge_kw=100.0,
    )

    assert [len(schedule.trips) for schedule in schedules] == [2, 3, 1]
    assert schedules[1].trips[0].departure_time == 0.0
    for schedule in schedules:
        events = sorted(schedule.parking_events, key=lambda event: event.start_time)
        assert all(right.start_time >= left.end_time for left, right in zip(events[:-1], events[1:]))
    total_km = sum(trip.distance_km for schedule in schedules for trip in schedule.trips)
    assert total_km == pytest.approx((40.0 + 20.0) * 2.0)

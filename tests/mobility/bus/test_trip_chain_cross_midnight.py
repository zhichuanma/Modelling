from __future__ import annotations

import pandas as pd
import pytest

from mobility.bus.trip_chain_bus import block_to_daily_schedules


def _cross_midnight_block() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ("t0", "OP", "R1", "S1", 0, "B1", "native", 23.5, 24.5, 12.0, "A", "B", 51.0, -1.0, 51.1, -1.1, "shape0"),
            ("t1", "OP", "R1", "S1", 0, "B1", "native", 25.0, 26.0, 18.0, "B", "C", 51.1, -1.1, 51.2, -1.2, None),
        ],
        columns=[
            "trip_id", "agency_id", "route_id", "service_id", "direction_id", "block_id",
            "block_source", "start_h", "end_h", "distance_km", "start_stop", "end_stop",
            "start_lat", "start_lon", "end_lat", "end_lon", "shape_id",
        ],
    )


def test_cross_midnight_block_returns_two_schedules_and_conserves_energy() -> None:
    schedules = block_to_daily_schedules(
        _cross_midnight_block(),
        "bus_B1",
        consumption_kwh_per_km=1.2,
        depot_charge_kw=100.0,
    )

    assert [schedule.day for schedule in schedules] == [0, 1]
    assert len(schedules[0].trips) == 1
    assert len(schedules[1].trips) == 2
    assert schedules[1].trips[0].departure_time == 0.0
    assert schedules[1].parking_events[0].start_time <= 0.0

    energy_kwh = sum(trip.energy_consumed_kwh for schedule in schedules for trip in schedule.trips)
    assert energy_kwh == pytest.approx((12.0 + 18.0) * 1.2, abs=1e-6)

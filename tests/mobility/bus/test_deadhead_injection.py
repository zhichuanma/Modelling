from __future__ import annotations

import mobility.bus.trip_chain_bus as trip_chain_bus
import pandas as pd
import pytest

from mobility.bus.trip_chain_bus import _inject_deadhead_trips, block_to_daily_schedules
from mobility.core.data_structures import Trip


def _trip(
    trip_id: str,
    dep: float,
    arr: float,
    *,
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
    start_stop: str,
    end_stop: str,
) -> Trip:
    trip = Trip(
        trip_id=trip_id,
        departure_time=dep,
        arrival_time=arr,
        distance_km=1.0,
        origin_purpose="bus_stop",
        destination_purpose="bus_stop",
        energy_consumed_kwh=1.0,
    )
    trip.start_lat = start_lat
    trip.start_lon = start_lon
    trip.end_lat = end_lat
    trip.end_lon = end_lon
    trip.start_stop = start_stop
    trip.end_stop = end_stop
    trip.block_id = "B1"
    return trip


def _pair(distance_lat_delta: float, right_depart_h: float) -> list[Trip]:
    left = _trip(
        "left",
        7.0,
        8.0,
        start_lat=51.0,
        start_lon=-0.1,
        end_lat=51.0,
        end_lon=-0.1,
        start_stop="A",
        end_stop="B",
    )
    right = _trip(
        "right",
        right_depart_h,
        right_depart_h + 1.0,
        start_lat=51.0 + distance_lat_delta,
        start_lon=-0.1,
        end_lat=51.2,
        end_lon=-0.1,
        start_stop="C",
        end_stop="D",
    )
    return [left, right]


def test_deadhead_gap_below_noise_threshold_is_not_injected() -> None:
    augmented, stats = _inject_deadhead_trips(_pair(0.0027, 9.0), consumption_kwh_per_km=1.5)

    assert len(augmented) == 2
    assert stats.short_count == 0
    assert stats.long_count == 0


def test_short_deadhead_is_injected_and_energy_accounted() -> None:
    augmented, stats = _inject_deadhead_trips(_pair(0.018, 9.0), consumption_kwh_per_km=1.5)

    deadhead = augmented[1]
    assert deadhead.is_deadhead is True
    assert deadhead.deadhead_class == "short"
    assert stats.short_count == 1
    assert stats.total_kwh == pytest.approx(deadhead.distance_km * 1.5)


def test_long_deadhead_skipped_when_time_window_is_too_short() -> None:
    augmented, stats = _inject_deadhead_trips(_pair(0.27, 9.0), consumption_kwh_per_km=1.0)

    assert len(augmented) == 2
    assert stats.skipped_time_count == 1
    assert stats.skipped_time_km > 25.0


def test_long_deadhead_is_injected_when_time_window_allows_it() -> None:
    augmented, stats = _inject_deadhead_trips(_pair(0.27, 9.5), consumption_kwh_per_km=1.0)

    deadhead = augmented[1]
    assert deadhead.deadhead_class == "long"
    assert stats.long_count == 1
    assert deadhead.arrival_time == pytest.approx(deadhead.departure_time + deadhead.distance_km / 30.0)


def test_non_positive_duration_deadhead_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trip_chain_bus, "DEADHEAD_SPEED_KMH", float("inf"))

    augmented, stats = _inject_deadhead_trips(_pair(0.018, 9.0), consumption_kwh_per_km=1.0)

    assert len(augmented) == 2
    assert all(not trip.is_deadhead for trip in augmented)
    assert stats.skipped_time_count == 1
    assert stats.skipped_time_km > 0.5


def test_cross_midnight_deadhead_is_split_after_block_level_injection() -> None:
    block = pd.DataFrame(
        [
            ("t0", "OP", "R1", "S1", 0, "B1", "native", 23.0, 23.5, 10.0, "A", "B", 51.0, -0.1, 51.0, -0.1, "shape"),
            ("t1", "OP", "R1", "S1", 0, "B1", "native", 24.7, 25.2, 10.0, "C", "D", 51.018, -0.1, 51.1, -0.1, "shape"),
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

    schedules = block_to_daily_schedules(
        block,
        "bus_B1",
        consumption_kwh_per_km=1.0,
        depot_charge_kw=100.0,
    )
    deadheads = [trip for schedule in schedules for trip in schedule.trips if trip.is_deadhead]

    assert [schedule.day for schedule in schedules] == [0, 1]
    assert len(deadheads) == 1
    assert deadheads[0].departure_time >= 23.5
    assert schedules[0].metadata["deadhead_short_count"] == 1

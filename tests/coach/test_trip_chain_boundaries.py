"""Task 1 gate: cross-midnight entry semantics in ``trip_chain_coach``."""
from __future__ import annotations

import pandas as pd
import pytest

from mobility.coach.trip_chain_coach import journey_to_daily_schedules


def _stops() -> pd.DataFrame:
    return pd.DataFrame({"stop_sequence": [1, 2], "stop_point_ref": ["A", "B"]})


def _row(start_h: float, end_h: float, distance_km: float = 90.0) -> pd.Series:
    return pd.Series(
        {
            "vehicle_journey_code": "BOUNDARY",
            "start_h": start_h,
            "end_h": end_h,
            "distance_km": distance_km,
            "distance_source": "haversine_x_detour",
        }
    )


def test_start_in_next_day_clock_raises() -> None:
    with pytest.raises(ValueError, match=r"start_h must be in \[0, 24\)"):
        journey_to_daily_schedules(
            _row(24.5, 25.0),
            _stops(),
            consumption_kwh_per_km=1.0,
        )


def test_end_beyond_day_one_raises() -> None:
    with pytest.raises(ValueError, match="exceeds 48h"):
        journey_to_daily_schedules(
            _row(23.0, 49.0),
            _stops(),
            consumption_kwh_per_km=1.0,
        )


def test_end_at_24h_stays_on_day_zero() -> None:
    schedules = journey_to_daily_schedules(
        _row(23.0, 24.0, 60.0),
        _stops(),
        consumption_kwh_per_km=1.2,
    )

    assert [schedule.day for schedule in schedules] == [0]
    assert schedules[0].trips[0].distance_km == pytest.approx(60.0)
    assert schedules[0].trips[0].energy_consumed_kwh == pytest.approx(72.0)


def test_cross_midnight_split_preserves_energy() -> None:
    schedules = journey_to_daily_schedules(
        _row(23.5, 25.0, 90.0),
        _stops(),
        consumption_kwh_per_km=1.2,
    )

    assert [schedule.day for schedule in schedules] == [0, 1]
    total_distance = sum(trip.distance_km for schedule in schedules for trip in schedule.trips)
    total_energy = sum(trip.energy_consumed_kwh for schedule in schedules for trip in schedule.trips)
    assert total_distance == pytest.approx(90.0, abs=1e-9)
    assert total_energy == pytest.approx(108.0, abs=1e-9)

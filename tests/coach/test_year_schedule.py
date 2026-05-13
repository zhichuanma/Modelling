from __future__ import annotations

import pandas as pd

from mobility.coach.calendar import COACH_FEED_YEAR_END, COACH_FEED_YEAR_START
from mobility.coach.year_schedule import chain_to_year_schedules


def _chain_journeys() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "journey_id": ["J1"],
            "vehicle_journey_code": ["VJ1"],
            "coach_chain_id": ["OP_2026-05-11_001"],
            "position_in_chain": [1],
            "start_h": [8.0],
            "end_h": [10.0],
            "distance_km": [80.0],
            "consumption_kwh_per_km": [0.9],
            "start_lsoa": ["E01000001"],
            "end_lsoa": ["E01000002"],
        }
    )


def test_chain_to_year_schedules_has_active_and_inactive_days() -> None:
    schedules = chain_to_year_schedules(_chain_journeys(), [COACH_FEED_YEAR_START])
    expected_days = (COACH_FEED_YEAR_END - COACH_FEED_YEAR_START).days + 1

    assert len(schedules) == expected_days
    assert schedules[0].date == COACH_FEED_YEAR_START
    assert schedules[0].trips

    inactive = schedules[1]
    assert inactive.trips == []
    assert len(inactive.parking_events) == 1
    event = inactive.parking_events[0]
    assert event.location_purpose == "terminus_dwell"
    assert event.duration_hours == 24.0
    assert event.can_charge is True

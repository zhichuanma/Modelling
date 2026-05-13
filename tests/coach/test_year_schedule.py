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
    assert event.location_purpose == "depot_terminus"
    assert event.duration_hours == 24.0
    assert event.can_charge is True


def test_layover_default_does_not_charge_and_opt_in_does() -> None:
    chain = pd.DataFrame(
        {
            "journey_id": ["J1", "J2"],
            "vehicle_journey_code": ["VJ1", "VJ2"],
            "coach_chain_id": ["C1", "C1"],
            "position_in_chain": [1, 2],
            "start_h": [8.0, 14.0],
            "end_h": [10.0, 16.0],
            "distance_km": [80.0, 80.0],
            "consumption_kwh_per_km": [0.9, 0.9],
            "start_lsoa": ["E01000001", "E01000002"],
            "end_lsoa": ["E01000002", "E01000003"],
        }
    )

    default_day = chain_to_year_schedules(chain, [COACH_FEED_YEAR_START])[0]
    layover = next(event for event in default_day.parking_events if event.start_time == 10.0 and event.end_time == 14.0)
    pre = next(event for event in default_day.parking_events if event.start_time == 2.0 and event.end_time == 8.0)
    post = next(event for event in default_day.parking_events if event.start_time == 16.0 and event.end_time == 24.0)

    assert layover.location_purpose == "layover"
    assert layover.can_charge is False
    assert layover.charge_power_kw == 0.0
    assert pre.location_purpose == "depot_terminus"
    assert post.location_purpose == "depot_terminus"
    assert pre.can_charge is True
    assert post.can_charge is True

    opt_in_day = chain_to_year_schedules(
        chain,
        [COACH_FEED_YEAR_START],
        allow_layover_charging=True,
        layover_charge_kw=50.0,
        min_layover_for_charging_h=2.0,
    )[0]
    opt_in_layover = next(event for event in opt_in_day.parking_events if event.location_purpose == "layover")
    assert opt_in_layover.can_charge is True
    assert opt_in_layover.charge_power_kw == 50.0

    too_short_day = chain_to_year_schedules(
        chain,
        [COACH_FEED_YEAR_START],
        allow_layover_charging=True,
        layover_charge_kw=50.0,
        min_layover_for_charging_h=5.0,
    )[0]
    too_short_layover = next(event for event in too_short_day.parking_events if event.location_purpose == "layover")
    assert too_short_layover.can_charge is False

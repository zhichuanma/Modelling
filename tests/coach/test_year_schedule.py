from __future__ import annotations

import pandas as pd

from mobility.coach.calendar import COACH_FEED_YEAR_END, COACH_FEED_YEAR_START
from mobility.coach.annual_simulation import simulate_coach_chain_year
from mobility.coach.year_schedule import chain_to_year_schedules
from mobility.core.constants import STEPS_PER_DAY_DECISION


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

    assert len(schedules) == expected_days + 1
    assert schedules[0].date == COACH_FEED_YEAR_START
    assert schedules[0].trips

    inactive = schedules[1]
    assert inactive.trips == []
    assert len(inactive.parking_events) == 1
    event = inactive.parking_events[0]
    assert event.location_purpose == "depot_terminus"
    assert event.duration_hours == 24.0
    assert event.can_charge is True
    assert schedules[-1].metadata["is_overflow_day"] is True
    assert schedules[-1].parking_events == []


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


def test_year_end_cross_midnight_overflow_preserves_soc_and_energy() -> None:
    chain = _chain_journeys().assign(
        start_h=[22.0],
        end_h=[26.0],
        distance_km=[80.0],
        consumption_kwh_per_km=[0.5],
    )

    schedules = chain_to_year_schedules(
        chain,
        [COACH_FEED_YEAR_END],
        terminus_charge_kw=0.0,
    )
    expected_days = (COACH_FEED_YEAR_END - COACH_FEED_YEAR_START).days + 1
    overflow = schedules[-1]

    assert len(schedules) == expected_days + 1
    assert overflow.trips
    assert overflow.parking_events == []

    result = simulate_coach_chain_year(
        "C1",
        chain,
        {"EV_ID": "EV1", "Energy_kWh": 400.0, "consumption_kwh_per_km": 0.5},
        [COACH_FEED_YEAR_END],
        warm_up_days=0,
        soc_init=1.0,
        terminus_charge_kw=0.0,
    )

    assert result["load_kw"].shape[0] == expected_days * STEPS_PER_DAY_DECISION
    assert result["total_kwh"] == 40.0
    assert result["overflow_trip_count"] >= 1

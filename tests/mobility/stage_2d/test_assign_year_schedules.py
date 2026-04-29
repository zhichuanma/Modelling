"""Stage 2d coverage for year-round person-linked schedule assignment."""

from __future__ import annotations

import hashlib
import importlib
import json

import numpy as np
import pandas as pd
import pytest

trip_chain = importlib.import_module("mobility.cars.trip_chain")

assign_chains_to_fleet = trip_chain.assign_chains_to_fleet
assign_year_schedules = trip_chain.assign_year_schedules


def _chain_json(chain: list[tuple[float, float, float, str, str]]) -> str:
    payload = [
        [dep, arr, distance_km, purpose_from, purpose_to]
        for dep, arr, distance_km, purpose_from, purpose_to in chain
    ]
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


def _build_library_df() -> pd.DataFrame:
    workday = [
        (8.0, 9.0, 10.0, "home", "work"),
        (17.0, 18.0, 10.0, "work", "home"),
    ]
    shopping_day = [
        (9.0, 10.0, 6.0, "home", "shopping"),
        (14.0, 15.0, 6.0, "shopping", "home"),
    ]
    leisure_day = [
        (11.0, 12.0, 8.0, "home", "leisure"),
        (18.0, 19.0, 8.0, "leisure", "home"),
    ]
    social_day = [
        (12.0, 13.0, 7.0, "home", "social"),
        (20.0, 21.0, 7.0, "social", "home"),
    ]

    rows: list[dict[str, object]] = []

    for day_of_week in range(7):
        rows.append(
            {
                "person_id": "person_1",
                "pattern_id": 0,
                "day_of_week": day_of_week,
                "chain_json": _chain_json(workday if day_of_week < 5 else leisure_day),
            }
        )
        rows.append(
            {
                "person_id": "person_2",
                "pattern_id": 0,
                "day_of_week": day_of_week,
                "chain_json": _chain_json(shopping_day if day_of_week < 5 else social_day),
            }
        )

    for pattern_id in [0, 1]:
        for day_of_week in range(7):
            if day_of_week < 5:
                chain = workday
            elif day_of_week == 6 and pattern_id == 1:
                chain = [(13.0, 14.0, 9.0, "home", "holiday")]
            else:
                chain = []
            rows.append(
                {
                    "person_id": "person_holiday",
                    "pattern_id": pattern_id,
                    "day_of_week": day_of_week,
                    "chain_json": _chain_json(chain),
                }
            )

    return pd.DataFrame(rows)


@pytest.fixture()
def ev_fleet() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "EV_ID": ["ev_1", "ev_2", "ev_holiday"],
            "battery_capacity_kwh": [60.0, 72.0, 55.0],
            "consumption_kwh_per_km": [0.20, 0.18, 0.19],
            "home_lsoa": ["LSOA_1", "LSOA_2", "LSOA_3"],
            "LSOA_code": ["LSOA_1", "LSOA_2", "LSOA_3"],
        }
    )


@pytest.fixture()
def library_df() -> pd.DataFrame:
    return _build_library_df()


@pytest.fixture()
def person_fleet_two_evs() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ev_id": ["ev_1", "ev_2"],
            "person_id": ["person_1", "person_2"],
            "nts_household_id": ["hh_1", "hh_2"],
            "nts_region": ["england", "england"],
        }
    )


def _run_year_assignment(
    person_fleet: pd.DataFrame,
    ev_fleet: pd.DataFrame,
    library_df: pd.DataFrame,
    *,
    year: int = 2025,
    n_weeks: int = 4,
    seed: int = 42,
) -> dict[str, list]:
    return assign_year_schedules(
        person_fleet,
        ev_fleet,
        library_df,
        year=year,
        n_weeks=n_weeks,
        rng=np.random.default_rng(seed),
    )


def _serialise_schedule_payload(schedules: dict[str, list]) -> dict[str, list[tuple]]:
    payload: dict[str, list[tuple]] = {}
    for ev_id, ev_schedules in schedules.items():
        payload[ev_id] = [
            (
                schedule.day,
                schedule.date,
                tuple(trip.distance_km for trip in schedule.trips),
                tuple(event.location_purpose for event in schedule.parking_events),
            )
            for schedule in ev_schedules
        ]
    return payload


def test_schedule_count(
    person_fleet_two_evs: pd.DataFrame,
    ev_fleet: pd.DataFrame,
    library_df: pd.DataFrame,
) -> None:
    schedules = _run_year_assignment(person_fleet_two_evs, ev_fleet, library_df, n_weeks=4)

    assert set(schedules) == {"ev_1", "ev_2"}
    assert all(len(ev_schedules) == 28 for ev_schedules in schedules.values())


def test_date_field_populated(
    person_fleet_two_evs: pd.DataFrame,
    ev_fleet: pd.DataFrame,
    library_df: pd.DataFrame,
) -> None:
    schedules = _run_year_assignment(person_fleet_two_evs, ev_fleet, library_df, n_weeks=4)

    for ev_schedules in schedules.values():
        dates = [schedule.date for schedule in ev_schedules]
        assert all(date_value is not None for date_value in dates)
        assert dates == sorted(dates)

        for schedule in ev_schedules:
            assert schedule.date is not None
            if schedule.day_type == "weekday":
                assert 1 <= schedule.date.isoweekday() <= 5
            else:
                assert schedule.date.isoweekday() in {6, 7}


def test_determinism(
    person_fleet_two_evs: pd.DataFrame,
    ev_fleet: pd.DataFrame,
    library_df: pd.DataFrame,
) -> None:
    observed_a = _run_year_assignment(person_fleet_two_evs, ev_fleet, library_df, n_weeks=4, seed=7)
    observed_b = _run_year_assignment(person_fleet_two_evs, ev_fleet, library_df, n_weeks=4, seed=7)

    assert _serialise_schedule_payload(observed_a) == _serialise_schedule_payload(observed_b)


def test_holiday_week_triggers_transform(
    ev_fleet: pd.DataFrame,
    library_df: pd.DataFrame,
) -> None:
    person_fleet = pd.DataFrame(
        {
            "ev_id": ["ev_holiday"],
            "person_id": ["person_holiday"],
            "nts_household_id": ["hh_holiday"],
            "nts_region": ["england"],
        }
    )

    schedules = _run_year_assignment(
        person_fleet,
        ev_fleet,
        library_df,
        year=2025,
        n_weeks=52,
        seed=0,
    )

    holiday_weekdays_with_leisure = []
    for schedule in schedules["ev_holiday"]:
        assert schedule.date is not None
        if schedule.date.isoweekday() >= 6:
            continue
        if not trip_chain.holiday_rules.is_holiday_week(schedule.date, "england"):
            continue
        holiday_weekdays_with_leisure.append(
            any(trip.destination_purpose in {"leisure", "holiday"} for trip in schedule.trips)
        )

    assert holiday_weekdays_with_leisure
    assert any(holiday_weekdays_with_leisure)


def test_deprecation_warning(ev_fleet: pd.DataFrame) -> None:
    pools = {
        "weekday": [[(8.0, 9.0, 10.0, "home", "work"), (17.0, 18.0, 10.0, "work", "home")]],
        "weekend": [[(11.0, 12.0, 5.0, "home", "leisure"), (18.0, 19.0, 5.0, "leisure", "home")]],
    }
    fleet = ev_fleet.iloc[:1].copy()

    with pytest.deprecated_call(match="assign_chains_to_fleet is deprecated"):
        schedules = assign_chains_to_fleet(
            fleet,
            pools,
            num_days=7,
            rng=np.random.default_rng(11),
        )

    assert schedules
    assert schedules["ev_1"]


def test_no_random_seed_side_effect(
    person_fleet_two_evs: pd.DataFrame,
    ev_fleet: pd.DataFrame,
    library_df: pd.DataFrame,
) -> None:
    state_before = hashlib.sha256(repr(np.random.get_state()).encode("utf-8")).hexdigest()

    _ = _run_year_assignment(person_fleet_two_evs, ev_fleet, library_df, n_weeks=4, seed=5)

    state_after = hashlib.sha256(repr(np.random.get_state()).encode("utf-8")).hexdigest()
    assert state_before == state_after

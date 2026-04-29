"""Stage 5 coverage for seasonal trip-energy correction in assign_year_schedules."""

from __future__ import annotations

import importlib
import json

import numpy as np
import pandas as pd
import pytest

constants = importlib.import_module("mobility.core.constants")
seasonal = importlib.import_module("mobility.core.seasonal")
trip_chain = importlib.import_module("mobility.cars.trip_chain")

assign_year_schedules = trip_chain.assign_year_schedules
chain_to_daily_schedule = trip_chain.chain_to_daily_schedule
get_seasonal_factor = seasonal.get_seasonal_factor


def _chain_json(chain: list[tuple[float, float, float, str, str]]) -> str:
    payload = [
        [dep, arr, distance_km, purpose_from, purpose_to]
        for dep, arr, distance_km, purpose_from, purpose_to in chain
    ]
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


def _build_library_df() -> pd.DataFrame:
    weekday_chain = [
        (8.0, 9.0, 10.0, "home", "work"),
        (17.0, 18.0, 10.0, "work", "home"),
    ]
    weekend_chain = [
        (11.0, 12.0, 8.0, "home", "leisure"),
        (18.0, 19.0, 8.0, "leisure", "home"),
    ]

    rows: list[dict[str, object]] = []
    for person_id in ["person_1", "person_2"]:
        for day_of_week in range(7):
            rows.append(
                {
                    "person_id": person_id,
                    "pattern_id": 0,
                    "day_of_week": day_of_week,
                    "chain_json": _chain_json(
                        weekday_chain if day_of_week < 5 else weekend_chain
                    ),
                }
            )

    return pd.DataFrame(rows)


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


@pytest.fixture()
def ev_fleet_two_evs() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "EV_ID": ["ev_1", "ev_2"],
            "battery_capacity_kwh": [60.0, 72.0],
            "consumption_kwh_per_km": [0.20, 0.20],
            "home_lsoa": ["LSOA_1", "LSOA_2"],
            "LSOA_code": ["LSOA_1", "LSOA_2"],
        }
    )


def _run_year_assignment(
    person_fleet: pd.DataFrame,
    ev_fleet: pd.DataFrame,
    library_df: pd.DataFrame,
    *,
    year: int = 2025,
    n_weeks: int = 52,
    seed: int = 42,
    apply_seasonal_correction: bool = True,
) -> dict[str, list]:
    return assign_year_schedules(
        person_fleet,
        ev_fleet,
        library_df,
        year=year,
        n_weeks=n_weeks,
        rng=np.random.default_rng(seed),
        apply_seasonal_correction=apply_seasonal_correction,
    )


def _find_schedule_for_month(
    schedules: list,
    *,
    month: int,
    region: str = "england",
) -> object:
    for schedule in schedules:
        if schedule.date is None or schedule.date.month != month:
            continue
        if trip_chain.holiday_rules.is_holiday_week(schedule.date, region):
            continue
        if schedule.trips:
            return schedule
    raise AssertionError(f"No non-holiday schedule with trips found for month={month}")


def test_constants_values() -> None:
    assert constants.SEASONAL_CONSUMPTION_FACTOR == {
        "winter": 1.35,
        "spring": 1.00,
        "summer": 1.10,
        "autumn": 1.00,
    }
    assert constants.MONTH_TO_SEASON == {
        12: "winter", 1: "winter", 2: "winter",
        3: "spring", 4: "spring", 5: "spring",
        6: "summer", 7: "summer", 8: "summer",
        9: "autumn", 10: "autumn", 11: "autumn",
    }


def test_get_seasonal_factor_all_months() -> None:
    for month in range(1, 13):
        factor = get_seasonal_factor(month)
        if month in {12, 1, 2}:
            assert factor == 1.35
        elif month in {3, 4, 5}:
            assert factor == 1.00
        elif month in {6, 7, 8}:
            assert factor == 1.10
        else:
            assert factor == 1.00


def test_get_seasonal_factor_rejects_invalid_month() -> None:
    for month in [0, 13, -1, 1.5]:
        with pytest.raises(ValueError, match="1..12"):
            get_seasonal_factor(month)


def test_winter_trip_has_135x_energy(
    person_fleet_two_evs: pd.DataFrame,
    ev_fleet_two_evs: pd.DataFrame,
    library_df: pd.DataFrame,
) -> None:
    schedules = _run_year_assignment(
        person_fleet_two_evs,
        ev_fleet_two_evs,
        library_df,
    )
    january_schedule = _find_schedule_for_month(schedules["ev_1"], month=1)
    trip = january_schedule.trips[0]
    expected_energy = trip.distance_km * 0.20 * 1.35
    relative_error = abs(trip.energy_consumed_kwh - expected_energy) / expected_energy
    assert relative_error < 1e-9


def test_summer_trip_has_110x_energy(
    person_fleet_two_evs: pd.DataFrame,
    ev_fleet_two_evs: pd.DataFrame,
    library_df: pd.DataFrame,
) -> None:
    schedules = _run_year_assignment(
        person_fleet_two_evs,
        ev_fleet_two_evs,
        library_df,
    )
    july_schedule = _find_schedule_for_month(schedules["ev_1"], month=7)
    trip = july_schedule.trips[0]
    expected_energy = trip.distance_km * 0.20 * 1.10
    relative_error = abs(trip.energy_consumed_kwh - expected_energy) / expected_energy
    assert relative_error < 1e-9


def test_neutral_seasons_unchanged(
    person_fleet_two_evs: pd.DataFrame,
    ev_fleet_two_evs: pd.DataFrame,
    library_df: pd.DataFrame,
) -> None:
    observed_off = _run_year_assignment(
        person_fleet_two_evs,
        ev_fleet_two_evs,
        library_df,
        seed=7,
        apply_seasonal_correction=False,
    )
    observed_on = _run_year_assignment(
        person_fleet_two_evs,
        ev_fleet_two_evs,
        library_df,
        seed=7,
        apply_seasonal_correction=True,
    )

    for month in [4, 10]:
        schedules_off = [schedule for schedule in observed_off["ev_1"] if schedule.date.month == month]
        schedules_on = [schedule for schedule in observed_on["ev_1"] if schedule.date.month == month]
        assert len(schedules_off) == len(schedules_on)
        for schedule_off, schedule_on in zip(schedules_off, schedules_on, strict=True):
            assert [trip.energy_consumed_kwh for trip in schedule_off.trips] == [
                trip.energy_consumed_kwh for trip in schedule_on.trips
            ]


def test_apply_seasonal_correction_false_disables_factor(
    person_fleet_two_evs: pd.DataFrame,
    ev_fleet_two_evs: pd.DataFrame,
    library_df: pd.DataFrame,
) -> None:
    schedules = _run_year_assignment(
        person_fleet_two_evs,
        ev_fleet_two_evs,
        library_df,
        apply_seasonal_correction=False,
    )

    for schedule in schedules["ev_1"]:
        for trip in schedule.trips:
            assert trip.energy_consumed_kwh == trip.distance_km * 0.20


def test_chain_to_daily_schedule_untouched() -> None:
    chain = [
        (8.0, 9.0, 10.0, "home", "work"),
        (17.0, 18.0, 10.0, "work", "home"),
    ]
    schedule = chain_to_daily_schedule(
        chain,
        "ev_chain",
        0,
        "weekday",
        0.20,
        rng=np.random.default_rng(9),
    )

    assert [trip.energy_consumed_kwh for trip in schedule.trips] == [2.0, 2.0]

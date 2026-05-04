from __future__ import annotations

from copy import deepcopy
import datetime as dt
import importlib
import json
from unittest.mock import patch

import numpy as np
import pandas as pd

trip_chain = importlib.import_module("mobility.cars.trip_chain")
data_structures = importlib.import_module("mobility.core.data_structures")

DailySchedule = data_structures.DailySchedule
ParkingEvent = data_structures.ParkingEvent
Trip = data_structures.Trip
_smooth_cross_day_parking = trip_chain._smooth_cross_day_parking
assign_year_schedules = trip_chain.assign_year_schedules


class FakeSampler:
    def __init__(
        self,
        destinations: dict[tuple[str, str], str],
        distances: dict[tuple[str, str], float],
    ):
        self.destinations = destinations
        self.distances = distances

    def sample_destination_lsoa(
        self,
        origin_lsoa: str,
        purpose: str,
        rng: np.random.Generator,
        home_lsoa: str,
    ) -> str:
        _ = rng
        return self.destinations[(origin_lsoa, purpose)]

    def distance_km(self, a: str, b: str) -> float:
        return self.distances[(a, b)]


def _build_trip(
    *,
    trip_id: str,
    departure_time: float,
    arrival_time: float,
    distance_km: float,
    origin_purpose: str,
    destination_purpose: str,
    origin_lsoa: str,
    destination_lsoa: str,
    energy_consumed_kwh: float,
) -> Trip:
    return Trip(
        trip_id=trip_id,
        departure_time=departure_time,
        arrival_time=arrival_time,
        distance_km=distance_km,
        origin_purpose=origin_purpose,
        destination_purpose=destination_purpose,
        energy_consumed_kwh=energy_consumed_kwh,
        origin_lsoa=origin_lsoa,
        destination_lsoa=destination_lsoa,
    )


def test_smooth_cross_day_parking_preserves_true_overnight_origin() -> None:
    day0 = DailySchedule(
        ev_id="ev_1",
        day=0,
        day_type="weekday",
        trips=[
            _build_trip(
                trip_id="d0_t0",
                departure_time=18.0,
                arrival_time=20.0,
                distance_km=30.0,
                origin_purpose="home",
                destination_purpose="holiday",
                origin_lsoa="HOME",
                destination_lsoa="X",
                energy_consumed_kwh=6.0,
            )
        ],
        parking_events=[
            ParkingEvent(
                start_time=20.0,
                end_time=24.0,
                duration_hours=4.0,
                location_purpose="holiday",
                location_lsoa="WRONG",
            )
        ],
    )
    day1 = DailySchedule(
        ev_id="ev_1",
        day=1,
        day_type="weekday",
        trips=[
            _build_trip(
                trip_id="d1_t0",
                departure_time=8.0,
                arrival_time=9.0,
                distance_km=12.5,
                origin_purpose="holiday",
                destination_purpose="work",
                origin_lsoa="X",
                destination_lsoa="Y",
                energy_consumed_kwh=2.4,
            )
        ],
        parking_events=[
            ParkingEvent(
                start_time=0.0,
                end_time=8.0,
                duration_hours=8.0,
                location_purpose="holiday",
                location_lsoa="X",
            )
        ],
    )
    original_day0_trips = deepcopy(day0.trips)
    original_day1_trip = deepcopy(day1.trips[0])
    original_day1_first_parking = deepcopy(day1.parking_events[0])

    _smooth_cross_day_parking([day0, day1])

    assert day0.parking_events[-1].location_lsoa == "X"
    assert day0.parking_events[-1].location_purpose == "holiday"
    assert day0.trips == original_day0_trips
    assert day1.trips[0] == original_day1_trip
    assert day1.parking_events[0] == original_day1_first_parking


def test_smooth_cross_day_parking_respects_declared_return_home() -> None:
    day0 = DailySchedule(
        ev_id="ev_1",
        day=0,
        day_type="weekday",
        trips=[
            _build_trip(
                trip_id="d0_t0",
                departure_time=18.0,
                arrival_time=20.0,
                distance_km=22.0,
                origin_purpose="home",
                destination_purpose="holiday",
                origin_lsoa="HOME",
                destination_lsoa="X",
                energy_consumed_kwh=4.1,
            )
        ],
        parking_events=[
            ParkingEvent(
                start_time=20.0,
                end_time=24.0,
                duration_hours=4.0,
                location_purpose="holiday",
                location_lsoa="X",
            )
        ],
    )
    day1 = DailySchedule(
        ev_id="ev_1",
        day=1,
        day_type="weekday",
        trips=[
            _build_trip(
                trip_id="d1_t0",
                departure_time=7.5,
                arrival_time=8.0,
                distance_km=5.0,
                origin_purpose="home",
                destination_purpose="shopping",
                origin_lsoa="HOME",
                destination_lsoa="Z",
                energy_consumed_kwh=1.1,
            )
        ],
        parking_events=[
            ParkingEvent(
                start_time=0.0,
                end_time=7.5,
                duration_hours=7.5,
                location_purpose="home",
                location_lsoa="HOME",
            )
        ],
    )
    original_day0_trips = deepcopy(day0.trips)
    original_day1 = deepcopy(day1)

    _smooth_cross_day_parking([day0, day1])

    assert day0.parking_events[-1].location_lsoa == "HOME"
    assert day0.parking_events[-1].location_purpose == "home"
    assert day0.trips == original_day0_trips
    assert day1 == original_day1


def test_smooth_cross_day_parking_is_noop_when_boundary_is_already_consistent() -> None:
    day0 = DailySchedule(
        ev_id="ev_1",
        day=0,
        day_type="weekday",
        trips=[
            _build_trip(
                trip_id="d0_t0",
                departure_time=17.0,
                arrival_time=18.0,
                distance_km=10.0,
                origin_purpose="work",
                destination_purpose="home",
                origin_lsoa="Y",
                destination_lsoa="HOME",
                energy_consumed_kwh=2.0,
            )
        ],
        parking_events=[
            ParkingEvent(
                start_time=18.0,
                end_time=24.0,
                duration_hours=6.0,
                location_purpose="home",
                location_lsoa="HOME",
            )
        ],
    )
    day1 = DailySchedule(
        ev_id="ev_1",
        day=1,
        day_type="weekday",
        trips=[
            _build_trip(
                trip_id="d1_t0",
                departure_time=8.0,
                arrival_time=9.0,
                distance_km=8.0,
                origin_purpose="home",
                destination_purpose="work",
                origin_lsoa="HOME",
                destination_lsoa="Y",
                energy_consumed_kwh=1.6,
            )
        ],
        parking_events=[
            ParkingEvent(
                start_time=0.0,
                end_time=8.0,
                duration_hours=8.0,
                location_purpose="home",
                location_lsoa="HOME",
            )
        ],
    )
    original_day0 = deepcopy(day0)
    original_day1 = deepcopy(day1)

    _smooth_cross_day_parking([day0, day1])

    assert day0 == original_day0
    assert day1 == original_day1


def _chain_json(chain: list[tuple[float, float, float, str, str]]) -> str:
    payload = [
        [dep, arr, distance_km, purpose_from, purpose_to]
        for dep, arr, distance_km, purpose_from, purpose_to in chain
    ]
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


def _build_library_df(
    day0: list[tuple[float, float, float, str, str]],
    day1: list[tuple[float, float, float, str, str]],
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for day_of_week in range(7):
        if day_of_week == 0:
            chain = day0
        elif day_of_week == 1:
            chain = day1
        else:
            chain = []
        rows.append(
            {
                "person_id": "person_1",
                "pattern_id": 0,
                "day_of_week": day_of_week,
                "chain_json": _chain_json(chain),
            }
        )
    return pd.DataFrame(rows)


def _build_fleet_inputs() -> tuple[pd.DataFrame, pd.DataFrame]:
    person_fleet = pd.DataFrame(
        {
            "ev_id": ["ev_1"],
            "person_id": ["person_1"],
            "nts_household_id": ["hh_1"],
            "nts_region": ["england"],
        }
    )
    ev_fleet = pd.DataFrame(
        {
            "EV_ID": ["ev_1"],
            "battery_capacity_kwh": [60.0],
            "consumption_kwh_per_km": [0.2],
            "home_lsoa": ["HOME"],
            "LSOA_code": ["HOME"],
        }
    )
    return person_fleet, ev_fleet


def _run_assignment(
    day0: list[tuple[float, float, float, str, str]],
    day1: list[tuple[float, float, float, str, str]],
) -> list[DailySchedule]:
    person_fleet, ev_fleet = _build_fleet_inputs()
    library_df = _build_library_df(day0, day1)
    sampler = FakeSampler(
        destinations={
            ("HOME", "holiday"): "X",
            ("HOME", "work"): "Y_HOME",
            ("X", "work"): "Y_X",
        },
        distances={
            ("HOME", "X"): 12.0,
            ("HOME", "Y_HOME"): 8.0,
            ("X", "Y_X"): 9.0,
        },
    )

    with patch.object(trip_chain.holiday_rules, "is_holiday_week", return_value=False):
        schedules = assign_year_schedules(
            person_fleet,
            ev_fleet,
            library_df,
            year=2025,
            n_weeks=1,
            sampler=sampler,
            jitter_minutes=0.0,
            rng=np.random.default_rng(0),
            apply_seasonal_correction=False,
            region="england",
        )
    return schedules["ev_1"]


def test_assign_year_schedules_threads_overnight_lsoa_for_true_overnight() -> None:
    schedules = _run_assignment(
        day0=[(18.0, 20.0, 10.0, "home", "holiday")],
        day1=[(8.0, 9.0, 7.0, "holiday", "work")],
    )

    day0 = schedules[0]
    day1 = schedules[1]

    assert day0.date == dt.date(2024, 12, 30)
    assert day1.date == dt.date(2024, 12, 31)
    assert day0.trips[-1].destination_lsoa == "X"
    assert day0.parking_events[-1].location_lsoa == "X"
    assert day1.trips[0].origin_lsoa == "X"
    assert day1.parking_events[0].location_lsoa == "X"


def test_assign_year_schedules_respects_declared_home_for_silent_return() -> None:
    schedules = _run_assignment(
        day0=[(18.0, 20.0, 10.0, "home", "holiday")],
        day1=[(8.0, 9.0, 7.0, "home", "work")],
    )

    day0 = schedules[0]
    day1 = schedules[1]

    assert day0.trips[-1].destination_lsoa == "X"
    assert day0.parking_events[-1].location_lsoa == "HOME"
    assert day1.trips[0].origin_lsoa == "HOME"
    assert day1.parking_events[0].location_lsoa == "HOME"


def test_day_zero_uses_home_when_no_prior_overnight() -> None:
    schedules = _run_assignment(
        day0=[(8.0, 9.0, 7.0, "holiday", "work")],
        day1=[],
    )

    day0 = schedules[0]

    assert day0.trips[0].origin_lsoa == "HOME"
    assert day0.parking_events[0].location_lsoa == "HOME"

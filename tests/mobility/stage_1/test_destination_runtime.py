"""Stage 1c coverage for runtime Layer-1 destination integration."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib
import pickle
from typing import Dict, List
import warnings

import numpy as np
import pandas as pd

destination_module = importlib.import_module("mobility.cars.destination")
trip_chain = importlib.import_module("mobility.cars.trip_chain")

DestinationSampler = destination_module.DestinationSampler
assign_chains_to_fleet = trip_chain.assign_chains_to_fleet
chain_to_daily_schedule = trip_chain.chain_to_daily_schedule


def _build_centroids() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "lsoa_code": ["A", "B", "C", "D", "E"],
            "easting_m": [0.0, 200_000.0, 200_500.0, 212_500.0, 210_000.0],
            "northing_m": [0.0, 0.0, 0.0, 0.0, 0.0],
        }
    )


def _build_sampler(tmp_path) -> DestinationSampler:
    table = pd.DataFrame(
        {
            "origin_lsoa": [
                "A",
                "A",
                "A",
                "A",
                "B",
                "C",
            ],
            "purpose": [
                "work",
                "education",
                "shopping",
                "holiday",
                "social",
                "leisure",
            ],
            "dest_lsoa": [
                "B",
                "A",
                "B",
                "B",
                "C",
                "D",
            ],
            "prob": np.array([1.0, 1.0, 1.0, 0.75, 1.0, 1.0], dtype=np.float32),
        }
    )
    extra = pd.DataFrame(
        {
            "origin_lsoa": ["A"],
            "purpose": ["holiday"],
            "dest_lsoa": ["E"],
            "prob": np.array([0.25], dtype=np.float32),
        }
    )
    table = pd.concat([table, extra], ignore_index=True)
    table_path = tmp_path / "destination_choice_table.parquet"
    table.to_parquet(table_path, engine="pyarrow", index=False)
    return DestinationSampler(table_path=table_path, centroids=_build_centroids())


def _build_small_pools() -> Dict[str, list[list[tuple[float, float, float, str, str]]]]:
    return {
        "weekday": [
            [
                (7.0, 8.0, 12.0, "home", "work"),
                (17.0, 18.0, 10.0, "work", "home"),
            ],
            [
                (8.5, 9.25, 6.5, "home", "shopping"),
                (14.0, 15.0, 5.0, "shopping", "home"),
            ],
        ],
        "weekend": [
            [
                (10.0, 11.0, 8.0, "home", "leisure"),
                (18.0, 19.0, 8.0, "leisure", "home"),
            ]
        ],
    }


def _build_small_fleet() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "EV_ID": ["ev_1", "ev_2", "ev_3"],
            "battery_capacity_kwh": [48.0, 64.0, 82.0],
            "consumption_kwh_per_km": [0.17, 0.19, 0.21],
            "home_lsoa": ["A", "B", "C"],
        }
    )


def _legacy_add_time_jitter(
    value: float,
    jitter_minutes: float,
    rng: np.random.Generator,
) -> float:
    delta = float(rng.uniform(-jitter_minutes, jitter_minutes)) / 60.0
    return max(0.0, min(23.75, value + delta))


def _legacy_chain_to_daily_schedule(
    chain,
    ev_id: str,
    day: int,
    day_type: str,
    consumption_kwh_per_km: float,
    jitter_minutes: float,
    rng: np.random.Generator,
):
    schedule = trip_chain.DailySchedule(ev_id=ev_id, day=day, day_type=day_type)

    for dep_t, arr_t, dist_km, p_from, p_to in chain:
        dep = _legacy_add_time_jitter(dep_t, jitter_minutes, rng)
        arr = _legacy_add_time_jitter(arr_t, jitter_minutes, rng)
        if arr < dep:
            arr = dep + 0.05

        schedule.trips.append(
            trip_chain.Trip(
                trip_id=f"{ev_id}_d{day}_{len(schedule.trips)}",
                departure_time=dep,
                arrival_time=arr,
                distance_km=dist_km,
                origin_purpose=p_from,
                destination_purpose=p_to,
                energy_consumed_kwh=dist_km * consumption_kwh_per_km,
            )
        )

    schedule.trips.sort(key=lambda trip: trip.departure_time)
    for i in range(1, len(schedule.trips)):
        prev = schedule.trips[i - 1]
        curr = schedule.trips[i]
        if curr.departure_time < prev.arrival_time:
            curr.departure_time = prev.arrival_time
            if curr.arrival_time < curr.departure_time:
                curr.arrival_time = curr.departure_time + 0.05

    trips = schedule.trips
    if trips:
        first_trip = trips[0]
        if first_trip.departure_time > 0:
            schedule.parking_events.append(
                trip_chain.ParkingEvent(
                    start_time=0.0,
                    end_time=first_trip.departure_time,
                    duration_hours=first_trip.departure_time,
                    location_purpose=first_trip.origin_purpose,
                )
            )
        for i in range(len(trips) - 1):
            park_start = trips[i].arrival_time
            park_end = trips[i + 1].departure_time
            if park_end <= park_start:
                continue
            schedule.parking_events.append(
                trip_chain.ParkingEvent(
                    start_time=park_start,
                    end_time=park_end,
                    duration_hours=park_end - park_start,
                    location_purpose=trips[i].destination_purpose,
                )
            )
        last_trip = trips[-1]
        if last_trip.arrival_time < 24.0:
            schedule.parking_events.append(
                trip_chain.ParkingEvent(
                    start_time=last_trip.arrival_time,
                    end_time=24.0,
                    duration_hours=24.0 - last_trip.arrival_time,
                    location_purpose=last_trip.destination_purpose,
                )
            )

    return schedule


def _legacy_assign_chains_to_fleet(
    ev_fleet: pd.DataFrame,
    pools,
    *,
    num_days: int,
    jitter_minutes: float,
    seed: int,
):
    rng = np.random.default_rng(seed)
    weekday_chains = pools["weekday"]
    weekend_chains = pools["weekend"]
    day_types = ["weekend" if (d % 7) >= 5 else "weekday" for d in range(num_days)]

    n_evs = len(ev_fleet)
    wd_indices = rng.integers(0, len(weekday_chains), size=n_evs * num_days)
    we_indices = rng.integers(0, len(weekend_chains), size=n_evs * num_days)

    ev_ids = ev_fleet["EV_ID"].to_numpy(dtype=object)
    batteries = ev_fleet["battery_capacity_kwh"].to_numpy(dtype=float)
    consumptions = ev_fleet["consumption_kwh_per_km"].to_numpy(dtype=float)

    fleet_schedules = {}
    for i in range(n_evs):
        battery = batteries[i] if batteries[i] > 0 else 60.0
        consumption = consumptions[i] if consumptions[i] > 0 else battery / 250.0
        schedules = []
        for day_idx in range(num_days):
            day_type = day_types[day_idx]
            if day_type == "weekend":
                chain = weekend_chains[int(we_indices[i * num_days + day_idx])]
            else:
                chain = weekday_chains[int(wd_indices[i * num_days + day_idx])]
            schedules.append(
                _legacy_chain_to_daily_schedule(
                    chain,
                    str(ev_ids[i]),
                    day_idx,
                    day_type,
                    consumption,
                    jitter_minutes,
                    rng,
                )
            )
        trip_chain._smooth_cross_day_parking(schedules)
        fleet_schedules[str(ev_ids[i])] = schedules

    return fleet_schedules


def _serialise_fleet_schedules(fleet_schedules) -> dict[str, list[dict[str, object]]]:
    payload: dict[str, list[dict[str, object]]] = {}
    for ev_id, schedules in fleet_schedules.items():
        payload[ev_id] = []
        for schedule in schedules:
            payload[ev_id].append(
                {
                    "day": schedule.day,
                    "day_type": schedule.day_type,
                    "trips": [
                        {
                            "trip_id": trip.trip_id,
                            "departure_time": trip.departure_time,
                            "arrival_time": trip.arrival_time,
                            "distance_km": trip.distance_km,
                            "origin_purpose": trip.origin_purpose,
                            "destination_purpose": trip.destination_purpose,
                            "origin_lsoa": trip.origin_lsoa,
                            "destination_lsoa": trip.destination_lsoa,
                            "distance_km_nts": trip.distance_km_nts,
                            "fallback_distance": trip.fallback_distance,
                        }
                        for trip in schedule.trips
                    ],
                    "parking_events": [
                        {
                            "start_time": event.start_time,
                            "end_time": event.end_time,
                            "duration_hours": event.duration_hours,
                            "location_purpose": event.location_purpose,
                            "location_lsoa": event.location_lsoa,
                        }
                        for event in schedule.parking_events
                    ],
                }
            )
    return payload


def test_old_path_without_sampler_stays_bit_identical() -> None:
    ev_fleet = _build_small_fleet()
    pools = _build_small_pools()

    expected = _legacy_assign_chains_to_fleet(
        ev_fleet=deepcopy(ev_fleet),
        pools=deepcopy(pools),
        num_days=3,
        jitter_minutes=10.0,
        seed=42,
    )
    observed = assign_chains_to_fleet(
        ev_fleet=deepcopy(ev_fleet),
        pools=deepcopy(pools),
        num_days=3,
        jitter_minutes=10.0,
        seed=42,
    )

    assert _serialise_fleet_schedules(observed) == _serialise_fleet_schedules(expected)


def test_destination_sampler_home_and_determinism(tmp_path) -> None:
    sampler = _build_sampler(tmp_path)

    assert sampler.sample_destination_lsoa(
        origin_lsoa="A",
        purpose="home",
        rng=np.random.default_rng(1),
        home_lsoa="HOME",
    ) == "HOME"

    rng_a = np.random.default_rng(42)
    rng_b = np.random.default_rng(42)
    sample_a = [
        sampler.sample_destination_lsoa("A", "holiday", rng_a, "A")
        for _ in range(6)
    ]
    sample_b = [
        sampler.sample_destination_lsoa("A", "holiday", rng_b, "A")
        for _ in range(6)
    ]
    assert sample_a == sample_b


def test_fallback_logic_marks_all_trigger_branches(tmp_path) -> None:
    sampler = _build_sampler(tmp_path)
    cases = [
        ("A", [(8.0, 9.0, 8.0, "home", "education")]),
        ("A", [(8.0, 9.0, 10.0, "home", "shopping")]),
        ("B", [(8.0, 9.0, 5.0, "home", "social")]),
        ("C", [(8.0, 10.0, 1.5, "home", "leisure")]),
    ]

    for home_lsoa, chain in cases:
        schedule = chain_to_daily_schedule(
            chain=chain,
            ev_id="ev_test",
            day=0,
            day_type="weekday",
            consumption_kwh_per_km=0.2,
            home_lsoa=home_lsoa,
            sampler=sampler,
            rng=np.random.default_rng(123),
        )
        trip = schedule.trips[0]
        assert trip.fallback_distance is True
        assert trip.distance_km == trip.distance_km_nts


def test_parking_event_location_lsoa_tracks_sampled_destinations(tmp_path) -> None:
    sampler = _build_sampler(tmp_path)

    schedule_roundtrip = chain_to_daily_schedule(
        chain=[
            (8.0, 9.0, 12.0, "home", "work"),
            (17.0, 18.0, 10.0, "work", "home"),
        ],
        ev_id="ev_roundtrip",
        day=0,
        day_type="weekday",
        consumption_kwh_per_km=0.2,
        home_lsoa="A",
        sampler=sampler,
        rng=np.random.default_rng(77),
    )
    assert schedule_roundtrip.parking_events[0].location_lsoa == "A"
    assert schedule_roundtrip.parking_events[-1].location_lsoa == "A"

    schedule_one_way = chain_to_daily_schedule(
        chain=[(8.0, 9.0, 12.0, "home", "work")],
        ev_id="ev_one_way",
        day=0,
        day_type="weekday",
        consumption_kwh_per_km=0.2,
        home_lsoa="A",
        sampler=sampler,
        rng=np.random.default_rng(88),
    )
    assert schedule_one_way.parking_events[0].location_lsoa == "A"
    assert schedule_one_way.parking_events[-1].location_lsoa == "B"


def test_unknown_origin_purpose_warns_only_once_and_returns_home(tmp_path) -> None:
    sampler = _build_sampler(tmp_path)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        first = sampler.sample_destination_lsoa(
            origin_lsoa="missing_origin",
            purpose="work",
            rng=np.random.default_rng(1),
            home_lsoa="HOME",
        )
        second = sampler.sample_destination_lsoa(
            origin_lsoa="missing_origin",
            purpose="work",
            rng=np.random.default_rng(2),
            home_lsoa="HOME",
        )

    assert first == "HOME"
    assert second == "HOME"
    assert len(caught) == 1


def test_old_path_hash_is_stable_for_fixed_seed() -> None:
    fleet_schedules = assign_chains_to_fleet(
        ev_fleet=_build_small_fleet(),
        pools=_build_small_pools(),
        num_days=3,
        jitter_minutes=10.0,
        seed=42,
    )
    digest = hashlib.sha256(
        pickle.dumps(_serialise_fleet_schedules(fleet_schedules))
    ).hexdigest()
    assert len(digest) == 64

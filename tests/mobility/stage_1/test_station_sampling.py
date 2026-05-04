"""Stage 1d coverage for Layer-2 Huff-weighted station sampling."""

from __future__ import annotations

from copy import deepcopy
import importlib

import numpy as np
import pandas as pd

constants = importlib.import_module("mobility.core.constants")
data_structures = importlib.import_module("mobility.core.data_structures")
station_matcher = importlib.import_module("mobility.cars.station_matcher")

DailySchedule = data_structures.DailySchedule
ParkingEvent = data_structures.ParkingEvent
HOME_CHARGER_KW = constants.HOME_CHARGER_KW
_build_lsoa_indices = station_matcher._build_lsoa_indices
_distance_m = station_matcher._distance_m
_huff_weights = station_matcher._huff_weights
match_stations_for_fleet = station_matcher.match_stations_for_fleet
match_stations_for_schedule = station_matcher.match_stations_for_schedule


def _build_centroids() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "lsoa_code": ["A", "B", "C"],
            "easting_m": [0.0, 3_000.0, 10_000.0],
            "northing_m": [0.0, 0.0, 0.0],
        }
    ).set_index("lsoa_code")


def _build_schedule(
    *,
    ev_id: str = "ev_1",
    day: int = 0,
    parking_events: list[ParkingEvent] | None = None,
) -> DailySchedule:
    return DailySchedule(
        ev_id=ev_id,
        day=day,
        day_type="weekday",
        parking_events=parking_events or [],
    )


def _serialise_events(fleet_schedules: dict[str, list[DailySchedule]]) -> dict[str, list[list[tuple]]]:
    payload: dict[str, list[list[tuple]]] = {}
    for ev_id, schedules in fleet_schedules.items():
        payload[ev_id] = []
        for schedule in schedules:
            payload[ev_id].append(
                [
                    (
                        event.start_time,
                        event.location_purpose,
                        event.location_lsoa,
                        event.can_charge,
                        event.matched_station_id,
                        event.charge_power_kw,
                    )
                    for event in schedule.parking_events
                ]
            )
    return payload


def test_match_stations_for_fleet_is_reproducible() -> None:
    stations_df = pd.DataFrame(
        {
            "lsoa_code": ["A", "A", "A", "B", "B"],
            "label": ["work", "work", "shopping", "work", "shopping"],
            "StationID": [1, 2, 3, 4, 5],
            "TotalCapacity_kW": [7.0, 22.0, 11.0, 50.0, 3.6],
            "station_attractiveness": [0.2, 0.8, 1.0, 0.7, 1.0],
        }
    )
    ev_fleet = pd.DataFrame(
        {
            "EV_ID": ["ev_1", "ev_2"],
            "home_lsoa": ["A", "B"],
            "ac_power_kw": [7.0, 11.0],
        }
    )
    fleet_schedules_a = {
        "ev_1": [
            _build_schedule(
                ev_id="ev_1",
                day=0,
                parking_events=[
                    ParkingEvent(9.0, 12.0, 3.0, "work", location_lsoa="A"),
                    ParkingEvent(18.0, 20.0, 2.0, "shopping", location_lsoa="A"),
                ],
            ),
            _build_schedule(
                ev_id="ev_1",
                day=1,
                parking_events=[
                    ParkingEvent(9.0, 12.0, 3.0, "work", location_lsoa="A"),
                ],
            ),
        ],
        "ev_2": [
            _build_schedule(
                ev_id="ev_2",
                day=0,
                parking_events=[
                    ParkingEvent(10.0, 14.0, 4.0, "work", location_lsoa="B"),
                    ParkingEvent(20.0, 24.0, 4.0, "home", location_lsoa="B"),
                ],
            )
        ],
    }
    fleet_schedules_b = deepcopy(fleet_schedules_a)

    match_stations_for_fleet(
        fleet_schedules=fleet_schedules_a,
        ev_fleet=ev_fleet,
        stations_df=stations_df,
        rng=np.random.default_rng(42),
        centroids=_build_centroids(),
    )
    match_stations_for_fleet(
        fleet_schedules=fleet_schedules_b,
        ev_fleet=ev_fleet,
        stations_df=stations_df,
        rng=np.random.default_rng(42),
        centroids=_build_centroids(),
    )

    assert _serialise_events(fleet_schedules_a) == _serialise_events(fleet_schedules_b)


def test_same_lsoa_sampling_follows_station_attractiveness() -> None:
    attr = np.array([0.1, 0.3, 0.6], dtype=np.float64)
    distances_m = np.full(3, 500.0, dtype=np.float64)
    weights = _huff_weights(attr, distances_m)
    probs = weights / weights.sum()
    draws = np.random.default_rng(123).choice(3, size=10_000, p=probs)
    freq = np.bincount(draws, minlength=3) / 10_000.0

    assert np.all(np.abs(freq - np.array([0.1, 0.3, 0.6])) < 0.02)


def test_same_lsoa_dominates_far_higher_attr_neighbor() -> None:
    distances_m = np.array([500.0, 3_000.0], dtype=np.float64)
    attr = np.array([0.01, 100.0], dtype=np.float64)
    weights = _huff_weights(attr, distances_m)
    probs = weights / weights.sum()
    draws = np.random.default_rng(456).choice(2, size=10_000, p=probs)
    freq = np.bincount(draws, minlength=2) / 10_000.0

    assert freq[0] > 0.9999


def test_same_lsoa_picks_regardless_of_label() -> None:
    schedule = _build_schedule(
        parking_events=[ParkingEvent(9.0, 12.0, 3.0, "work", location_lsoa="A")]
    )
    stations_df = pd.DataFrame(
        {
            "lsoa_code": ["A"],
            "label": ["shopping"],
            "StationID": [10],
            "TotalCapacity_kW": [22.0],
            "station_attractiveness": [1.0],
        }
    )

    match_stations_for_schedule(
        schedule=schedule,
        ev_home_lsoa="A",
        ev_ac_power_kw=7.0,
        stations_df=stations_df,
        rng=np.random.default_rng(0),
        centroids=_build_centroids(),
        date_iso="day000",
    )

    event = schedule.parking_events[0]
    assert event.can_charge is True
    assert event.matched_station_id == 10


def test_same_lsoa_with_mismatched_labels_still_matches_station() -> None:
    schedule = _build_schedule(
        parking_events=[ParkingEvent(9.0, 12.0, 3.0, "holiday", location_lsoa="A")]
    )
    stations_df = pd.DataFrame(
        {
            "lsoa_code": ["A", "A"],
            "label": ["work", "shopping"],
            "StationID": [11, 12],
            "TotalCapacity_kW": [22.0, 11.0],
            "station_attractiveness": [0.2, 0.8],
        }
    )

    match_stations_for_schedule(
        schedule=schedule,
        ev_home_lsoa="A",
        ev_ac_power_kw=7.0,
        stations_df=stations_df,
        rng=np.random.default_rng(0),
        centroids=_build_centroids(),
        date_iso="day000",
    )

    event = schedule.parking_events[0]
    assert event.can_charge is True
    assert event.matched_station_id in {11, 12}


def test_tier3_fallback_uses_neighbor_lsoa() -> None:
    schedule = _build_schedule(
        parking_events=[ParkingEvent(9.0, 12.0, 3.0, "work", location_lsoa="A")]
    )
    stations_df = pd.DataFrame(
        {
            "lsoa_code": ["B"],
            "label": ["shopping"],
            "StationID": [20],
            "TotalCapacity_kW": [11.0],
            "station_attractiveness": [1.0],
        }
    )

    match_stations_for_schedule(
        schedule=schedule,
        ev_home_lsoa="A",
        ev_ac_power_kw=7.0,
        stations_df=stations_df,
        rng=np.random.default_rng(0),
        centroids=_build_centroids(),
        neighbor_buffer_lsoas={"A": ["B"]},
        date_iso="day000",
    )

    event = schedule.parking_events[0]
    assert event.can_charge is True
    assert event.matched_station_id == 20


def test_neighbor_fallback_ignores_label_mismatch() -> None:
    schedule = _build_schedule(
        parking_events=[ParkingEvent(9.0, 12.0, 3.0, "holiday", location_lsoa="A")]
    )
    stations_df = pd.DataFrame(
        {
            "lsoa_code": ["B"],
            "label": ["work"],
            "StationID": [21],
            "TotalCapacity_kW": [11.0],
            "station_attractiveness": [1.0],
        }
    )

    match_stations_for_schedule(
        schedule=schedule,
        ev_home_lsoa="A",
        ev_ac_power_kw=7.0,
        stations_df=stations_df,
        rng=np.random.default_rng(0),
        centroids=_build_centroids(),
        neighbor_buffer_lsoas={"A": ["B"]},
        date_iso="day000",
    )

    event = schedule.parking_events[0]
    assert event.can_charge is True
    assert event.matched_station_id == 21


def test_empty_pool_marks_event_as_cannot_charge() -> None:
    schedule = _build_schedule(
        parking_events=[ParkingEvent(9.0, 12.0, 3.0, "work", location_lsoa="C")]
    )
    stations_df = pd.DataFrame(
        {
            "lsoa_code": ["A"],
            "label": ["shopping"],
            "StationID": [30],
            "TotalCapacity_kW": [11.0],
            "station_attractiveness": [1.0],
        }
    )

    match_stations_for_schedule(
        schedule=schedule,
        ev_home_lsoa="C",
        ev_ac_power_kw=7.0,
        stations_df=stations_df,
        rng=np.random.default_rng(0),
        centroids=_build_centroids(),
        date_iso="day000",
    )

    event = schedule.parking_events[0]
    assert event.can_charge is False
    assert event.charge_power_kw == 0.0
    assert event.matched_station_id is None


def test_home_events_remain_home_charger_short_circuit() -> None:
    schedule = _build_schedule(
        parking_events=[ParkingEvent(0.0, 8.0, 8.0, "home", location_lsoa="A")]
    )
    stations_df = pd.DataFrame(
        {
            "lsoa_code": ["A"],
            "label": ["work"],
            "StationID": [40],
            "TotalCapacity_kW": [50.0],
            "station_attractiveness": [1.0],
        }
    )

    match_stations_for_schedule(
        schedule=schedule,
        ev_home_lsoa="A",
        ev_ac_power_kw=7.0,
        stations_df=stations_df,
        rng=np.random.default_rng(0),
        centroids=_build_centroids(),
        date_iso="day000",
    )

    event = schedule.parking_events[0]
    assert event.can_charge is True
    assert event.charge_power_kw == HOME_CHARGER_KW
    assert event.matched_station_id is None


def test_power_cap_and_empty_location_lsoa_fallback_to_home() -> None:
    schedule = _build_schedule(
        parking_events=[ParkingEvent(9.0, 12.0, 3.0, "work", location_lsoa="")]
    )
    stations_df = pd.DataFrame(
        {
            "lsoa_code": ["A"],
            "label": ["work"],
            "StationID": [50],
            "TotalCapacity_kW": [50.0],
            "station_attractiveness": [1.0],
        }
    )
    indices = _build_lsoa_indices(stations_df)

    match_stations_for_schedule(
        schedule=schedule,
        ev_home_lsoa="A",
        ev_ac_power_kw=7.0,
        stations_df=stations_df,
        rng=np.random.default_rng(0),
        centroids=_build_centroids(),
        _indices=indices,
        date_iso="day000",
    )

    event = schedule.parking_events[0]
    assert event.can_charge is True
    assert event.matched_station_id == 50
    assert event.charge_power_kw == 7.0


def test_build_indices_keeps_rows_with_missing_label() -> None:
    stations_df = pd.DataFrame(
        {
            "lsoa_code": ["A"],
            "label": [np.nan],
            "StationID": [60],
            "TotalCapacity_kW": [7.0],
            "station_attractiveness": [1.0],
        }
    )

    indices = _build_lsoa_indices(stations_df)

    assert indices["by_lsoa"]["A"].tolist() == [0]
    assert indices["sid"].tolist() == [60]


def test_distance_helper_matches_intra_lsoa_and_centroid_distance() -> None:
    centroids = _build_centroids()
    assert _distance_m("A", "A", centroids) == 500.0
    assert _distance_m("A", "B", centroids) == 3_000.0

"""Stage 6 coverage for home charging short-circuit behavior."""

from __future__ import annotations

from copy import deepcopy
import importlib

import numpy as np
import pandas as pd
import pytest

constants = importlib.import_module("mobility.core.constants")
data_structures = importlib.import_module("mobility.core.data_structures")
station_matcher = importlib.import_module("mobility.cars.station_matcher")

DailySchedule = data_structures.DailySchedule
ParkingEvent = data_structures.ParkingEvent
HOME_CHARGER_KW = constants.HOME_CHARGER_KW
_build_lsoa_indices = station_matcher._build_lsoa_indices
match_stations_for_fleet = station_matcher.match_stations_for_fleet
match_stations_for_schedule = station_matcher.match_stations_for_schedule

EV_LSOA = "E01000001"


def _build_centroids() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "lsoa_code": [EV_LSOA],
            "easting_m": [0.0],
            "northing_m": [0.0],
        }
    ).set_index("lsoa_code")


def _build_schedule() -> DailySchedule:
    return DailySchedule(
        ev_id="ev_1",
        day=0,
        day_type="weekday",
        parking_events=[
            ParkingEvent(
                start_time=0.0,
                end_time=8.0,
                duration_hours=8.0,
                location_purpose="home",
            ),
            ParkingEvent(
                start_time=9.0,
                end_time=17.0,
                duration_hours=8.0,
                location_purpose="work",
            ),
            ParkingEvent(
                start_time=18.0,
                end_time=19.5,
                duration_hours=1.5,
                location_purpose="shopping",
            ),
            ParkingEvent(
                start_time=20.0,
                end_time=24.0,
                duration_hours=4.0,
                location_purpose="home",
            ),
        ],
    )


def _build_precleaned_station_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "StationID": [102, 103],
            "lsoa_code": [EV_LSOA, EV_LSOA],
            "label": ["work", "shopping"],
            "TotalCapacity_kW": [22.0, 3.6],
            "station_attractiveness": [3.0, 1.0],
        }
    )


def _legacy_match_stations_for_schedule(
    schedule: DailySchedule,
    ev_lsoa: str,
    ev_ac_power_kw: float,
    station_lookup: dict[tuple[str, str], tuple[int, float]],
) -> None:
    for parking_event in schedule.parking_events:
        if parking_event.location_purpose == "home":
            parking_event.can_charge = True
            parking_event.matched_station_id = None
            parking_event.charge_power_kw = HOME_CHARGER_KW
            continue

        hit = station_lookup.get((ev_lsoa, parking_event.location_purpose))
        if hit is not None:
            parking_event.can_charge = True
            parking_event.matched_station_id = hit[0]
            parking_event.charge_power_kw = min(hit[1], ev_ac_power_kw)
        else:
            parking_event.can_charge = False
            parking_event.matched_station_id = None
            parking_event.charge_power_kw = 0.0


def _non_home_state(schedule: DailySchedule) -> list[tuple[str, bool, int | None, float]]:
    return [
        (
            parking_event.location_purpose,
            parking_event.can_charge,
            parking_event.matched_station_id,
            parking_event.charge_power_kw,
        )
        for parking_event in schedule.parking_events
        if parking_event.location_purpose != "home"
    ]


def test_home_events_short_circuit_to_home_charger_and_keep_non_home_matching() -> None:
    schedule = _build_schedule()

    match_stations_for_schedule(
        schedule=schedule,
        ev_home_lsoa=EV_LSOA,
        ev_ac_power_kw=11.0,
        stations_df=_build_precleaned_station_df(),
        rng=np.random.default_rng(0),
        centroids=_build_centroids(),
        date_iso="day000",
    )

    for parking_event in schedule.parking_events:
        if parking_event.location_purpose == "home":
            assert parking_event.can_charge is True
            assert parking_event.charge_power_kw == HOME_CHARGER_KW
            assert parking_event.matched_station_id is None
        elif parking_event.location_purpose == "work":
            assert parking_event.can_charge is True
            assert parking_event.charge_power_kw == pytest.approx(11.0)
            assert parking_event.matched_station_id == 102
        else:
            assert parking_event.can_charge is True
            assert parking_event.charge_power_kw == pytest.approx(3.6)
            assert parking_event.matched_station_id == 103


def test_match_stations_for_fleet_leaves_station_dataframe_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, list[str]] = {}
    real_build_lsoa_indices = station_matcher._build_lsoa_indices

    def wrapped_build_lsoa_indices(stations_df: pd.DataFrame) -> dict:
        captured["labels"] = stations_df["label"].tolist()
        return real_build_lsoa_indices(stations_df)

    monkeypatch.setattr(station_matcher, "_build_lsoa_indices", wrapped_build_lsoa_indices)

    station_df_before = pd.DataFrame(
        {
            "StationID": [1, 2, 3, 4],
            "lsoa_code": [EV_LSOA, EV_LSOA, EV_LSOA, EV_LSOA],
            "label": ["home", "work", "shopping", "home"],
            "TotalCapacity_kW": [7.0, 22.0, 3.6, 11.0],
            "station_attractiveness": [1.0, 3.0, 1.5, 2.0],
        }
    )
    station_df_copy = station_df_before.copy(deep=True)

    match_stations_for_fleet(
        fleet_schedules={"ev_1": [_build_schedule()]},
        ev_fleet=pd.DataFrame(
            {
                "EV_ID": ["ev_1"],
                "home_lsoa": [EV_LSOA],
                "LSOA_code": [EV_LSOA],
                "ac_power_kw": [7.0],
            }
        ),
        stations_df=station_df_before,
        rng=np.random.default_rng(42),
        centroids=_build_centroids(),
    )

    pd.testing.assert_frame_equal(station_df_before, station_df_copy)
    assert captured["labels"] == station_df_copy["label"].tolist()


def test_non_home_station_matching_is_bit_identical_to_legacy_baseline() -> None:
    station_df = _build_precleaned_station_df()
    station_lookup = {
        (EV_LSOA, row.label): (int(row.StationID), float(row.TotalCapacity_kW))
        for row in station_df.itertuples(index=False)
    }
    legacy_schedule = _build_schedule()
    current_schedule = deepcopy(legacy_schedule)

    _legacy_match_stations_for_schedule(
        schedule=legacy_schedule,
        ev_lsoa=EV_LSOA,
        ev_ac_power_kw=11.0,
        station_lookup=station_lookup,
    )
    match_stations_for_schedule(
        schedule=current_schedule,
        ev_home_lsoa=EV_LSOA,
        ev_ac_power_kw=11.0,
        stations_df=station_df,
        rng=np.random.default_rng(0),
        centroids=_build_centroids(),
        date_iso="day000",
        _indices=_build_lsoa_indices(station_df),
    )

    assert _non_home_state(current_schedule) == _non_home_state(legacy_schedule)

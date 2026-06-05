from __future__ import annotations

import pandas as pd

from mobility.bus.depot_only_events import FORBIDDEN_CHARGING_EVENT_TYPES, build_vehicle_day_events


def _case(end_h: float = 18.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "simulation_case_id": ["case_1"],
            "service_date": ["2026-06-03"],
            "vehicle_id": ["bus_1"],
            "vehicle_model": ["A"],
            "vehicle_subtype": ["bus"],
            "block_template_id": ["bt1"],
            "block_id": ["B1"],
            "agency_id": ["OP"],
            "sample_mode": ["full_ev_inventory"],
            "start_h": [8.0],
            "end_h": [end_h],
            "passenger_distance_km": [100.0],
            "start_lat": [51.0],
            "start_lon": [-1.0],
            "end_lat": [51.0],
            "end_lon": [-1.0],
            "start_lsoa": ["E1"],
            "end_lsoa": ["E1"],
            "region_key": ["London"],
            "depot_id": ["opdepot_OP_E1"],
            "operational_depot_lsoa": ["E1"],
            "depot_lat": [51.0],
            "depot_lon": [-1.0],
            "battery_kwh": [300.0],
            "consumption_kwh_per_km": [1.0],
            "ac_charge_kw_max": [80.0],
            "dc_charge_kw_max": [150.0],
            "usable_soc_min": [0.1],
            "usable_soc_max": [0.95],
            "trip_ids": [["t1", "t2"]],
            "trip_start_times": [[8.0, 12.0]],
            "trip_end_times": [[9.0, end_h]],
            "trip_start_lats": [[51.0, 51.0]],
            "trip_start_lons": [[-1.0, -1.0]],
            "trip_end_lats": [[51.0, 51.0]],
            "trip_end_lons": [[-1.0, -1.0]],
            "trip_distances_km": [[20.0, 80.0]],
            "trip_start_lsoas": [["E1", "E1"]],
            "trip_end_lsoas": [["E1", "E1"]],
        }
    )


def test_every_case_has_event_ledger() -> None:
    events = build_vehicle_day_events(_case())
    assert not events.empty
    assert set(events["simulation_case_id"]) == {"case_1"}


def test_sequence_starts_and_ends_at_depot() -> None:
    events = build_vehicle_day_events(_case())
    assert events.iloc[0]["event_type"] == "depot_parking_pre"
    assert events.iloc[-1]["event_type"] == "depot_parking_post"
    assert events.iloc[0]["start_lsoa"] == "E1"
    assert events.iloc[-1]["end_lsoa"] == "E1"


def test_strictly_increasing_event_seq() -> None:
    events = build_vehicle_day_events(_case())
    assert events["event_seq"].tolist() == list(range(len(events)))


def test_deadhead_events_present_and_no_public_charging() -> None:
    events = build_vehicle_day_events(_case())
    assert {"depot_to_block_deadhead", "block_to_depot_deadhead"}.issubset(set(events["event_type"]))
    assert set(events["event_type"]).isdisjoint(FORBIDDEN_CHARGING_EVENT_TYPES)


def test_post_parking_extends_past_midnight_and_not_truncated() -> None:
    events = build_vehicle_day_events(_case())
    post = events.iloc[-1]
    assert post["end_datetime"] == pd.Timestamp("2026-06-04 06:00")
    assert post["end_datetime"] > pd.Timestamp("2026-06-04 00:00")


def test_late_return_fallback_6h_after_return() -> None:
    events = build_vehicle_day_events(_case(end_h=31.0))
    post = events.iloc[-1]
    assert post["overnight_window_method"] == "fallback_6h_after_return"
    assert post["end_datetime"] - post["start_datetime"] == pd.Timedelta(hours=6)


def test_midday_depot_parking_inserted_when_valid() -> None:
    events = build_vehicle_day_events(_case())
    assert "depot_parking_midday" in set(events["event_type"])

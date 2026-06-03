from __future__ import annotations

import pandas as pd

from mobility.bus.annual_depot_artifacts import (
    build_bus_charging_event_records,
    build_bus_ev_state_records,
    build_bus_trip_records,
)
from mobility.bus.annual_depot_events import build_vehicle_day_events
from mobility.bus.annual_depot_soc import apply_depot_only_soc


def _soc_events() -> pd.DataFrame:
    assignments = pd.DataFrame(
        {
            "service_date": ["2026-04-17"],
            "vehicle_day_id": ["vd1"],
            "vehicle_spec_id": ["ev1"],
            "block_instance_id": ["bi1"],
            "block_template_id": ["bt1"],
            "agency_id": ["OP"],
            "service_id": ["S1"],
            "block_id": ["B1"],
            "depot_id": ["opdepot_OP_E1"],
            "region_key": ["London"],
            "scenario_mode": ["ev_stock_scale"],
        }
    )
    block_instances = pd.DataFrame(
        {
            "service_date": ["2026-04-17"],
            "block_instance_id": ["bi1"],
            "block_template_id": ["bt1"],
            "agency_id": ["OP"],
            "service_id": ["S1"],
            "block_id": ["B1"],
            "start_datetime": [pd.Timestamp("2026-04-17 08:00")],
            "end_datetime": [pd.Timestamp("2026-04-17 10:00")],
            "start_h": [8.0],
            "end_h": [10.0],
            "duration_h": [2.0],
            "passenger_distance_km": [20.0],
            "start_lat": [51.0],
            "start_lon": [-1.0],
            "end_lat": [51.0],
            "end_lon": [-1.0],
            "start_lsoa": ["E1"],
            "end_lsoa": ["E1"],
            "region_key": ["London"],
            "depot_id": ["opdepot_OP_E1"],
            "depot_lsoa": ["E1"],
        }
    )
    templates = pd.DataFrame(
        {
            "block_template_id": ["bt1"],
            "trip_ids": [["trip_a"]],
            "trip_start_times": [[8.0]],
            "trip_end_times": [[10.0]],
            "trip_start_lats": [[51.0]],
            "trip_start_lons": [[-1.0]],
            "trip_end_lats": [[51.0]],
            "trip_end_lons": [[-1.0]],
            "trip_distances_km": [[20.0]],
            "trip_start_lsoas": [["E1"]],
            "trip_end_lsoas": [["E1"]],
        }
    )
    specs = pd.DataFrame(
        {
            "vehicle_spec_id": ["ev1"],
            "battery_kwh": [100.0],
            "consumption_kwh_per_km": [1.0],
            "ac_charge_kw_max": [50.0],
            "usable_soc_min": [0.10],
            "usable_soc_max": [0.95],
        }
    )
    registry = pd.DataFrame(
        {
            "depot_id": ["opdepot_OP_E1"],
            "depot_lat": [51.0],
            "depot_lon": [-1.0],
            "depot_lsoa": ["E1"],
            "depot_confidence": ["high"],
        }
    )
    events = build_vehicle_day_events(assignments, block_instances, templates, specs, registry, depot_power_kw=100.0)
    events, _ = apply_depot_only_soc(events, depot_power_kw=100.0)
    return events


def test_bus_trip_records_align_private_car_trip_fields() -> None:
    records = build_bus_trip_records(_soc_events(), feed_year_start="2026-04-17")

    required = {
        "ev_id",
        "person_id",
        "origin_lsoa",
        "destination_lsoa",
        "departure_time",
        "arrival_time",
        "soc_before_trip",
        "soc_after_trip",
    }
    assert required.issubset(records.columns)
    assert records.loc[0, "trip_id"] == "trip_a"
    assert records.loc[0, "origin_lsoa"] == "E1"
    assert records.loc[0, "destination_lsoa"] == "E1"
    assert records.loc[0, "soc_after_trip"] <= records.loc[0, "soc_before_trip"]


def test_bus_charging_events_align_private_car_charging_fields() -> None:
    charging = build_bus_charging_event_records(_soc_events(), feed_year_start="2026-04-17")

    required = {
        "ev_id",
        "event_id",
        "charging_start_time",
        "charging_end_time",
        "charging_lsoa",
        "home_lsoa",
        "charging_type",
        "station_id",
        "charged_energy_kwh",
        "soc_before_charging",
        "soc_after_charging",
    }
    assert required.issubset(charging.columns)
    assert set(charging["charging_type"]) == {"depot"}
    assert charging["station_id"].eq("opdepot_OP_E1").all()


def test_bus_ev_state_records_track_15min_location_lsoa() -> None:
    state = build_bus_ev_state_records(_soc_events(), feed_year_start="2026-04-17")

    required = {
        "ev_id",
        "time_bin_start",
        "time_bin_end",
        "current_lsoa",
        "origin_lsoa",
        "destination_lsoa",
        "location_status",
        "soc_start",
        "soc_end",
    }
    assert required.issubset(state.columns)
    assert not state.empty
    assert "parked_depot" in set(state["location_status"])
    assert "in_service" in set(state["location_status"])
    assert state.loc[state["location_status"].eq("parked_depot"), "current_lsoa"].eq("E1").all()

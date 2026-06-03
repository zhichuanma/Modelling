from __future__ import annotations

import pandas as pd

from mobility.bus.annual_depot_events import build_vehicle_day_events


def _inputs():
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
            "block_source": ["native"],
            "start_datetime": [pd.Timestamp("2026-04-17 08:00")],
            "end_datetime": [pd.Timestamp("2026-04-17 18:00")],
            "start_h": [8.0],
            "end_h": [18.0],
            "duration_h": [10.0],
            "passenger_distance_km": [100.0],
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
            "trip_ids": [["t1", "t2"]],
            "trip_start_times": [[8.0, 12.0]],
            "trip_end_times": [[9.0, 18.0]],
            "trip_start_lats": [[51.0, 51.0]],
            "trip_start_lons": [[-1.0, -1.0]],
            "trip_end_lats": [[51.0, 51.0]],
            "trip_end_lons": [[-1.0, -1.0]],
            "trip_distances_km": [[20.0, 80.0]],
            "trip_start_lsoas": [["E1", "E1"]],
            "trip_end_lsoas": [["E1", "E1"]],
        }
    )
    specs = pd.DataFrame(
        {
            "vehicle_spec_id": ["ev1"],
            "battery_kwh": [300.0],
            "consumption_kwh_per_km": [1.0],
            "ac_charge_kw_max": [80.0],
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
    return assignments, block_instances, templates, specs, registry


def _events() -> pd.DataFrame:
    return build_vehicle_day_events(*_inputs(), depot_power_kw=100.0)


def test_every_vehicle_day_has_events() -> None:
    assert not _events().empty


def test_event_sequence_starts_and_ends_at_depot() -> None:
    events = _events()
    assert events.iloc[0]["event_type"] == "depot_parking_pre"
    assert events.iloc[-1]["event_type"] == "depot_parking_overnight"


def test_overnight_charging_can_cross_midnight() -> None:
    events = _events()
    overnight = events[events["event_type"].eq("depot_parking_overnight")].iloc[0]
    assert overnight["end_datetime"].strftime("%Y-%m-%d") == "2026-04-18"


def test_midday_depot_window_inserted_when_layover_at_depot() -> None:
    assert "depot_parking_midday" in set(_events()["event_type"])


def test_no_public_charging_events_created() -> None:
    forbidden = {"public_charger_event", "opportunity_charging", "terminal_public_charging", "OCM_station_event"}
    assert set(_events()["event_type"]).isdisjoint(forbidden)

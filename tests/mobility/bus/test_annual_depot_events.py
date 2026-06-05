from __future__ import annotations

import pandas as pd

from mobility.bus.annual_depot_events import build_vehicle_day_events, haversine_km
from mobility.bus.annual_depot_soc import apply_depot_only_soc


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


def test_late_cross_midnight_return_still_gets_charging_window() -> None:
    """A block returning after the scheduled overnight end (next-day 06:00) must
    still get a post-return depot charging window (return_dt + overnight hours),
    not silently lose its recharge load."""
    assignments, block_instances, templates, specs, registry = _inputs()
    block_instances = block_instances.assign(
        end_datetime=[pd.Timestamp("2026-04-18 06:30")],
        end_h=[30.5],
        duration_h=[22.5],
    )
    templates = templates.assign(
        trip_start_times=[[8.0, 12.0]],
        trip_end_times=[[9.0, 30.5]],
    )
    events = build_vehicle_day_events(assignments, block_instances, templates, specs, registry, depot_power_kw=100.0)
    last = events.iloc[-1]
    assert last["event_type"] == "depot_parking_post"
    assert bool(last["can_charge"])
    assert last["start_datetime"] >= pd.Timestamp("2026-04-18 06:30")
    assert (last["end_datetime"] - last["start_datetime"]) == pd.Timedelta(hours=6)


def test_midday_depot_window_inserted_when_layover_at_depot() -> None:
    assert "depot_parking_midday" in set(_events()["event_type"])


def test_no_public_charging_events_created() -> None:
    forbidden = {"public_charger_event", "opportunity_charging", "terminal_public_charging", "OCM_station_event"}
    assert set(_events()["event_type"]).isdisjoint(forbidden)


def test_home_depot_override_uses_home_depot_for_deadhead_and_lsoa() -> None:
    assignments, block_instances, templates, specs, registry = _inputs()
    registry = pd.concat(
        [
            registry,
            pd.DataFrame(
                {
                    "depot_id": ["opdepot_OP_HOME"],
                    "depot_lat": [51.05],
                    "depot_lon": [-1.05],
                    "depot_lsoa": ["E2"],
                    "depot_confidence": ["high"],
                }
            ),
        ],
        ignore_index=True,
    )
    assignments["home_depot_id"] = ["opdepot_OP_HOME"]
    events = build_vehicle_day_events(assignments, block_instances, templates, specs, registry, depot_power_kw=100.0)
    assert set(events["depot_id"]) == {"opdepot_OP_HOME"}
    assert set(events["depot_lsoa"]) == {"E2"}
    deadhead = events[events["event_type"].eq("depot_to_block_deadhead")].iloc[0]
    expected_km = haversine_km(51.05, -1.05, 51.0, -1.0)
    assert abs(deadhead["distance_km"] - expected_km) < 1e-9


def test_empty_home_depot_id_keeps_block_attached_depot() -> None:
    assignments, block_instances, templates, specs, registry = _inputs()
    assignments["home_depot_id"] = [""]
    events = build_vehicle_day_events(assignments, block_instances, templates, specs, registry, depot_power_kw=100.0)
    assert set(events["depot_id"]) == {"opdepot_OP_E1"}
    assert set(events["depot_lsoa"]) == {"E1"}


def test_home_depot_soc_walk_matches_feasibility_screen() -> None:
    """Regression: PR 1.5 screen budgets deadhead via the home depot; the SOC walk
    must use the same depot. With the block-attached depot (far '_missing'-style
    anchor) this vehicle-day would breach; via the home depot it is feasible."""
    assignments = pd.DataFrame(
        {
            "service_date": ["2026-04-17"],
            "vehicle_day_id": ["vd9"],
            "vehicle_spec_id": ["ev9"],
            "block_instance_id": ["bi9"],
            "block_template_id": ["bt9"],
            "agency_id": ["OP"],
            "service_id": ["S9"],
            "block_id": ["B9"],
            "depot_id": ["opdepot_OP_missing"],
            "home_depot_id": ["opdepot_OP_HOME"],
            "region_key": ["unknown"],
            "scenario_mode": ["ev_stock_scale"],
        }
    )
    block_instances = pd.DataFrame(
        {
            "service_date": ["2026-04-17"],
            "block_instance_id": ["bi9"],
            "block_template_id": ["bt9"],
            "agency_id": ["OP"],
            "service_id": ["S9"],
            "block_id": ["B9"],
            "start_datetime": [pd.Timestamp("2026-04-17 08:00")],
            "end_datetime": [pd.Timestamp("2026-04-17 18:00")],
            "duration_h": [10.0],
            "passenger_distance_km": [54.8],
            "start_lat": [51.0],
            "start_lon": [-1.0],
            "end_lat": [51.1],
            "end_lon": [-1.0],
            "start_lsoa": ["E1"],
            "end_lsoa": ["E3"],
            "depot_id": ["opdepot_OP_missing"],
            "depot_lsoa": [""],
        }
    )
    templates = pd.DataFrame({"block_template_id": ["bt9"]})
    specs = pd.DataFrame(
        {
            "vehicle_spec_id": ["ev9"],
            "battery_kwh": [100.0],
            "consumption_kwh_per_km": [1.0],
            "ac_charge_kw_max": [80.0],
            "usable_soc_min": [0.10],
            "usable_soc_max": [0.95],
        }
    )
    registry = pd.DataFrame(
        {
            "depot_id": ["opdepot_OP_missing", "opdepot_OP_HOME"],
            "depot_lat": [51.5, 51.05],
            "depot_lon": [-1.5, -1.0],
            "depot_lsoa": ["", "E2"],
            "depot_confidence": ["missing", "high"],
        }
    )
    events = build_vehicle_day_events(assignments, block_instances, templates, specs, registry, depot_power_kw=100.0)
    _, summary = apply_depot_only_soc(events, depot_power_kw=100.0)
    row = summary.iloc[0]
    expected_deadhead = haversine_km(51.05, -1.0, 51.0, -1.0) + haversine_km(51.1, -1.0, 51.05, -1.0)
    # screen budget: 54.8 passenger + ~11.1 home deadhead = ~65.9 km <= 85 km range
    assert abs(row["total_deadhead_km"] - expected_deadhead) < 1e-9
    assert bool(row["depot_only_feasible"])
    assert row["energy_shortfall_kwh"] == 0.0
    assert row["depot_id"] == "opdepot_OP_HOME"
    # via the far block-attached depot the same day would have breached
    block_depot_deadhead = haversine_km(51.5, -1.5, 51.0, -1.0) + haversine_km(51.1, -1.0, 51.5, -1.5)
    assert 54.8 + block_depot_deadhead > 85.0

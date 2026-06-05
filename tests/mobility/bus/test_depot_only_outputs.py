from __future__ import annotations

import pandas as pd
import pytest

from mobility.bus.depot_only_outputs import aggregate_depot_load_15min, build_run_summary, depot_load_energy_matches_events


def _events() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "simulation_case_id": "case_1",
                "service_date": "2026-06-03",
                "depot_id": "opdepot_OP_E1",
                "operational_depot_lsoa": "E1",
                "region_key": "London",
                "event_type": "depot_parking_post",
                "start_datetime": pd.Timestamp("2026-06-03 23:30"),
                "end_datetime": pd.Timestamp("2026-06-04 00:30"),
                "charging_end_datetime": pd.NaT,
                "charge_kwh_added": 60.0,
                "sample_mode": "full_ev_inventory",
                "weighting_mode": "unweighted_ev_stock_scenario",
            },
            {
                "simulation_case_id": "case_2",
                "service_date": "2026-06-03",
                "depot_id": "public_1",
                "operational_depot_lsoa": "E2",
                "region_key": "London",
                "event_type": "public_charger_event",
                "start_datetime": pd.Timestamp("2026-06-03 12:00"),
                "end_datetime": pd.Timestamp("2026-06-03 12:30"),
                "charging_end_datetime": pd.NaT,
                "charge_kwh_added": 999.0,
                "sample_mode": "full_ev_inventory",
                "weighting_mode": "unweighted_ev_stock_scenario",
            },
        ]
    )


def test_depot_load_only_uses_depot_charging_and_no_public_station() -> None:
    load = aggregate_depot_load_15min(_events())
    assert load["charge_kwh"].sum() == pytest.approx(60.0)
    assert "public_1" not in set(load["depot_id"])


def test_depot_load_supports_cross_midnight_slots() -> None:
    load = aggregate_depot_load_15min(_events())
    assert pd.Timestamp("2026-06-04 00:00") in set(load["slot_start"])


def test_depot_load_energy_matches_event_ledger() -> None:
    load = aggregate_depot_load_15min(_events())
    assert depot_load_energy_matches_events(load, _events())


def test_depot_load_required_columns_and_weighting_mode() -> None:
    load = aggregate_depot_load_15min(_events())
    required = {
        "depot_id",
        "operational_depot_lsoa",
        "region_key",
        "time_slot",
        "slot_start",
        "slot_end",
        "charge_kwh",
        "average_kw",
        "n_active_cases",
        "sample_mode",
        "weighting_mode",
    }
    assert required.issubset(load.columns)
    assert set(load["weighting_mode"]) == {"unweighted_ev_stock_scenario"}


def test_run_summary_mentions_estimand_and_limitations() -> None:
    summary = build_run_summary(
        preflight_summary={
            "n_trip_rows_raw": 2,
            "n_block_templates_available": 1,
            "n_valid_ev_bus_instances": 1,
            "minibus_count_note": "minibus count is 0",
            "low_consumption_filtered_count": 0,
            "invalid_battery_vehicle_count": 0,
            "ev_id_is_unique": True,
            "count_matches_lsoa_model_group_size": True,
            "lsoa_attach": {"both_endpoint_lsoa_hit_rate": 1.0},
        },
        sampled_blocks=pd.DataFrame({"region_key": ["London"], "block_template_id": ["bt1"]}),
        block_sample_diagnostics=pd.DataFrame({"any_region_cap_breach": [False]}),
        operational_depot_registry=pd.DataFrame({"depot_id": ["D1"], "depot_confidence": ["high"]}),
        simulation_cases=pd.DataFrame({"vehicle_model": ["A"], "vehicle_subtype": ["bus"]}),
        vehicle_day_events=_events().iloc[:1],
        case_soc_summary=pd.DataFrame(
            {
                "simulation_case_id": ["case_1"],
                "depot_only_feasible": [True],
                "energy_shortfall_kwh": [0.0],
                "block_template_id": ["bt1"],
                "vehicle_id": ["bus_1"],
            }
        ),
        depot_load_15min=aggregate_depot_load_15min(_events()),
        sample_mode="full_ev_inventory",
    )
    assert "current EV bus stock" in summary
    assert "not a real garage" in summary
    assert "not a national all-bus electrification total" in summary
    assert "minibus count is 0" in summary

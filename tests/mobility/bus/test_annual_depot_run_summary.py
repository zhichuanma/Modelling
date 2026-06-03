from __future__ import annotations

import pandas as pd

from mobility.bus.annual_depot_outputs import build_run_summary_markdown, write_run_summary


def _markdown() -> str:
    return build_run_summary_markdown(
        preflight_summary={"n_trip_rows": 2, "minibus_row_count": 1, "n_ev_specs_dropped_by_sanity": 0},
        block_templates=pd.DataFrame({"start_lsoa": ["E1"], "end_lsoa": ["E1"]}),
        block_instances=pd.DataFrame({"block_instance_id": ["bi1"]}),
        depot_registry=pd.DataFrame({"depot_id": ["D1"], "is_physical_depot": [False], "is_operational_anchor": [True], "depot_confidence": ["high"]}),
        ev_bus_specs=pd.DataFrame({"vehicle_spec_id": ["ev1"]}),
        vehicle_day_assignments=pd.DataFrame({"region_key": ["London"]}),
        vehicle_day_soc_summary=pd.DataFrame({"vehicle_day_id": ["vd1"], "depot_only_feasible": [True], "total_energy_kwh": [10.0], "total_deadhead_km": [1.0], "energy_shortfall_kwh": [0.0], "block_instance_id": ["bi1"]}),
        depot_load_15min=pd.DataFrame({"depot_id": ["D1"], "charge_kwh": [5.0], "average_kw": [20.0]}),
        depot_daily_summary=pd.DataFrame(),
        feed_year_start="2026-04-17",
        feed_year_end="2026-04-17",
        scenario_mode="ev_stock_scale",
    )


def test_run_summary_is_written(tmp_path) -> None:
    path = write_run_summary(_markdown(), tmp_path)
    assert path.exists()


def test_run_summary_contains_key_counts() -> None:
    md = _markdown()
    assert "n_block_templates: 1" in md
    assert "n_vehicle_day_assignments: 1" in md
    assert "n_unassigned_block_instances_under_ev_stock_scale" in md


def test_run_summary_contains_depot_limitations() -> None:
    assert "not verified physical garage locations" in _markdown()


def test_run_summary_states_no_public_charging() -> None:
    assert "public charging and opportunity charging are not modelled" in _markdown()


def test_run_summary_states_no_multiday_soc_carryover() -> None:
    assert "multi-day SOC carry-over" in _markdown()

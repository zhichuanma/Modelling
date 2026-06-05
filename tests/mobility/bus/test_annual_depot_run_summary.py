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


def test_feasible_share_is_labelled_matched_vehicle_day() -> None:
    md = _markdown()
    assert "matched_vehicle_day_feasible_share: 1.0000" in md
    assert "depot_only_feasible_share" not in md
    assert "denominator is matched vehicle-days only" in md


def test_matched_share_lines_from_diagnostics() -> None:
    diagnostics = pd.DataFrame(
        {
            "service_date": ["2026-04-17"],
            "n_matched_feasible_block_instances_for_service_date": [3],
            "n_sampled_block_instances_for_service_date": [4],
            "n_active_block_instances_for_service_date": [10],
            "n_unmatched_no_feasible_vehicle": [1],
            "n_unmatched_no_vehicle_in_radius": [0],
            "n_unmatched_no_feasible_vehicle_in_radius": [0],
            "n_unmatched_lost_matching_competition": [0],
        }
    )
    md = build_run_summary_markdown(
        preflight_summary={"n_trip_rows": 2, "minibus_row_count": 1, "n_ev_specs_dropped_by_sanity": 0},
        block_templates=pd.DataFrame({"start_lsoa": ["E1"], "end_lsoa": ["E1"]}),
        block_instances=pd.DataFrame({"block_instance_id": ["bi1"]}),
        depot_registry=pd.DataFrame({"depot_id": ["D1"], "is_physical_depot": [False], "is_operational_anchor": [True], "depot_confidence": ["high"]}),
        ev_bus_specs=pd.DataFrame({"vehicle_spec_id": ["ev1"]}),
        vehicle_day_assignments=pd.DataFrame({"region_key": ["London"]}),
        assignment_diagnostics=diagnostics,
        vehicle_day_soc_summary=pd.DataFrame({"vehicle_day_id": ["vd1"], "depot_only_feasible": [True], "total_energy_kwh": [10.0], "total_deadhead_km": [1.0], "energy_shortfall_kwh": [0.0], "block_instance_id": ["bi1"]}),
        depot_load_15min=pd.DataFrame({"depot_id": ["D1"], "charge_kwh": [5.0], "average_kw": [20.0]}),
        depot_daily_summary=pd.DataFrame(),
        feed_year_start="2026-04-17",
        feed_year_end="2026-04-17",
        scenario_mode="ev_stock_scale",
    )
    assert "matched_sample_share: 0.7500" in md
    assert "matched_active_block_share: 0.3000" in md
    assert "n_unmatched_sampled_blocks_no_feasible_vehicle: 1" in md
    # Radius sub-decomposition is all-zero here, so it must be suppressed to
    # avoid implying an extra unmatched bucket.
    assert "of which" not in md


def test_unknown_depot_load_is_isolated() -> None:
    md = build_run_summary_markdown(
        preflight_summary={"n_trip_rows": 2, "minibus_row_count": 1, "n_ev_specs_dropped_by_sanity": 0},
        block_templates=pd.DataFrame({"start_lsoa": ["E1"], "end_lsoa": ["E1"]}),
        block_instances=pd.DataFrame({"block_instance_id": ["bi1"]}),
        depot_registry=pd.DataFrame({"depot_id": ["D1"], "is_physical_depot": [False], "is_operational_anchor": [True], "depot_confidence": ["high"]}),
        ev_bus_specs=pd.DataFrame({"vehicle_spec_id": ["ev1"]}),
        vehicle_day_assignments=pd.DataFrame({"region_key": ["London", "London"], "deadhead_estimate_incomplete": [True, False]}),
        vehicle_day_soc_summary=pd.DataFrame({"vehicle_day_id": ["vd1"], "depot_only_feasible": [True], "total_energy_kwh": [10.0], "total_deadhead_km": [1.0], "energy_shortfall_kwh": [0.0], "block_instance_id": ["bi1"]}),
        depot_load_15min=pd.DataFrame(
            {
                "depot_id": ["opdepot_OP1_E1", "opdepot_OP2_missing"],
                "depot_lsoa": ["E1", ""],
                "charge_kwh": [6.0, 2.0],
                "average_kw": [24.0, 8.0],
            }
        ),
        depot_daily_summary=pd.DataFrame(),
        feed_year_start="2026-04-17",
        feed_year_end="2026-04-17",
        scenario_mode="ev_stock_scale",
    )
    assert "## Unknown-depot load isolation" in md
    assert "unknown_depot_charge_kwh: 2.000" in md
    assert "unknown_depot_charge_share: 0.2500" in md
    assert "n_unknown_depots_with_load: 1" in md
    assert "n_vehicle_days_deadhead_incomplete: 1" in md
    assert "share_vehicle_days_deadhead_incomplete: 0.5000" in md
    assert "must not be mapped spatially" in md

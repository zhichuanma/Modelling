from __future__ import annotations

import numpy as np
import pandas as pd

from mobility.bus.annual_vehicle_day_assignment import (
    FEASIBLE_ASSIGNMENT_METHOD,
    build_feasible_vehicle_day_assignments,
    build_vehicle_day_assignments,
)


def _instances(passenger_km: list[float], *, service_date: str = "2026-04-17", with_coords: bool = False) -> pd.DataFrame:
    n = len(passenger_km)
    frame = pd.DataFrame(
        {
            "service_date": [service_date] * n,
            "block_instance_id": [f"bi{i}" for i in range(n)],
            "block_template_id": [f"bt{i}" for i in range(n)],
            "agency_id": ["OP"] * n,
            "service_id": ["S1"] * n,
            "block_id": [f"B{i}" for i in range(n)],
            "depot_id": ["opdepot_OP_E1"] * n,
            "region_key": ["London"] * n,
            "passenger_distance_km": passenger_km,
            "duration_h": [max(0.5, km / 20.0) for km in passenger_km],
        }
    )
    if with_coords:
        frame["start_lat"] = 51.5
        frame["start_lon"] = 0.5
        frame["end_lat"] = 51.5
        frame["end_lon"] = 0.5
    return frame


def _specs(range_km: list[float]) -> pd.DataFrame:
    # usable window [0.0, 1.0] and consumption 1.0 make range_km == battery_kwh.
    return pd.DataFrame(
        {
            "vehicle_spec_id": [f"ev{i}" for i in range(len(range_km))],
            "battery_kwh": [float(km) for km in range_km],
            "consumption_kwh_per_km": [1.0] * len(range_km),
            "usable_soc_min": [0.0] * len(range_km),
            "usable_soc_max": [1.0] * len(range_km),
        }
    )


def _depot_registry(lat: float = 51.5, lon: float = 0.0) -> pd.DataFrame:
    return pd.DataFrame({"depot_id": ["opdepot_OP_E1"], "depot_lat": [lat], "depot_lon": [lon]})


def test_never_emits_statically_infeasible_pair() -> None:
    assignments, _, _ = build_feasible_vehicle_day_assignments(_instances([10.0, 50.0, 200.0]), _specs([60.0, 60.0]), seed=1)
    assert not assignments.empty
    assert (assignments["required_kwh_est"] <= assignments["available_kwh_at_assignment"]).all()
    assert assignments["assignment_status"].eq("matched_feasible").all()


def test_block_with_no_feasible_vehicle_is_unmatched() -> None:
    assignments, diagnostics, unmatched = build_feasible_vehicle_day_assignments(_instances([10.0, 500.0]), _specs([60.0, 60.0]), seed=1)
    assert len(assignments) == 1
    assert assignments["block_id"].iloc[0] == "B0"
    assert len(unmatched) == 1
    assert unmatched["unmatched_reason"].iloc[0] == "no_feasible_vehicle"
    assert unmatched["n_feasible_vehicles"].iloc[0] == 0
    assert diagnostics["n_unmatched_no_feasible_vehicle"].iloc[0] == 1
    assert diagnostics["n_unmatched_lost_matching_competition"].iloc[0] == 0


def test_competition_loss_is_reported() -> None:
    # Two big blocks, only one vehicle can run them.
    assignments, diagnostics, unmatched = build_feasible_vehicle_day_assignments(_instances([100.0, 110.0]), _specs([150.0, 20.0]), seed=1)
    assert len(assignments) == 1
    assert len(unmatched) == 1
    assert unmatched["unmatched_reason"].iloc[0] == "lost_matching_competition"
    assert unmatched["n_feasible_vehicles"].iloc[0] >= 1
    assert diagnostics["n_unmatched_lost_matching_competition"].iloc[0] == 1


def test_matching_is_maximum_cardinality() -> None:
    # ev0 covers both blocks, ev1 covers only the small one. A careless pairing
    # (ev0 -> small block) would strand the big block; maximum matching covers both.
    for seed in range(10):
        assignments, diagnostics, unmatched = build_feasible_vehicle_day_assignments(_instances([100.0, 10.0]), _specs([150.0, 20.0]), seed=seed)
        assert len(assignments) == 2, f"seed={seed}"
        assert unmatched.empty
        big = assignments.loc[assignments["block_id"] == "B0"]
        assert big["vehicle_spec_id"].iloc[0] == "ev0"
        assert diagnostics["matched_sample_share"].iloc[0] == 1.0


def test_each_vehicle_and_block_used_at_most_once_per_day() -> None:
    assignments, _, _ = build_feasible_vehicle_day_assignments(_instances([10.0, 20.0, 30.0, 40.0]), _specs([60.0, 60.0, 60.0]), seed=3)
    assert assignments["vehicle_spec_id"].is_unique
    assert assignments["block_instance_id"].is_unique
    assert len(assignments) == 3


def test_deterministic_for_fixed_seed() -> None:
    first = build_feasible_vehicle_day_assignments(_instances([10.0, 50.0, 90.0]), _specs([100.0, 60.0]), seed=42)
    second = build_feasible_vehicle_day_assignments(_instances([10.0, 50.0, 90.0]), _specs([100.0, 60.0]), seed=42)
    for a, b in zip(first, second):
        pd.testing.assert_frame_equal(a, b)


def test_deadhead_included_in_feasibility() -> None:
    # Depot ~34.7 km from block start/end -> ~69 km round-trip deadhead.
    instances = _instances([30.0], with_coords=True)
    registry = _depot_registry()
    specs = _specs([50.0])  # enough for passenger km, not for passenger + deadhead

    assignments, _, unmatched = build_feasible_vehicle_day_assignments(instances, specs, registry, seed=1)
    assert assignments.empty
    assert len(unmatched) == 1
    assert unmatched["deadhead_km_est"].iloc[0] > 60.0

    # Without depot coordinates, deadhead falls back to 0 km and is flagged.
    assignments_nc, _, unmatched_nc = build_feasible_vehicle_day_assignments(_instances([30.0]), specs, seed=1)
    assert len(assignments_nc) == 1
    assert unmatched_nc.empty
    assert bool(assignments_nc["deadhead_estimate_incomplete"].iloc[0])
    assert assignments_nc["deadhead_km_est"].iloc[0] == 0.0


def test_deadhead_matches_event_stage_haversine() -> None:
    from mobility.bus.annual_depot_events import haversine_km

    instances = _instances([30.0], with_coords=True)
    registry = _depot_registry()
    assignments, _, _ = build_feasible_vehicle_day_assignments(instances, _specs([200.0]), registry, seed=1)
    expected = haversine_km(51.5, 0.0, 51.5, 0.5) + haversine_km(51.5, 0.5, 51.5, 0.0)
    assert np.isclose(assignments["deadhead_km_est"].iloc[0], expected)


def test_sample_block_multiplier_caps_sampled_blocks() -> None:
    _, diagnostics, _ = build_feasible_vehicle_day_assignments(_instances([10.0] * 8), _specs([60.0, 60.0]), seed=1, sample_block_multiplier=2.0)
    assert diagnostics["n_sampled_block_instances_for_service_date"].iloc[0] == 4
    _, diagnostics_default, _ = build_feasible_vehicle_day_assignments(_instances([10.0] * 8), _specs([60.0, 60.0]), seed=1)
    assert diagnostics_default["n_sampled_block_instances_for_service_date"].iloc[0] == 2


def test_diagnostics_counts_are_consistent() -> None:
    assignments, diagnostics, unmatched = build_feasible_vehicle_day_assignments(_instances([10.0, 100.0, 500.0]), _specs([150.0, 20.0]), seed=7)
    row = diagnostics.iloc[0]
    assert row["n_sampled_block_instances_for_service_date"] == len(assignments) + len(unmatched)
    assert row["n_matched_feasible_block_instances_for_service_date"] == len(assignments)
    assert row["n_unmatched_sampled_block_instances_for_service_date"] == len(unmatched)
    assert row["n_unmatched_no_feasible_vehicle"] + row["n_unmatched_lost_matching_competition"] == len(unmatched)
    # Legacy-named compatibility columns.
    assert row["n_available_block_instances_for_service_date"] == 3
    assert row["n_assigned_block_instances_for_service_date"] == len(assignments)
    assert row["n_unassigned_block_instances_for_service_date"] == 3 - len(assignments)


def test_assignments_keep_event_builder_merge_keys() -> None:
    assignments, _, _ = build_feasible_vehicle_day_assignments(_instances([10.0]), _specs([60.0]), seed=1)
    merge_keys = {"service_date", "block_instance_id", "block_template_id", "agency_id", "service_id", "block_id"}
    assert merge_keys.issubset(assignments.columns)
    assert {"vehicle_day_id", "vehicle_spec_id", "depot_id", "scenario_mode"}.issubset(assignments.columns)
    assert assignments["assignment_method"].eq(FEASIBLE_ASSIGNMENT_METHOD).all()
    assert assignments["daily_soc_mode"].eq("daily_reset").all()


def test_invalid_spec_params_are_never_matched() -> None:
    specs = _specs([60.0, 60.0])
    specs.loc[1, "consumption_kwh_per_km"] = np.nan
    assignments, diagnostics, _ = build_feasible_vehicle_day_assignments(_instances([10.0, 20.0]), specs, seed=1)
    assert assignments["vehicle_spec_id"].eq("ev0").all()
    assert len(assignments) == 1
    assert diagnostics["n_ev_specs_valid_params"].iloc[0] == 1


def test_multi_day_assignment_is_per_day_independent_under_daily_reset() -> None:
    instances = pd.concat(
        [_instances([10.0, 50.0], service_date="2026-04-17"), _instances([10.0, 50.0], service_date="2026-04-18")],
        ignore_index=True,
    )
    assignments, diagnostics, _ = build_feasible_vehicle_day_assignments(instances, _specs([100.0]), seed=1)
    assert len(diagnostics) == 2
    assert assignments.groupby("service_date")["vehicle_spec_id"].nunique().eq(1).all()


def test_legacy_function_unchanged_signature_and_output() -> None:
    legacy = build_vehicle_day_assignments(
        _instances([10.0, 50.0, 100.0]),
        pd.DataFrame({"vehicle_spec_id": ["ev0", "ev1"], "source_lsoa": ["X0", "X1"]}),
        seed=1,
    )
    assert len(legacy) == 2
    assert legacy["assignment_method"].str.contains("representative_duty").all()
    assert "required_kwh_est" not in legacy.columns

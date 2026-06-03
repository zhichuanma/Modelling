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


# ---------------------------------------------------------------------------
# PR 1.5: home-depot constrained matching
# ---------------------------------------------------------------------------

from mobility.bus.annual_vehicle_day_assignment import (  # noqa: E402
    FEASIBLE_HOME_DEPOT_ASSIGNMENT_METHOD,
    _haversine_km_vec,
)

# At lat 51.5, 1 degree of longitude is ~69.3 km.
_DEPOTS = {
    "depA": (51.5, 0.0),
    "depB": (51.5, 1.0),  # ~69 km from depA
    "depC": (51.5, 0.1),  # ~6.9 km from depA
}


def _registry(*depot_ids: str) -> pd.DataFrame:
    ids = list(depot_ids)
    return pd.DataFrame(
        {
            "depot_id": ids,
            "depot_lat": [_DEPOTS[d][0] for d in ids],
            "depot_lon": [_DEPOTS[d][1] for d in ids],
        }
    )


def _instances_at(passenger_km: list[float], depot_ids: list[str], *, service_date: str = "2026-04-17") -> pd.DataFrame:
    frame = _instances(passenger_km, service_date=service_date)
    frame["depot_id"] = depot_ids
    # Blocks start and end at their attached depot, so the matched same-depot
    # deadhead is 0 and cross-depot deadhead is the pure depot-depot distance x2.
    frame["start_lat"] = [_DEPOTS[d][0] for d in depot_ids]
    frame["start_lon"] = [_DEPOTS[d][1] for d in depot_ids]
    frame["end_lat"] = [_DEPOTS[d][0] for d in depot_ids]
    frame["end_lon"] = [_DEPOTS[d][1] for d in depot_ids]
    return frame


def _specs_with_home(range_km: list[float], home_depot_ids: list[str]) -> pd.DataFrame:
    specs = _specs(range_km)
    specs["home_depot_id"] = [str(d) for d in home_depot_ids]
    specs["home_depot_lat"] = [_DEPOTS[d][0] if d else np.nan for d in home_depot_ids]
    specs["home_depot_lon"] = [_DEPOTS[d][1] if d else np.nan for d in home_depot_ids]
    specs["home_depot_status"] = ["assigned" if d else "missing_source_lsoa" for d in home_depot_ids]
    return specs


def _depot_km(a: str, b: str) -> float:
    return float(
        _haversine_km_vec(
            np.array([_DEPOTS[a][0]]), np.array([_DEPOTS[a][1]]), np.array([_DEPOTS[b][0]]), np.array([_DEPOTS[b][1]])
        )[0]
    )


def test_strict_radius_zero_constrains_to_home_depot() -> None:
    instances = _instances_at([10.0, 10.0, 10.0], ["depA", "depA", "depB"])
    specs = _specs_with_home([1000.0, 1000.0], ["depA", "depB"])
    registry = _registry("depA", "depB")
    for seed in range(5):
        assignments, diagnostics, unmatched = build_feasible_vehicle_day_assignments(
            instances, specs, registry, seed=seed, sample_block_multiplier=2.0, home_depot_radius_km=0.0
        )
        assert len(assignments) == 2, f"seed={seed}"
        by_vehicle = assignments.set_index("vehicle_spec_id")
        assert by_vehicle.loc["ev0", "depot_id"] == "depA"
        assert by_vehicle.loc["ev1", "depot_id"] == "depB"
        assert by_vehicle.loc["ev0", "home_depot_id"] == "depA"
        assert assignments["assignment_method"].eq(FEASIBLE_HOME_DEPOT_ASSIGNMENT_METHOD).all()
        # The second depA block loses the competition for the single depA vehicle.
        assert len(unmatched) == 1
        assert unmatched["unmatched_reason"].iloc[0] == "lost_matching_competition"
        assert unmatched["n_vehicles_in_radius"].iloc[0] == 1


def test_radius_admits_nearby_depot_with_pair_deadhead() -> None:
    instances = _instances_at([10.0], ["depC"])
    specs = _specs_with_home([1000.0], ["depA"])
    registry = _registry("depA", "depC")

    _, _, unmatched_strict = build_feasible_vehicle_day_assignments(
        instances, specs, registry, seed=1, home_depot_radius_km=0.0
    )
    assert unmatched_strict["unmatched_reason"].iloc[0] == "no_vehicle_in_radius"
    assert unmatched_strict["n_vehicles_in_radius"].iloc[0] == 0

    assignments, _, unmatched = build_feasible_vehicle_day_assignments(
        instances, specs, registry, seed=1, home_depot_radius_km=10.0
    )
    assert unmatched.empty
    assert len(assignments) == 1
    # Pair-specific deadhead: home depot -> block start + block end -> home depot.
    assert np.isclose(assignments["deadhead_km_est"].iloc[0], 2.0 * _depot_km("depA", "depC"))
    assert not bool(assignments["deadhead_estimate_incomplete"].iloc[0])


def test_no_feasible_vehicle_in_radius_reason() -> None:
    instances = _instances_at([500.0], ["depA"])
    specs = _specs_with_home([60.0], ["depA"])
    registry = _registry("depA")
    assignments, diagnostics, unmatched = build_feasible_vehicle_day_assignments(
        instances, specs, registry, seed=1, home_depot_radius_km=0.0
    )
    assert assignments.empty
    assert unmatched["unmatched_reason"].iloc[0] == "no_feasible_vehicle_in_radius"
    assert unmatched["n_vehicles_in_radius"].iloc[0] == 1
    assert unmatched["n_feasible_vehicles"].iloc[0] == 0
    row = diagnostics.iloc[0]
    assert row["n_unmatched_no_feasible_vehicle_in_radius"] == 1
    assert row["n_unmatched_no_vehicle_in_radius"] == 0
    assert row["n_unmatched_no_feasible_vehicle"] == 1  # zero feasible edges overall


def test_vehicles_without_home_depot_are_excluded() -> None:
    instances = _instances_at([10.0], ["depA"])
    registry = _registry("depA")
    specs = _specs_with_home([1000.0, 1000.0], ["", "depA"])
    assignments, diagnostics, _ = build_feasible_vehicle_day_assignments(
        instances, specs, registry, seed=1, home_depot_radius_km=1000.0
    )
    assert assignments["vehicle_spec_id"].eq("ev1").all()
    assert diagnostics["n_ev_specs_with_home_depot"].iloc[0] == 1

    only_unassigned = _specs_with_home([1000.0], [""])
    assignments_none, _, unmatched_none = build_feasible_vehicle_day_assignments(
        instances, only_unassigned, registry, seed=1, home_depot_radius_km=1000.0
    )
    assert assignments_none.empty
    assert unmatched_none["unmatched_reason"].iloc[0] == "no_vehicle_in_radius"


def test_unconstrained_path_is_unchanged_by_home_columns() -> None:
    # A/B guard: radius=None must reproduce PR 1 byte-for-byte even when the
    # specs carry home-depot columns.
    instances = _instances([10.0, 50.0, 90.0])
    plain = build_feasible_vehicle_day_assignments(instances, _specs([100.0, 60.0]), seed=42)
    with_home = build_feasible_vehicle_day_assignments(
        instances, _specs_with_home([100.0, 60.0], ["depA", "depB"]), seed=42
    )
    for a, b in zip(plain, with_home):
        pd.testing.assert_frame_equal(a, b)
    assert plain[0]["assignment_method"].eq(FEASIBLE_ASSIGNMENT_METHOD).all()
    assert plain[0]["home_depot_id"].eq("").all()


def test_constrained_deterministic_for_fixed_seed() -> None:
    instances = _instances_at([10.0, 20.0, 30.0, 40.0], ["depA", "depA", "depB", "depC"])
    specs = _specs_with_home([100.0, 60.0, 80.0], ["depA", "depB", "depC"])
    registry = _registry("depA", "depB", "depC")
    first = build_feasible_vehicle_day_assignments(instances, specs, registry, seed=42, sample_block_multiplier=2.0, home_depot_radius_km=10.0)
    second = build_feasible_vehicle_day_assignments(instances, specs, registry, seed=42, sample_block_multiplier=2.0, home_depot_radius_km=10.0)
    for a, b in zip(first, second):
        pd.testing.assert_frame_equal(a, b)


def test_constrained_matching_is_maximum_cardinality_vs_networkx() -> None:
    import networkx as nx

    rng = np.random.default_rng(7)
    depot_ids = list(_DEPOTS)
    for trial in range(5):
        n_blocks = 12
        n_specs = 15  # >= n_blocks so every block is sampled
        block_depots = [depot_ids[i] for i in rng.integers(0, len(depot_ids), size=n_blocks)]
        passenger = [float(km) for km in rng.uniform(5.0, 120.0, size=n_blocks)]
        spec_homes = [depot_ids[i] for i in rng.integers(0, len(depot_ids), size=n_specs)]
        ranges = [float(km) for km in rng.uniform(20.0, 150.0, size=n_specs)]
        radius = float(rng.choice([0.0, 10.0, 100.0]))
        instances = _instances_at(passenger, block_depots)
        specs = _specs_with_home(ranges, spec_homes)
        registry = _registry(*depot_ids)

        assignments, _, _ = build_feasible_vehicle_day_assignments(
            instances, specs, registry, seed=trial, sample_block_multiplier=1.0, home_depot_radius_km=radius
        )

        # Independent reference: build the admissible-and-feasible bipartite
        # graph from first principles and compare matching cardinality.
        graph = nx.Graph()
        block_nodes = [f"b{i}" for i in range(n_blocks)]
        spec_nodes = [f"v{j}" for j in range(n_specs)]
        graph.add_nodes_from(block_nodes, bipartite=0)
        graph.add_nodes_from(spec_nodes, bipartite=1)
        for i in range(n_blocks):
            for j in range(n_specs):
                depot_distance = _depot_km(spec_homes[j], block_depots[i])
                if not (spec_homes[j] == block_depots[i] or depot_distance <= radius):
                    continue
                deadhead = 2.0 * _depot_km(spec_homes[j], block_depots[i])
                if ranges[j] >= passenger[i] + deadhead:
                    graph.add_edge(block_nodes[i], spec_nodes[j])
        reference = nx.algorithms.bipartite.matching.hopcroft_karp_matching(graph, top_nodes=block_nodes)
        reference_size = sum(1 for node in reference if node.startswith("b"))
        assert len(assignments) == reference_size, f"trial={trial} radius={radius}"


# ---------------------------------------------------------------------------
# PR 1.5: supply-weighted block sampling (plan v2 §15 decision (b))
# ---------------------------------------------------------------------------

import pytest  # noqa: E402


def test_supply_weighted_requires_constrained_mode() -> None:
    with pytest.raises(ValueError, match="supply_weighted"):
        build_feasible_vehicle_day_assignments(
            _instances([10.0]), _specs([60.0]), block_sampling="supply_weighted"
        )
    with pytest.raises(ValueError, match="block_sampling"):
        build_feasible_vehicle_day_assignments(
            _instances([10.0]), _specs([60.0]), block_sampling="bogus"
        )


def test_supply_weighted_never_samples_blocks_without_reachable_fleet() -> None:
    # 4 blocks at depB (no fleet), 2 at depA (one vehicle). Under uniform sampling
    # a depB block would often be drawn; under supply weighting depB weight is 0.
    instances = _instances_at([10.0] * 6, ["depB", "depB", "depB", "depB", "depA", "depA"])
    specs = _specs_with_home([1000.0], ["depA"])
    registry = _registry("depA", "depB")
    for seed in range(10):
        assignments, diagnostics, unmatched = build_feasible_vehicle_day_assignments(
            instances, specs, registry, seed=seed, sample_block_multiplier=1.0,
            home_depot_radius_km=0.0, block_sampling="supply_weighted",
        )
        assert len(assignments) == 1, f"seed={seed}"
        assert assignments["depot_id"].iloc[0] == "depA"
        assert unmatched.empty
        row = diagnostics.iloc[0]
        assert row["block_sampling"] == "supply_weighted"
        assert row["n_blocks_positive_sampling_weight"] == 2
        assert row["n_unmatched_no_vehicle_in_radius"] == 0
        assert assignments["block_sampling_weight"].iloc[0] == 1.0


def test_supply_weighted_sample_size_capped_by_positive_weights() -> None:
    # Fleet of 3 wants 3 blocks, but only 2 blocks have reachable fleet.
    instances = _instances_at([10.0, 10.0, 10.0], ["depA", "depA", "depB"])
    specs = _specs_with_home([1000.0, 1000.0, 1000.0], ["depA", "depA", "depA"])
    registry = _registry("depA", "depB")
    _, diagnostics, _ = build_feasible_vehicle_day_assignments(
        instances, specs, registry, seed=1, home_depot_radius_km=0.0, block_sampling="supply_weighted"
    )
    assert diagnostics["n_sampled_block_instances_for_service_date"].iloc[0] == 2


def test_supply_weighted_weights_reflect_in_radius_fleet() -> None:
    # depC is ~6.9 km from depA: radius 10 makes depA's 2 vehicles admit depC
    # blocks too, and depC's own vehicle admits depA blocks.
    instances = _instances_at([10.0, 10.0], ["depA", "depC"])
    specs = _specs_with_home([1000.0, 1000.0, 1000.0], ["depA", "depA", "depC"])
    registry = _registry("depA", "depC")
    assignments, _, _ = build_feasible_vehicle_day_assignments(
        instances, specs, registry, seed=3, sample_block_multiplier=1.0,
        home_depot_radius_km=10.0, block_sampling="supply_weighted",
    )
    assert assignments["block_sampling_weight"].eq(3.0).all()


def test_uniform_sampling_unchanged_in_constrained_mode() -> None:
    instances = _instances_at([10.0, 10.0], ["depA", "depB"])
    specs = _specs_with_home([1000.0], ["depA"])
    registry = _registry("depA", "depB")
    _, diagnostics, _ = build_feasible_vehicle_day_assignments(
        instances, specs, registry, seed=1, home_depot_radius_km=0.0, block_sampling="uniform"
    )
    row = diagnostics.iloc[0]
    assert row["block_sampling"] == "uniform"
    assert np.isnan(row["n_blocks_positive_sampling_weight"])

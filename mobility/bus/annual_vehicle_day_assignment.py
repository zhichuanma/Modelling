"""Vehicle-day assignment for ev_stock_scale annual bus depot-load runs."""

from __future__ import annotations

import hashlib
from typing import Any

import numpy as np
import pandas as pd


RNG_SEED = 20260603


def stable_daily_seed(seed: int, service_date: str) -> int:
    digest = hashlib.sha1(f"{int(seed)}:{service_date}".encode("utf-8")).hexdigest()[:16]
    return int(digest, 16) % (2**32)


def _distance_bin(distance_km: float) -> str:
    value = float(distance_km)
    if value < 50:
        return "lt_50km"
    if value < 100:
        return "50_100km"
    if value < 200:
        return "100_200km"
    return "ge_200km"


def _duration_bin(duration_h: float) -> str:
    value = float(duration_h)
    if value < 4:
        return "lt_4h"
    if value < 8:
        return "4_8h"
    if value < 12:
        return "8_12h"
    return "ge_12h"


def build_vehicle_day_assignments(
    block_instances: pd.DataFrame,
    ev_bus_specs: pd.DataFrame,
    *,
    seed: int = RNG_SEED,
    scenario_mode: str = "ev_stock_scale",
    max_vehicle_days: int | None = None,
) -> pd.DataFrame:
    """Assign one EV spec to at most one sampled active block per service date."""
    if scenario_mode != "ev_stock_scale":
        raise NotImplementedError("Only scenario_mode='ev_stock_scale' is implemented.")
    if block_instances.empty or ev_bus_specs.empty:
        return pd.DataFrame(columns=_assignment_columns())
    required_instances = {"service_date", "block_instance_id", "block_template_id", "agency_id", "service_id", "block_id", "depot_id", "region_key"}
    required_specs = {"vehicle_spec_id"}
    missing = sorted(required_instances - set(block_instances.columns))
    if missing:
        raise ValueError(f"block_instances is missing required columns: {missing}")
    missing_specs = sorted(required_specs - set(ev_bus_specs.columns))
    if missing_specs:
        raise ValueError(f"ev_bus_specs is missing required columns: {missing_specs}")

    specs = ev_bus_specs.sort_values("vehicle_spec_id", kind="stable").reset_index(drop=True)
    records: list[dict[str, Any]] = []
    for service_date, day_blocks in block_instances.groupby("service_date", sort=True):
        day_blocks = day_blocks.copy().sort_values(["region_key", "passenger_distance_km", "duration_h", "block_instance_id"], kind="stable")
        rng = np.random.default_rng(stable_daily_seed(seed, str(service_date)))
        n_assign = min(len(specs), len(day_blocks))
        if n_assign <= 0:
            continue
        n_available = int(len(day_blocks))
        n_unassigned = int(n_available - n_assign)
        coverage_share = float(n_assign / n_available) if n_available else np.nan
        block_positions = rng.choice(np.arange(len(day_blocks)), size=n_assign, replace=False)
        spec_positions = rng.permutation(np.arange(len(specs)))[:n_assign]
        sampled_blocks = day_blocks.iloc[block_positions].reset_index(drop=True)
        sampled_specs = specs.iloc[spec_positions].reset_index(drop=True)
        for idx, (block_row, spec_row) in enumerate(zip(sampled_blocks.itertuples(index=False), sampled_specs.itertuples(index=False))):
            distance = float(getattr(block_row, "passenger_distance_km", 0.0))
            duration = float(getattr(block_row, "duration_h", 0.0))
            vehicle_day_id = f"vd_{service_date}_{idx:05d}_{getattr(spec_row, 'vehicle_spec_id')}"
            records.append(
                {
                    "service_date": str(service_date),
                    "vehicle_day_id": vehicle_day_id,
                    "vehicle_spec_id": str(getattr(spec_row, "vehicle_spec_id")),
                    "block_instance_id": str(getattr(block_row, "block_instance_id")),
                    "block_template_id": str(getattr(block_row, "block_template_id")),
                    "agency_id": str(getattr(block_row, "agency_id")),
                    "service_id": str(getattr(block_row, "service_id")),
                    "block_id": str(getattr(block_row, "block_id")),
                    "depot_id": str(getattr(block_row, "depot_id", "")),
                    "region_key": str(getattr(block_row, "region_key", "unknown")),
                    "distance_bin": _distance_bin(distance),
                    "duration_bin": _duration_bin(duration),
                    "assignment_method": "ev_stock_scale_random_representative_duty",
                    "scenario_mode": scenario_mode,
                    "sample_weight": 1.0,
                    "assignment_seed": stable_daily_seed(seed, str(service_date)),
                    "n_available_block_instances_for_service_date": n_available,
                    "n_assigned_block_instances_for_service_date": int(n_assign),
                    "n_unassigned_block_instances_for_service_date": n_unassigned,
                    "daily_assignment_coverage_share": coverage_share,
                }
            )
        if max_vehicle_days is not None and len(records) >= int(max_vehicle_days):
            break

    assignments = pd.DataFrame.from_records(records, columns=_assignment_columns())
    if max_vehicle_days is not None and len(assignments) > int(max_vehicle_days):
        assignments = assignments.iloc[: int(max_vehicle_days)].copy()
    return assignments.reset_index(drop=True)


def _assignment_columns() -> list[str]:
    return [
        "service_date",
        "vehicle_day_id",
        "vehicle_spec_id",
        "block_instance_id",
        "block_template_id",
        "agency_id",
        "service_id",
        "block_id",
        "depot_id",
        "region_key",
        "distance_bin",
        "duration_bin",
        "assignment_method",
        "scenario_mode",
        "sample_weight",
        "assignment_seed",
        "n_available_block_instances_for_service_date",
        "n_assigned_block_instances_for_service_date",
        "n_unassigned_block_instances_for_service_date",
        "daily_assignment_coverage_share",
    ]


FEASIBLE_ASSIGNMENT_METHOD = "sample_then_feasible_match"
_EARTH_RADIUS_KM = 6371.0088  # must match annual_depot_events.haversine_km


def _haversine_km_vec(lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray, lon2: np.ndarray) -> np.ndarray:
    """Vectorized haversine matching annual_depot_events.haversine_km (x 1.0, NaN-propagating)."""
    phi1 = np.radians(lat1)
    phi2 = np.radians(lat2)
    d_phi = np.radians(lat2 - lat1)
    d_lam = np.radians(lon2 - lon1)
    a = np.sin(d_phi / 2.0) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(d_lam / 2.0) ** 2
    return 2.0 * _EARTH_RADIUS_KM * np.arcsin(np.minimum(1.0, np.sqrt(a)))


def _spec_range_km(specs: pd.DataFrame) -> np.ndarray:
    """Maximum distance each spec can drive within its usable SOC window (daily-reset mode).

    Invalid parameters yield NaN, which never satisfies the feasibility comparison.
    """
    battery = pd.to_numeric(specs["battery_kwh"], errors="coerce").to_numpy(dtype=float)
    soc_min = pd.to_numeric(specs["usable_soc_min"], errors="coerce").to_numpy(dtype=float)
    soc_max = pd.to_numeric(specs["usable_soc_max"], errors="coerce").to_numpy(dtype=float)
    consumption = pd.to_numeric(specs["consumption_kwh_per_km"], errors="coerce").to_numpy(dtype=float)
    available_kwh = battery * (soc_max - soc_min)
    with np.errstate(divide="ignore", invalid="ignore"):
        range_km = np.where(
            np.isfinite(available_kwh) & np.isfinite(consumption) & (consumption > 0.0) & (available_kwh > 0.0),
            available_kwh / consumption,
            np.nan,
        )
    return range_km


def _block_deadhead_km(day_blocks: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Per-block deadhead estimate: attached depot <-> block start/end, haversine x 1.0.

    Missing coordinates contribute 0 km and set the incomplete flag, matching the
    event-stage behavior so the feasibility screen and SOC walk stay consistent.
    """
    n = len(day_blocks)
    coord_cols = ("depot_lat", "depot_lon", "start_lat", "start_lon", "end_lat", "end_lon")
    if n == 0 or not all(col in day_blocks.columns for col in coord_cols):
        return np.zeros(n, dtype=float), np.ones(n, dtype=bool)
    values = {col: pd.to_numeric(day_blocks[col], errors="coerce").to_numpy(dtype=float) for col in coord_cols}
    to_block = _haversine_km_vec(values["depot_lat"], values["depot_lon"], values["start_lat"], values["start_lon"])
    from_block = _haversine_km_vec(values["end_lat"], values["end_lon"], values["depot_lat"], values["depot_lon"])
    incomplete = ~np.isfinite(to_block) | ~np.isfinite(from_block)
    deadhead = np.nan_to_num(to_block, nan=0.0) + np.nan_to_num(from_block, nan=0.0)
    return deadhead, incomplete


def build_feasible_vehicle_day_assignments(
    block_instances: pd.DataFrame,
    ev_bus_specs: pd.DataFrame,
    depot_registry: pd.DataFrame | None = None,
    *,
    seed: int = RNG_SEED,
    scenario_mode: str = "ev_stock_scale",
    sample_block_multiplier: float = 1.0,
    max_vehicle_days: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Sample blocks per service date and match EVs to blocks feasibility-aware.

    Feasibility under daily-reset SOC is a pure threshold rule:
    ``(passenger_km + deadhead_km) <= battery_kwh * (usable_soc_max - usable_soc_min)
    / consumption_kwh_per_km``, i.e. each block is feasible exactly for the specs
    whose range covers its total distance. The feasible spec-sets are therefore
    nested by inclusion, so processing blocks in descending total distance and
    assigning a uniformly random spec from the currently feasible, still-unused
    pool yields an exact maximum-cardinality matching (exchange argument on the
    nested set system). If PR 2 adds non-nested constraints (home depot, vehicle
    type), this must be replaced by general bipartite matching per constraint group.

    Returns ``(assignments, daily_diagnostics, unmatched_sampled_blocks)``.
    """
    if scenario_mode != "ev_stock_scale":
        raise NotImplementedError("Only scenario_mode='ev_stock_scale' is implemented.")
    if float(sample_block_multiplier) <= 0.0:
        raise ValueError("sample_block_multiplier must be positive.")
    if block_instances.empty or ev_bus_specs.empty:
        return (
            pd.DataFrame(columns=_feasible_assignment_columns()),
            pd.DataFrame(columns=_feasible_diagnostic_columns()),
            pd.DataFrame(columns=_unmatched_block_columns()),
        )
    required_instances = {"service_date", "block_instance_id", "block_template_id", "agency_id", "service_id", "block_id", "depot_id", "region_key", "passenger_distance_km"}
    missing = sorted(required_instances - set(block_instances.columns))
    if missing:
        raise ValueError(f"block_instances is missing required columns: {missing}")
    required_specs = {"vehicle_spec_id", "battery_kwh", "consumption_kwh_per_km", "usable_soc_min", "usable_soc_max"}
    missing_specs = sorted(required_specs - set(ev_bus_specs.columns))
    if missing_specs:
        raise ValueError(f"ev_bus_specs is missing required columns: {missing_specs}")

    instances = block_instances
    if depot_registry is not None and not depot_registry.empty and {"depot_id", "depot_lat", "depot_lon"}.issubset(depot_registry.columns):
        registry_cols = depot_registry.loc[:, ["depot_id", "depot_lat", "depot_lon"]].drop_duplicates("depot_id")
        instances = block_instances.drop(columns=["depot_lat", "depot_lon"], errors="ignore").merge(registry_cols, on="depot_id", how="left")

    specs = ev_bus_specs.sort_values("vehicle_spec_id", kind="stable").reset_index(drop=True)
    range_km = _spec_range_km(specs)
    spec_order_desc = np.argsort(-np.nan_to_num(range_km, nan=-np.inf), kind="stable")
    spec_ids = specs["vehicle_spec_id"].astype(str).to_numpy()
    spec_consumption = pd.to_numeric(specs["consumption_kwh_per_km"], errors="coerce").to_numpy(dtype=float)
    spec_available_kwh = (
        pd.to_numeric(specs["battery_kwh"], errors="coerce").to_numpy(dtype=float)
        * (
            pd.to_numeric(specs["usable_soc_max"], errors="coerce").to_numpy(dtype=float)
            - pd.to_numeric(specs["usable_soc_min"], errors="coerce").to_numpy(dtype=float)
        )
    )
    n_valid_specs = int(np.isfinite(range_km).sum())

    assignment_records: list[dict[str, Any]] = []
    diagnostic_records: list[dict[str, Any]] = []
    unmatched_records: list[dict[str, Any]] = []
    for service_date, day_blocks in instances.groupby("service_date", sort=True):
        day_blocks = day_blocks.copy().sort_values(["region_key", "passenger_distance_km", "duration_h", "block_instance_id"], kind="stable").reset_index(drop=True)
        daily_seed = stable_daily_seed(seed, str(service_date))
        rng = np.random.default_rng(daily_seed)
        n_available = int(len(day_blocks))
        n_sample = min(n_available, int(np.ceil(len(specs) * float(sample_block_multiplier))))
        if n_sample <= 0:
            continue
        sampled_positions = rng.choice(np.arange(n_available), size=n_sample, replace=False)
        sampled = day_blocks.iloc[sampled_positions].reset_index(drop=True)

        passenger_km = pd.to_numeric(sampled["passenger_distance_km"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
        deadhead_km, deadhead_incomplete = _block_deadhead_km(sampled)
        total_km = passenger_km + deadhead_km

        # Exact maximum matching on the nested threshold structure: descending
        # block distance, random pick from the feasible unused-spec pool.
        block_order = np.argsort(-total_km, kind="stable")
        pool: list[int] = []
        admitted = 0
        matched_spec_pos = np.full(n_sample, -1, dtype=int)
        n_feasible_vehicles = np.zeros(n_sample, dtype=int)
        for block_pos in block_order:
            km = float(total_km[block_pos])
            while admitted < len(spec_order_desc):
                candidate = int(spec_order_desc[admitted])
                if np.isfinite(range_km[candidate]) and range_km[candidate] >= km:
                    pool.append(candidate)
                    admitted += 1
                else:
                    break
            n_feasible_vehicles[block_pos] = admitted
            if pool:
                pick = int(rng.integers(len(pool)))
                pool[pick], pool[-1] = pool[-1], pool[pick]
                matched_spec_pos[block_pos] = pool.pop()

        n_matched = 0
        for emit_index, block_pos in enumerate(block_order):
            block_row = sampled.iloc[int(block_pos)]
            spec_pos = int(matched_spec_pos[block_pos])
            if spec_pos < 0:
                unmatched_records.append(
                    {
                        "service_date": str(service_date),
                        "block_instance_id": str(block_row["block_instance_id"]),
                        "block_id": str(block_row["block_id"]),
                        "unmatched_reason": "no_feasible_vehicle" if n_feasible_vehicles[block_pos] == 0 else "lost_matching_competition",
                        "n_feasible_vehicles": int(n_feasible_vehicles[block_pos]),
                        "passenger_distance_km": float(passenger_km[block_pos]),
                        "deadhead_km_est": float(deadhead_km[block_pos]),
                        "total_distance_km_est": float(total_km[block_pos]),
                        "assignment_method": FEASIBLE_ASSIGNMENT_METHOD,
                        "daily_soc_mode": "daily_reset",
                    }
                )
                continue
            distance = float(passenger_km[block_pos])
            duration = float(np.nan_to_num(pd.to_numeric(block_row.get("duration_h"), errors="coerce"), nan=0.0))
            assignment_records.append(
                {
                    "service_date": str(service_date),
                    "vehicle_day_id": f"vd_{service_date}_{emit_index:05d}_{spec_ids[spec_pos]}",
                    "vehicle_spec_id": spec_ids[spec_pos],
                    "block_instance_id": str(block_row["block_instance_id"]),
                    "block_template_id": str(block_row["block_template_id"]),
                    "agency_id": str(block_row["agency_id"]),
                    "service_id": str(block_row["service_id"]),
                    "block_id": str(block_row["block_id"]),
                    "depot_id": str(block_row.get("depot_id", "")),
                    "region_key": str(block_row.get("region_key", "unknown")),
                    "distance_bin": _distance_bin(distance),
                    "duration_bin": _duration_bin(duration),
                    "assignment_status": "matched_feasible",
                    "assignment_method": FEASIBLE_ASSIGNMENT_METHOD,
                    "scenario_mode": scenario_mode,
                    "sample_weight": 1.0,
                    "assignment_seed": daily_seed,
                    "required_kwh_est": float(total_km[block_pos] * spec_consumption[spec_pos]),
                    "available_kwh_at_assignment": float(spec_available_kwh[spec_pos]),
                    "deadhead_km_est": float(deadhead_km[block_pos]),
                    "deadhead_estimate_incomplete": bool(deadhead_incomplete[block_pos]),
                    "daily_soc_mode": "daily_reset",
                }
            )
            n_matched += 1

        n_unmatched = n_sample - n_matched
        n_blocks_any_feasible = int((n_feasible_vehicles > 0).sum())
        n_no_feasible = int((n_feasible_vehicles == 0).sum())
        diagnostic_records.append(
            {
                "service_date": str(service_date),
                "n_ev_specs": int(len(specs)),
                "n_ev_specs_valid_params": n_valid_specs,
                "n_active_block_instances_for_service_date": n_available,
                "n_sampled_block_instances_for_service_date": int(n_sample),
                "n_feasible_edges": int(n_feasible_vehicles.sum()),
                "n_blocks_with_any_feasible_vehicle": n_blocks_any_feasible,
                "n_matched_feasible_block_instances_for_service_date": int(n_matched),
                "n_unmatched_sampled_block_instances_for_service_date": int(n_unmatched),
                "n_unmatched_no_feasible_vehicle": n_no_feasible,
                "n_unmatched_lost_matching_competition": int(n_unmatched - n_no_feasible),
                "sampled_block_coverage_share": float(n_sample / n_available) if n_available else np.nan,
                "matched_sample_share": float(n_matched / n_sample) if n_sample else np.nan,
                "matched_active_block_share": float(n_matched / n_available) if n_available else np.nan,
                "assignment_method": FEASIBLE_ASSIGNMENT_METHOD,
                "daily_soc_mode": "daily_reset",
                # Legacy-named columns kept for run-summary compatibility.
                "n_available_block_instances_for_service_date": n_available,
                "n_assigned_block_instances_for_service_date": int(n_matched),
                "n_unassigned_block_instances_for_service_date": int(n_available - n_matched),
                "daily_assignment_coverage_share": float(n_matched / n_available) if n_available else np.nan,
            }
        )
        if max_vehicle_days is not None and len(assignment_records) >= int(max_vehicle_days):
            break

    assignments = pd.DataFrame.from_records(assignment_records, columns=_feasible_assignment_columns())
    if max_vehicle_days is not None and len(assignments) > int(max_vehicle_days):
        assignments = assignments.iloc[: int(max_vehicle_days)].copy()
    diagnostics = pd.DataFrame.from_records(diagnostic_records, columns=_feasible_diagnostic_columns())
    unmatched = pd.DataFrame.from_records(unmatched_records, columns=_unmatched_block_columns())
    return assignments.reset_index(drop=True), diagnostics.reset_index(drop=True), unmatched.reset_index(drop=True)


def _feasible_assignment_columns() -> list[str]:
    return [
        "service_date",
        "vehicle_day_id",
        "vehicle_spec_id",
        "block_instance_id",
        "block_template_id",
        "agency_id",
        "service_id",
        "block_id",
        "depot_id",
        "region_key",
        "distance_bin",
        "duration_bin",
        "assignment_status",
        "assignment_method",
        "scenario_mode",
        "sample_weight",
        "assignment_seed",
        "required_kwh_est",
        "available_kwh_at_assignment",
        "deadhead_km_est",
        "deadhead_estimate_incomplete",
        "daily_soc_mode",
    ]


def _feasible_diagnostic_columns() -> list[str]:
    return [
        "service_date",
        "n_ev_specs",
        "n_ev_specs_valid_params",
        "n_active_block_instances_for_service_date",
        "n_sampled_block_instances_for_service_date",
        "n_feasible_edges",
        "n_blocks_with_any_feasible_vehicle",
        "n_matched_feasible_block_instances_for_service_date",
        "n_unmatched_sampled_block_instances_for_service_date",
        "n_unmatched_no_feasible_vehicle",
        "n_unmatched_lost_matching_competition",
        "sampled_block_coverage_share",
        "matched_sample_share",
        "matched_active_block_share",
        "assignment_method",
        "daily_soc_mode",
        "n_available_block_instances_for_service_date",
        "n_assigned_block_instances_for_service_date",
        "n_unassigned_block_instances_for_service_date",
        "daily_assignment_coverage_share",
    ]


def _unmatched_block_columns() -> list[str]:
    return [
        "service_date",
        "block_instance_id",
        "block_id",
        "unmatched_reason",
        "n_feasible_vehicles",
        "passenger_distance_km",
        "deadhead_km_est",
        "total_distance_km_est",
        "assignment_method",
        "daily_soc_mode",
    ]


__all__ = [
    "FEASIBLE_ASSIGNMENT_METHOD",
    "RNG_SEED",
    "build_feasible_vehicle_day_assignments",
    "build_vehicle_day_assignments",
    "stable_daily_seed",
]

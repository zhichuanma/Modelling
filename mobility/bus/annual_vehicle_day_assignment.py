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


__all__ = ["RNG_SEED", "build_vehicle_day_assignments", "stable_daily_seed"]

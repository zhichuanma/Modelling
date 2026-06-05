"""Stage 4 deterministic one-to-one vehicle/block pairing."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .depot_only_sampling import DEFAULT_SEED, FULL_EV_INVENTORY_MODE, PILOT_MODE


ASSIGNMENT_METHOD = "deterministic_random_pairing_no_vehicle_geography"


def build_simulation_cases(
    ev_bus_instances: pd.DataFrame,
    sampled_blocks: pd.DataFrame,
    *,
    sample_mode: str = FULL_EV_INVENTORY_MODE,
    seed: int = DEFAULT_SEED,
    service_date: str = "2026-06-03",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Pair shuffled vehicles and sampled blocks one-to-one.

    Vehicle ``source_lsoa`` is carried through for audit only and is never used
    for matching.
    """
    vehicles = ev_bus_instances.copy()
    blocks = sampled_blocks.copy()
    usable_blocks = blocks.loc[blocks.get("operational_depot_lsoa", pd.Series("", index=blocks.index)).fillna("").astype(str).str.strip().ne("")].copy()
    if sample_mode == FULL_EV_INVENTORY_MODE and len(usable_blocks) != len(vehicles):
        raise ValueError(
            "full_ev_inventory mode requires one non-missing-depot sampled block per valid EV bus instance; "
            f"got {len(usable_blocks)} blocks and {len(vehicles)} vehicles."
        )
    if sample_mode == PILOT_MODE:
        n = min(len(vehicles), len(usable_blocks))
        vehicles = vehicles.iloc[:n].copy()
        usable_blocks = usable_blocks.iloc[:n].copy()
    if len(vehicles) != len(usable_blocks):
        raise ValueError(f"vehicle/block count mismatch: {len(vehicles)} vehicles, {len(usable_blocks)} blocks")

    rng = np.random.default_rng(int(seed))
    vehicle_order = rng.permutation(len(vehicles))
    block_order = rng.permutation(len(usable_blocks))
    vehicles_shuffled = vehicles.iloc[vehicle_order].reset_index(drop=True)
    blocks_shuffled = usable_blocks.iloc[block_order].reset_index(drop=True)

    records: list[dict[str, Any]] = []
    for idx, (vehicle, block) in enumerate(zip(vehicles_shuffled.to_dict(orient="records"), blocks_shuffled.to_dict(orient="records"))):
        case = {
            "simulation_case_id": f"case_{idx:06d}",
            "vehicle_day_id": f"case_{idx:06d}",
            "service_date": str(service_date),
            "vehicle_id": str(vehicle["vehicle_id"]),
            "source_row_id": int(vehicle["source_row_id"]),
            "vehicle_model": str(vehicle["vehicle_model"]),
            "vehicle_subtype": str(vehicle["vehicle_subtype"]),
            "source_lsoa": str(vehicle.get("source_lsoa", "")),
            "battery_kwh": float(vehicle["battery_kwh"]),
            "consumption_kwh_per_km": float(vehicle["consumption_kwh_per_km"]),
            "ac_charge_kw_max": float(vehicle["ac_charge_kw_max"]),
            "dc_charge_kw_max": float(vehicle.get("dc_charge_kw_max", np.nan)),
            "usable_soc_min": float(vehicle["usable_soc_min"]),
            "usable_soc_max": float(vehicle["usable_soc_max"]),
            "vehicle_instance_weight": float(vehicle.get("vehicle_instance_weight", 1.0)),
            "assignment_method": ASSIGNMENT_METHOD if sample_mode == FULL_EV_INVENTORY_MODE else f"pilot_debug_{ASSIGNMENT_METHOD}",
            "sample_mode": sample_mode,
            "assignment_seed": int(seed),
            "vehicle_source_lsoa_used_for_matching": False,
        }
        case.update(_block_fields(block))
        records.append(case)
    cases = pd.DataFrame.from_records(records)
    diagnostics = pd.DataFrame(
        [
            {
                "sample_mode": sample_mode,
                "assignment_method": ASSIGNMENT_METHOD if sample_mode == FULL_EV_INVENTORY_MODE else f"pilot_debug_{ASSIGNMENT_METHOD}",
                "assignment_seed": int(seed),
                "n_ev_instances_available": int(len(ev_bus_instances)),
                "n_sampled_blocks_input": int(len(sampled_blocks)),
                "n_blocks_with_missing_depot": int(len(sampled_blocks) - len(usable_blocks)),
                "n_simulation_cases_created": int(len(cases)),
                "each_vehicle_used_once": bool(cases["vehicle_id"].is_unique) if not cases.empty else True,
                "each_sampled_block_used_once": bool(cases["block_template_id"].is_unique) if sample_mode == FULL_EV_INVENTORY_MODE and not cases.empty else True,
                "vehicle_source_lsoa_used_for_matching": False,
            }
        ]
    )
    return cases.reset_index(drop=True), diagnostics


def _block_fields(block: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "block_template_id",
        "block_id",
        "agency_id",
        "service_id",
        "block_source",
        "start_h",
        "end_h",
        "start_time",
        "end_time",
        "duration_h",
        "passenger_distance_km",
        "start_stop",
        "end_stop",
        "start_lat",
        "start_lon",
        "end_lat",
        "end_lon",
        "start_lsoa",
        "end_lsoa",
        "region_key",
        "distance_bin",
        "duration_bin",
        "sample_weight",
        "depot_id",
        "operational_depot_lsoa",
        "depot_confidence",
        "depot_inference_method",
        "depot_lat",
        "depot_lon",
        "depot_coordinate_source",
    }
    out = {field: block.get(field, np.nan if field.endswith(("_lat", "_lon", "_h", "_km")) else "") for field in fields}
    for column in (
        "trip_ids",
        "trip_start_times",
        "trip_end_times",
        "trip_start_lats",
        "trip_start_lons",
        "trip_end_lats",
        "trip_end_lons",
        "trip_distances_km",
        "trip_start_stops",
        "trip_end_stops",
        "trip_start_lsoas",
        "trip_end_lsoas",
        "candidate_terminal_lsoas",
    ):
        if column in block:
            out[column] = block[column]
    return out


__all__ = ["ASSIGNMENT_METHOD", "build_simulation_cases"]

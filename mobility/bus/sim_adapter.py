"""Bus adapters around the vehicle-agnostic SOC simulator."""

from __future__ import annotations

import numpy as np
import pandas as pd

from mobility.core.constants import DEFAULT_CHEMISTRY, STEP_HOURS_DECISION, STEPS_PER_DAY_DECISION
from mobility.core.simulator import simulate_single_ev

from .feasibility import scan_block_infeasibility
from .trip_chain_bus import block_to_daily_schedules
from .vehicle_sampling import sample_bus_vehicle_specs


DEFAULT_BATTERY_KWH = 300.0
DEFAULT_CONSUMPTION_KWH_PER_KM = 1.2
DEFAULT_DEPOT_CHARGE_KW = 100.0


def _parking_energy_kwh(schedules, purpose: str) -> float:
    return float(
        sum(
            event.energy_charged_kwh
            for schedule in schedules
            for event in schedule.parking_events
            if event.location_purpose == purpose
        )
    )


def _deadhead_audit_fields(schedules) -> dict:
    metadata = getattr(schedules[0], "metadata", {}) if schedules else {}
    return {
        "deadhead_short_count": int(metadata.get("deadhead_short_count", 0)),
        "deadhead_long_count": int(metadata.get("deadhead_long_count", 0)),
        "deadhead_total_km": float(metadata.get("deadhead_total_km", 0.0)),
        "deadhead_total_kwh": float(metadata.get("deadhead_total_kwh", 0.0)),
        "deadhead_skipped_time_count": int(metadata.get("deadhead_skipped_time_count", 0)),
        "deadhead_skipped_time_km": float(metadata.get("deadhead_skipped_time_km", 0.0)),
        "deadhead_skipped_missing_coord_count": int(metadata.get("deadhead_skipped_missing_coord_count", 0)),
    }


def simulate_block(
    block_df: pd.DataFrame,
    *,
    battery_kwh: float,
    consumption_kwh_per_km: float,
    depot_charge_kw: float,
    soc_init: float = 1.0,
    allow_layover_charging: bool = False,
    layover_charge_kw: float = 0.0,
    min_layover_for_charging_h: float = 0.0,
    chemistry: str = DEFAULT_CHEMISTRY,
) -> dict:
    """Run one bus block end-to-end through schedule conversion and simulation."""
    schedules = block_to_daily_schedules(
        block_df,
        ev_id=str(block_df["block_id"].iloc[0]) if "block_id" in block_df else "bus_block",
        consumption_kwh_per_km=consumption_kwh_per_km,
        depot_charge_kw=depot_charge_kw,
        allow_layover_charging=allow_layover_charging,
        layover_charge_kw=layover_charge_kw,
        min_layover_for_charging_h=min_layover_for_charging_h,
    )
    soc, load_kw, _soc_after_warmup = simulate_single_ev(
        schedules,
        battery_kwh,
        soc_init=soc_init,
        warm_up_days=0,
        chemistry=chemistry,
    )
    total_km = float(sum(trip.distance_km for schedule in schedules for trip in schedule.trips))
    total_consumed_kwh = float(
        sum(trip.energy_consumed_kwh for schedule in schedules for trip in schedule.trips)
    )
    energy_charged_kwh = float(np.sum(load_kw) * STEP_HOURS_DECISION)
    depot_kwh = _parking_energy_kwh(schedules, "depot_terminus")
    layover_kwh = _parking_energy_kwh(schedules, "layover")
    feasibility = scan_block_infeasibility(
        soc,
        schedules,
        battery_kwh,
        soc_init=soc_init,
        depot_charge_kw=depot_charge_kw,
        layover_charge_kw=layover_charge_kw,
        allow_layover_charging=allow_layover_charging,
    )
    deadhead_audit = _deadhead_audit_fields(schedules)

    return {
        "schedules": schedules,
        "soc": soc,
        "load_kw": load_kw,
        "soc_end": float(soc[-1]),
        "soc_min": float(soc.min()),
        "energy_charged_kwh": energy_charged_kwh,
        "depot_kwh": depot_kwh,
        "layover_kwh": layover_kwh,
        "total_km": total_km,
        "total_consumed_kwh": total_consumed_kwh,
        "battery_kwh": float(battery_kwh),
        "consumption_kwh_per_km": float(consumption_kwh_per_km),
        "depot_charge_kw": float(depot_charge_kw),
        "infeasible": bool(feasibility["infeasible"]),
        "first_floor_hit_h": feasibility["first_floor_hit_h"],
        "first_floor_trip_id": feasibility["first_floor_trip_id"],
        "shortfall_kwh": float(feasibility["shortfall_kwh"]),
        "infeasibility_reason": feasibility["infeasibility_reason"],
        **deadhead_audit,
    }


def _add_load(fleet_load_kw: np.ndarray, block_load_kw: np.ndarray) -> np.ndarray:
    """Wrap multi-day block loads into a 96-step representative service day."""
    n = STEPS_PER_DAY_DECISION
    full_days = block_load_kw.shape[0] // n
    for day_index in range(full_days):
        start = day_index * n
        fleet_load_kw += block_load_kw[start : start + n]

    remainder = block_load_kw.shape[0] - (full_days * n)
    if remainder > 0:
        fleet_load_kw[:remainder] += block_load_kw[full_days * n :]
    return fleet_load_kw


def simulate_fleet_blocks(
    df: pd.DataFrame,
    *,
    battery_kwh: float | None = None,
    consumption_kwh_per_km: float | None = None,
    depot_charge_kw: float | None = None,
    vehicle_params: pd.DataFrame | None = None,
    vehicle_rng: np.random.Generator | None = None,
    progress_interval: int = 0,
    **kwargs,
) -> tuple[pd.DataFrame, np.ndarray]:
    """Simulate all blocks and return per-block metrics plus aggregate load.

    Fleet load is aggregated as a single 96-step representative service day.
    Cross-midnight blocks contribute their day-1 tail wrapped back to the same
    hour-of-day on day 0, assuming steady-state service across consecutive days.
    """
    groups = df.groupby("block_id", sort=False)
    block_ids = list(groups.groups)
    sampled_specs: dict[object, pd.Series] = {}
    if vehicle_params is not None:
        if vehicle_rng is None:
            raise ValueError("vehicle_rng must be provided when vehicle_params is used.")
        sampled = sample_bus_vehicle_specs(vehicle_params, vehicle_rng, n=len(block_ids))
        sampled.insert(0, "block_id", block_ids)
        sampled_specs = {
            row["block_id"]: row
            for _, row in sampled.iterrows()
        }
    elif battery_kwh is None or consumption_kwh_per_km is None or depot_charge_kw is None:
        raise ValueError(
            "Provide either vehicle_params + vehicle_rng or fixed battery_kwh, "
            "consumption_kwh_per_km, and depot_charge_kw."
        )

    fleet_load_kw = np.zeros(STEPS_PER_DAY_DECISION, dtype=float)
    records: list[dict] = []
    total = len(groups)

    for index, (block_id, block_df) in enumerate(groups, 1):
        vehicle_spec = sampled_specs.get(block_id)
        block_battery_kwh = float(vehicle_spec["battery_kwh"]) if vehicle_spec is not None else float(battery_kwh)
        block_consumption_kwh_per_km = (
            float(vehicle_spec["consumption_kwh_per_km"])
            if vehicle_spec is not None
            else float(consumption_kwh_per_km)
        )
        block_depot_charge_kw = (
            float(vehicle_spec["depot_charge_kw"])
            if vehicle_spec is not None
            else float(depot_charge_kw)
        )
        try:
            result = simulate_block(
                block_df,
                battery_kwh=block_battery_kwh,
                consumption_kwh_per_km=block_consumption_kwh_per_km,
                depot_charge_kw=block_depot_charge_kw,
                **kwargs,
            )
            fleet_load_kw = _add_load(fleet_load_kw, result["load_kw"])
            record = {
                "block_id": block_id,
                "agency_id": block_df["agency_id"].iloc[0],
                "block_source": block_df["block_source"].iloc[0],
                "n_trips": int(len(block_df)),
                "n_schedule_days": int(len(result["schedules"])),
                "total_km": result["total_km"],
                "total_consumed_kwh": result["total_consumed_kwh"],
                "energy_charged_kwh": result["energy_charged_kwh"],
                "depot_kwh": result["depot_kwh"],
                "layover_kwh": result["layover_kwh"],
                "soc_end": result["soc_end"],
                "soc_min": result["soc_min"],
                "battery_kwh": block_battery_kwh,
                "consumption_kwh_per_km": block_consumption_kwh_per_km,
                "depot_charge_kw": block_depot_charge_kw,
                "infeasible": result["infeasible"],
                "first_floor_hit_h": result["first_floor_hit_h"],
                "first_floor_trip_id": result["first_floor_trip_id"],
                "shortfall_kwh": result["shortfall_kwh"],
                "infeasibility_reason": result["infeasibility_reason"],
                "deadhead_short_count": result["deadhead_short_count"],
                "deadhead_long_count": result["deadhead_long_count"],
                "deadhead_total_km": result["deadhead_total_km"],
                "deadhead_total_kwh": result["deadhead_total_kwh"],
                "deadhead_skipped_time_count": result["deadhead_skipped_time_count"],
                "deadhead_skipped_time_km": result["deadhead_skipped_time_km"],
                "deadhead_skipped_missing_coord_count": result["deadhead_skipped_missing_coord_count"],
                "simulation_error": "",
            }
        except Exception as exc:
            record = {
                "block_id": block_id,
                "agency_id": block_df["agency_id"].iloc[0],
                "block_source": block_df["block_source"].iloc[0],
                "n_trips": int(len(block_df)),
                "n_schedule_days": 0,
                "total_km": float(block_df["distance_km"].sum()) if "distance_km" in block_df else np.nan,
                "total_consumed_kwh": np.nan,
                "energy_charged_kwh": np.nan,
                "depot_kwh": np.nan,
                "layover_kwh": np.nan,
                "soc_end": np.nan,
                "soc_min": np.nan,
                "battery_kwh": block_battery_kwh,
                "consumption_kwh_per_km": block_consumption_kwh_per_km,
                "depot_charge_kw": block_depot_charge_kw,
                "infeasible": True,
                "first_floor_hit_h": np.nan,
                "first_floor_trip_id": "",
                "shortfall_kwh": np.nan,
                "infeasibility_reason": "simulation_error",
                "deadhead_short_count": 0,
                "deadhead_long_count": 0,
                "deadhead_total_km": 0.0,
                "deadhead_total_kwh": 0.0,
                "deadhead_skipped_time_count": 0,
                "deadhead_skipped_time_km": 0.0,
                "deadhead_skipped_missing_coord_count": 0,
                "simulation_error": str(exc),
            }
        if vehicle_spec is not None:
            record.update(
                {
                    "vehicle_make": str(vehicle_spec["make"]),
                    "vehicle_gen_model": str(vehicle_spec["gen_model"]),
                    "vehicle_stock_2025_q2": float(vehicle_spec["stock_2025_q2"]),
                }
            )
        records.append(record)
        if progress_interval > 0 and (index % progress_interval == 0 or index == total):
            print(f"  Simulated {index:,}/{total:,} bus blocks", flush=True)

    per_block = pd.DataFrame.from_records(records)
    if not per_block.empty:
        per_block = per_block.set_index("block_id")
    return per_block, fleet_load_kw

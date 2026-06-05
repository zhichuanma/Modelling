"""Stage 6 depot-only SOC walk and feasibility diagnostics."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .depot_only_events import DEPOT_CHARGING_EVENT_TYPES, MOVEMENT_EVENT_TYPES


def apply_depot_only_soc(events: pd.DataFrame, *, depot_power_kw: float = 100.0) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Apply a depot-only SOC walk.

    SOC is not clamped at zero. Negative SOC is preserved as an infeasibility
    diagnostic.
    """
    if events.empty:
        return events.copy(), pd.DataFrame(columns=_summary_columns())
    records: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for case_id, group in events.sort_values(["simulation_case_id", "event_seq"], kind="stable").groupby("simulation_case_id", sort=False):
        group = group.copy()
        first = group.iloc[0]
        battery = float(first.get("battery_kwh", np.nan))
        consumption = float(first.get("consumption_kwh_per_km", np.nan))
        ac_kw = float(first.get("ac_charge_kw_max", np.nan))
        usable_min = float(first.get("usable_soc_min", 0.10))
        usable_max = float(first.get("usable_soc_max", 0.95))
        valid_params = all(np.isfinite(value) and value > 0.0 for value in (battery, consumption, ac_kw)) and usable_min < usable_max
        soc = battery * usable_max if valid_params else np.nan
        min_soc = soc
        total_energy = 0.0
        passenger_km = 0.0
        deadhead_km = 0.0
        total_charge = 0.0
        largest_movement_energy = 0.0
        any_depot_window = False

        for _, row in group.iterrows():
            event = row.to_dict()
            event_type = str(event["event_type"])
            event["soc_start_kwh"] = soc
            event["energy_kwh"] = 0.0
            event["charge_kwh_added"] = 0.0
            event["charging_end_datetime"] = pd.NaT
            if not valid_params:
                event["soc_end_kwh"] = soc
                records.append(event)
                continue
            if event_type in MOVEMENT_EVENT_TYPES:
                distance = float(event.get("distance_km", 0.0) or 0.0)
                energy = distance * consumption
                soc -= energy
                event["energy_kwh"] = energy
                total_energy += energy
                largest_movement_energy = max(largest_movement_energy, energy)
                if event_type in {"passenger_block", "passenger_trip"}:
                    passenger_km += distance
                else:
                    deadhead_km += distance
            elif event_type in DEPOT_CHARGING_EVENT_TYPES and bool(event.get("can_charge", False)):
                any_depot_window = True
                duration_h = float(event.get("duration_min", 0.0) or 0.0) / 60.0
                effective_kw = max(0.0, min(ac_kw, float(depot_power_kw), float(event.get("charge_power_kw", depot_power_kw) or depot_power_kw)))
                max_soc = battery * usable_max
                charge = max(0.0, min(effective_kw * duration_h, max_soc - soc))
                soc += charge
                event["charge_power_kw"] = effective_kw
                event["effective_charge_kw"] = effective_kw
                event["charge_kwh_added"] = charge
                if effective_kw > 0.0 and charge > 0.0:
                    event["charging_end_datetime"] = pd.Timestamp(event["start_datetime"]) + pd.to_timedelta(charge / effective_kw, unit="h")
                total_charge += charge
            event["soc_end_kwh"] = soc
            min_soc = min(min_soc, soc)
            records.append(event)

        min_soc_pct = min_soc / battery if valid_params else np.nan
        threshold = battery * usable_min if valid_params else np.nan
        feasible = bool(valid_params and min_soc >= threshold)
        summary = {
            "simulation_case_id": str(case_id),
            "service_date": str(first.get("service_date", "")),
            "vehicle_id": str(first.get("vehicle_id", "")),
            "vehicle_model": str(first.get("vehicle_model", "")),
            "vehicle_subtype": str(first.get("vehicle_subtype", "")),
            "block_template_id": str(first.get("block_template_id", "")),
            "block_id": str(first.get("block_id", "")),
            "agency_id": str(first.get("agency_id", "")),
            "depot_id": str(first.get("depot_id", "")),
            "operational_depot_lsoa": str(first.get("operational_depot_lsoa", "")),
            "region_key": str(first.get("region_key", "unknown")),
            "sample_mode": str(first.get("sample_mode", "")),
            "weighting_mode": "unweighted_ev_stock_scenario",
            "battery_kwh": battery,
            "consumption_kwh_per_km": consumption,
            "ac_charge_kw_max": ac_kw,
            "depot_power_kw": float(depot_power_kw),
            "depot_power_source": "fixed_default_100kw" if float(depot_power_kw) == 100.0 else "fixed_cli_depot_power_kw",
            "total_passenger_km": float(passenger_km),
            "total_deadhead_km": float(deadhead_km),
            "total_energy_kwh": float(total_energy),
            "total_charge_kwh": float(total_charge),
            "min_soc_kwh": float(min_soc) if valid_params else np.nan,
            "min_soc_pct": float(min_soc_pct) if valid_params else np.nan,
            "energy_shortfall_kwh": float(max(0.0, -min_soc)) if valid_params else np.nan,
            "depot_only_feasible": feasible,
            "breaches_zero_soc": bool(valid_params and min_soc < 0.0),
            "breaches_usable_min_soc": bool(valid_params and min_soc < threshold),
            "infeasibility_reason": _reason(
                valid_params=valid_params,
                feasible=feasible,
                depot_lsoa=str(first.get("operational_depot_lsoa", "")),
                largest_movement_energy=largest_movement_energy,
                total_energy=total_energy,
                usable_energy=battery * (usable_max - usable_min) if valid_params else np.nan,
                any_depot_window=any_depot_window,
            ),
        }
        summaries.append(summary)
    return pd.DataFrame.from_records(records).reset_index(drop=True), pd.DataFrame.from_records(summaries, columns=_summary_columns()).reset_index(drop=True)


def _reason(
    *,
    valid_params: bool,
    feasible: bool,
    depot_lsoa: str,
    largest_movement_energy: float,
    total_energy: float,
    usable_energy: float,
    any_depot_window: bool,
) -> str:
    if not valid_params:
        return "invalid_vehicle_parameters"
    if not str(depot_lsoa).strip():
        return "missing_operational_depot_lsoa"
    if feasible:
        return ""
    if largest_movement_energy > usable_energy:
        return "single_movement_exceeds_usable_battery"
    if total_energy > usable_energy:
        return "daily_energy_exceeds_usable_battery"
    if any_depot_window:
        return "insufficient_depot_charging_time"
    return "unknown"


def _summary_columns() -> list[str]:
    return [
        "simulation_case_id",
        "service_date",
        "vehicle_id",
        "vehicle_model",
        "vehicle_subtype",
        "block_template_id",
        "block_id",
        "agency_id",
        "depot_id",
        "operational_depot_lsoa",
        "region_key",
        "sample_mode",
        "weighting_mode",
        "battery_kwh",
        "consumption_kwh_per_km",
        "ac_charge_kw_max",
        "depot_power_kw",
        "depot_power_source",
        "total_passenger_km",
        "total_deadhead_km",
        "total_energy_kwh",
        "total_charge_kwh",
        "min_soc_kwh",
        "min_soc_pct",
        "energy_shortfall_kwh",
        "depot_only_feasible",
        "breaches_zero_soc",
        "breaches_usable_min_soc",
        "infeasibility_reason",
    ]


__all__ = ["apply_depot_only_soc"]

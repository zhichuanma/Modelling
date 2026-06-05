"""Depot-only SOC walk for annual vehicle-day event ledgers."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .annual_depot_events import DEPOT_CHARGING_EVENT_TYPES, MOVEMENT_EVENT_TYPES


def apply_depot_only_soc(
    events: pd.DataFrame,
    *,
    depot_power_kw: float = 100.0,
    soc_mode: str = "daily_reset",
    soc_init_by_vehicle_day: dict[str, float] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fill SOC/charge columns and return vehicle-day summaries.

    SOC is not clamped at zero. Negative SOC is preserved as an infeasibility
    diagnostic, as required by the annual depot-only prompt.

    ``soc_mode="daily_reset"`` (default): every vehicle-day starts at
    ``battery_kwh * usable_soc_max``; consecutive days are independent and
    overlapping pre/overnight wall-clock windows are tolerated (any overlap adds
    zero energy because the day already starts full).

    ``soc_mode="carryover"`` (plan v2 §8): each vehicle-day starts at the exact
    stitch SOC carried from the previous day via ``soc_init_by_vehicle_day``
    (keyed by ``vehicle_day_id``). Event windows must be non-overlapping per
    vehicle in wall-clock time (per-vehicle ledger stitching, §3.3). A missing
    SOC state is fatal (§8.5) — it means the chronological simulation state is
    broken — so no default is ever applied.
    """
    if soc_mode not in ("daily_reset", "carryover"):
        raise ValueError(f"soc_mode must be 'daily_reset' or 'carryover', got {soc_mode!r}.")
    if soc_mode == "carryover" and soc_init_by_vehicle_day is None:
        raise ValueError("soc_mode='carryover' requires soc_init_by_vehicle_day.")
    if events.empty:
        return events.copy(), pd.DataFrame(columns=_summary_columns())
    records: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for vehicle_day_id, group in events.sort_values(["vehicle_day_id", "event_seq"], kind="stable").groupby("vehicle_day_id", sort=False):
        group = group.copy()
        first = group.iloc[0]
        battery_kwh = float(first.get("battery_kwh", np.nan))
        usable_min = float(first.get("usable_soc_min", 0.10))
        usable_max = float(first.get("usable_soc_max", 0.95))
        consumption = float(first.get("consumption_kwh_per_km", np.nan))
        ac_kw = float(first.get("ac_charge_kw_max", np.nan))
        valid_params = all(np.isfinite(value) and value > 0 for value in (battery_kwh, consumption, ac_kw)) and usable_min < usable_max
        if soc_mode == "carryover":
            # Carry-over: the day starts at the stitch-point SOC inherited from
            # the previous day's finalized window. The carried value is never
            # floored at usable_soc_min (recovery happens only through explicit
            # charging events, §5.2), and a missing state entry is fatal.
            if str(vehicle_day_id) not in soc_init_by_vehicle_day:
                raise KeyError(f"carryover: missing SOC state for vehicle_day_id={vehicle_day_id}")
            soc = float(soc_init_by_vehicle_day[str(vehicle_day_id)]) if valid_params else np.nan
        else:
            soc = battery_kwh * usable_max if valid_params else np.nan
        start_soc = soc
        min_soc = soc
        movement_energy_values: list[float] = []
        trip_energy_values: list[float] = []
        deadhead_km = 0.0
        passenger_km = 0.0
        total_charge = 0.0
        any_depot_window = False
        largest_movement_energy = 0.0

        for _, row in group.iterrows():
            event = row.to_dict()
            event_type = str(event["event_type"])
            duration_h = float(event.get("duration_min", 0.0)) / 60.0
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
                event["energy_kwh"] = energy
                soc -= energy
                movement_energy_values.append(energy)
                largest_movement_energy = max(largest_movement_energy, energy)
                if event_type in {"passenger_trip", "passenger_block"}:
                    passenger_km += distance
                    trip_energy_values.append(energy)
                else:
                    deadhead_km += distance
            elif event_type in DEPOT_CHARGING_EVENT_TYPES and bool(event.get("can_charge", False)):
                any_depot_window = True
                effective_kw = min(ac_kw, float(depot_power_kw), float(event.get("charge_power_kw", depot_power_kw) or depot_power_kw))
                max_soc = battery_kwh * usable_max
                available = max_soc - soc
                charge = max(0.0, min(effective_kw * duration_h, available))
                soc += charge
                event["charge_power_kw"] = effective_kw
                event["charge_kwh_added"] = charge
                if effective_kw > 0 and charge > 0:
                    charging_minutes = charge / effective_kw * 60.0
                    event["charging_end_datetime"] = pd.Timestamp(event["start_datetime"]) + pd.to_timedelta(charging_minutes, unit="m")
                total_charge += charge
            event["soc_end_kwh"] = soc
            min_soc = min(min_soc, soc)
            records.append(event)

        min_soc_pct = min_soc / battery_kwh if valid_params else np.nan
        feasible = bool(valid_params and min_soc >= battery_kwh * usable_min)
        shortfall = max(0.0, -min_soc) if valid_params else np.nan
        total_energy = float(np.nansum(movement_energy_values))
        end_soc_ts = pd.Timestamp(group.iloc[-1]["end_datetime"]) if len(group) else pd.NaT
        summary = {
            "service_date": str(first.get("service_date")),
            "vehicle_day_id": str(vehicle_day_id),
            "vehicle_spec_id": str(first.get("vehicle_spec_id")),
            "block_instance_id": str(first.get("block_instance_id")),
            "block_template_id": str(first.get("block_template_id")),
            "depot_id": str(first.get("depot_id")),
            "depot_lsoa": str(first.get("depot_lsoa", "")),
            "battery_kwh": battery_kwh,
            "consumption_kwh_per_km": consumption,
            "ac_charge_kw_max": ac_kw,
            "depot_power_kw": float(depot_power_kw),
            "total_passenger_km": float(passenger_km),
            "total_deadhead_km": float(deadhead_km),
            "total_energy_kwh": total_energy,
            "total_charge_kwh": float(total_charge),
            "min_soc_kwh": float(min_soc) if valid_params else np.nan,
            "min_soc_pct": float(min_soc_pct) if valid_params else np.nan,
            "start_soc_kwh": float(start_soc) if valid_params else np.nan,
            "end_soc_kwh": float(soc) if valid_params else np.nan,
            "end_soc_ts": end_soc_ts,
            "energy_shortfall_kwh": float(shortfall) if valid_params else np.nan,
            "depot_only_feasible": feasible,
            "breaches_zero_soc": bool(valid_params and min_soc < 0.0),
            "breaches_usable_min_soc": bool(valid_params and min_soc < battery_kwh * usable_min),
            "infeasibility_reason": _reason(
                valid_params=valid_params,
                depot_lsoa=str(first.get("depot_lsoa", "")),
                feasible=feasible,
                largest_movement_energy=largest_movement_energy,
                daily_energy=total_energy,
                usable_energy=battery_kwh * (usable_max - usable_min) if valid_params else np.nan,
                any_depot_window=any_depot_window,
            ),
            "scenario_mode": str(first.get("scenario_mode", "ev_stock_scale")),
        }
        summaries.append(summary)

    out_events = pd.DataFrame.from_records(records)
    summary_df = pd.DataFrame.from_records(summaries, columns=_summary_columns())
    return out_events.reset_index(drop=True), summary_df.reset_index(drop=True)


def _reason(
    *,
    valid_params: bool,
    depot_lsoa: str,
    feasible: bool,
    largest_movement_energy: float,
    daily_energy: float,
    usable_energy: float,
    any_depot_window: bool,
) -> str:
    if not valid_params:
        return "invalid_vehicle_parameters"
    if not str(depot_lsoa).strip():
        return "missing_depot_lsoa"
    if feasible:
        return ""
    if largest_movement_energy > usable_energy:
        return "single_trip_exceeds_usable_battery"
    if daily_energy > usable_energy:
        return "daily_energy_exceeds_usable_battery"
    if any_depot_window:
        return "insufficient_depot_charging_time"
    return "unknown"


def _summary_columns() -> list[str]:
    return [
        "service_date",
        "vehicle_day_id",
        "vehicle_spec_id",
        "block_instance_id",
        "block_template_id",
        "depot_id",
        "depot_lsoa",
        "battery_kwh",
        "consumption_kwh_per_km",
        "ac_charge_kw_max",
        "depot_power_kw",
        "total_passenger_km",
        "total_deadhead_km",
        "total_energy_kwh",
        "total_charge_kwh",
        "min_soc_kwh",
        "min_soc_pct",
        "start_soc_kwh",
        "end_soc_kwh",
        "end_soc_ts",
        "energy_shortfall_kwh",
        "depot_only_feasible",
        "breaches_zero_soc",
        "breaches_usable_min_soc",
        "infeasibility_reason",
        "scenario_mode",
    ]


__all__ = ["apply_depot_only_soc"]

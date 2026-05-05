"""Coach adapters around the vehicle-agnostic SOC simulator."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from mobility.core.constants import DEFAULT_CHEMISTRY, STEP_HOURS_DECISION
from mobility.core.simulator import simulate_single_ev

from .feasibility import journey_feasibility
from .trip_chain_coach import journey_to_daily_schedules


def _spec_value(ev_spec: Any, key: str, default: Any = None) -> Any:
    if isinstance(ev_spec, pd.Series):
        return ev_spec.get(key, default)
    if isinstance(ev_spec, dict):
        return ev_spec.get(key, default)
    return getattr(ev_spec, key, default)


def _first_floor_hit_h(soc: np.ndarray) -> float | None:
    hits = np.flatnonzero(np.asarray(soc, dtype=float) <= 1e-12)
    if hits.size == 0:
        return None
    return float((int(hits[0]) + 1) * STEP_HOURS_DECISION)


def simulate_coach_journey(
    journey_row: Any,
    stop_seq: pd.DataFrame,
    ev_spec: Any,
    *,
    terminus_charge_kw: float = 50.0,
    soc_init: float = 1.0,
    safety_margin: float = 0.05,
    chemistry: str = DEFAULT_CHEMISTRY,
) -> dict:
    """Run one coach journey through feasibility then SOC simulation."""
    battery_kwh = float(_spec_value(ev_spec, "battery_kwh"))
    consumption_kwh_per_km = float(_spec_value(ev_spec, "consumption_kwh_per_km"))
    distance_km = float(journey_row["distance_km"] if isinstance(journey_row, pd.Series) else _spec_value(journey_row, "distance_km"))

    feasibility = journey_feasibility(
        distance_km,
        battery_kwh=battery_kwh,
        consumption_kwh_per_km=consumption_kwh_per_km,
        safety_margin=safety_margin,
    )
    schedules = journey_to_daily_schedules(
        journey_row,
        stop_seq,
        consumption_kwh_per_km=consumption_kwh_per_km,
        terminus_charge_kw=terminus_charge_kw,
    )
    soc, load_kw, soc_after_warmup = simulate_single_ev(
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
    terminus_kwh = float(
        sum(
            event.energy_charged_kwh
            for schedule in schedules
            for event in schedule.parking_events
            if event.location_purpose == "terminus_dwell"
        )
    )
    soc_floor_hit_h = _first_floor_hit_h(soc)

    return {
        "schedules": schedules,
        "soc": soc,
        "load_kw": load_kw,
        "soc_after_warmup": float(soc_after_warmup),
        "soc_end": float(soc[-1]),
        "soc_min": float(soc.min()),
        "soc_floor_hit_h": soc_floor_hit_h,
        "energy_charged_kwh": energy_charged_kwh,
        "terminus_kwh": terminus_kwh,
        "total_km": total_km,
        "total_consumed_kwh": total_consumed_kwh,
        "battery_kwh": battery_kwh,
        "consumption_kwh_per_km": consumption_kwh_per_km,
        "terminus_charge_kw": float(terminus_charge_kw),
        "feasibility": feasibility,
        "feasible_single_charge": bool(feasibility["feasible_single_charge"]),
        "soc_clamped_to_zero": bool(soc_floor_hit_h is not None),
    }

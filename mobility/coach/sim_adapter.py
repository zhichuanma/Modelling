"""Coach adapters around the vehicle-agnostic SOC simulator.

``soc_init`` and ``pre_journey_dwell_h`` are coupled: when ``soc_init`` is left
as ``None`` the starting SoC is auto-derived so that ``pre_journey_dwell_h``
hours of charging at ``terminus_charge_kw`` would refill the battery to full,
turning the pre-journey dwell into a real charging window.
"""

from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd

from mobility.core.constants import DEFAULT_CHEMISTRY, STEP_HOURS_DECISION
from mobility.core.simulator import simulate_single_ev

from ._compat import field
from .feasibility import journey_feasibility
from .trip_chain_coach import journey_to_daily_schedules


DEFAULT_TERMINUS_CHARGE_KW = 50.0


def _battery_kwh(ev_spec: Any) -> float:
    value = field(ev_spec, "Energy_kWh", field(ev_spec, "battery_kwh"))
    return float(value)


def _first_floor_hit_h(soc: np.ndarray) -> float | None:
    hits = np.flatnonzero(np.asarray(soc, dtype=float) <= 1e-12)
    if hits.size == 0:
        return None
    return float((int(hits[0]) + 1) * STEP_HOURS_DECISION)


def _energy_in_window_kwh(load_kw: np.ndarray, start_h: float, end_h: float, step_h: float) -> float:
    start_idx = max(0, int(round(start_h / step_h)))
    end_idx = min(len(load_kw), int(round(end_h / step_h)))
    if end_idx <= start_idx:
        return 0.0
    return float(load_kw[start_idx:end_idx].sum() * step_h)


def _parking_load_energy_kwh(
    schedules: list,
    load_kw: np.ndarray,
    *,
    purpose: str,
    step_h: float,
) -> float:
    total = 0.0
    for schedule in schedules:
        day_offset_h = float(schedule.day) * 24.0
        for event in schedule.parking_events:
            if event.location_purpose != purpose:
                continue
            total += _energy_in_window_kwh(
                load_kw,
                day_offset_h + float(event.start_time),
                day_offset_h + float(event.end_time),
                step_h,
            )
    return float(total)


def simulate_coach_journey(
    journey_row: Any,
    stop_seq: pd.DataFrame,
    ev_spec: Any,
    *,
    terminus_charge_kw: float = DEFAULT_TERMINUS_CHARGE_KW,
    pre_journey_dwell_h: float = 6.0,
    soc_init: Optional[float] = None,
    safety_margin: float = 0.05,
    chemistry: str = DEFAULT_CHEMISTRY,
) -> dict:
    """Run one coach journey through feasibility then SOC simulation."""
    battery_kwh = _battery_kwh(ev_spec)
    consumption_kwh_per_km = float(field(ev_spec, "consumption_kwh_per_km"))
    distance_km = float(field(journey_row, "distance_km"))

    if soc_init is None:
        derived = 1.0 - (float(pre_journey_dwell_h) * float(terminus_charge_kw) / battery_kwh)
        soc_init = max(0.0, min(1.0, derived))

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
        pre_journey_dwell_h=pre_journey_dwell_h,
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
    terminus_kwh = _parking_load_energy_kwh(
        schedules,
        load_kw,
        purpose="terminus_dwell",
        step_h=STEP_HOURS_DECISION,
    )
    soc_floor_hit_h = _first_floor_hit_h(soc)
    ev_model = field(ev_spec, "Model", field(ev_spec, "model", ""))

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
        "ev_model": str(ev_model),
        "battery_kwh": battery_kwh,
        "consumption_kwh_per_km": consumption_kwh_per_km,
        "terminus_charge_kw": float(terminus_charge_kw),
        "feasibility": feasibility,
        "feasible_single_charge": bool(feasibility["feasible_single_charge"]),
        "soc_clamped_to_zero": bool(soc_floor_hit_h is not None),
    }

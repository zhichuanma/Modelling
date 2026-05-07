"""Bus block feasibility checks layered above the clamped core simulator."""

from __future__ import annotations

from typing import Iterable

import numpy as np

from mobility.core.constants import (
    CV_THRESHOLD,
    DEFAULT_CHEMISTRY,
    STEP_HOURS_DECISION,
    STEPS_PER_DAY_DECISION,
)
from mobility.core.data_structures import DailySchedule


INFEASIBILITY_REASONS = (
    "single_trip_exceeds_battery",
    "starts_below_min_required",
    "depot_only_insufficient",
    "midday_depletion",
)


def _ordered_schedules(schedules: Iterable[DailySchedule]) -> list[DailySchedule]:
    return sorted(list(schedules), key=lambda schedule: (int(schedule.day), str(schedule.date or "")))


def _step_arrays(
    schedules: list[DailySchedule],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    n_days = len(schedules)
    n_steps = n_days * STEPS_PER_DAY_DECISION
    trip_energy = np.zeros(n_steps, dtype=float)
    park_power = np.zeros(n_steps, dtype=float)
    event_id = np.full(n_steps, "", dtype=object)
    event_type = np.full(n_steps, "", dtype=object)
    local_starts = np.arange(STEPS_PER_DAY_DECISION, dtype=float) * STEP_HOURS_DECISION
    local_ends = local_starts + STEP_HOURS_DECISION

    for day_index, schedule in enumerate(schedules):
        offset = day_index * STEPS_PER_DAY_DECISION
        for trip in schedule.trips:
            dep = float(trip.departure_time)
            arr = float(trip.arrival_time)
            duration = max(arr - dep, 0.01)
            rate_per_hour = float(trip.energy_consumed_kwh) / duration
            overlap = np.maximum(0.0, np.minimum(local_ends, arr) - np.maximum(local_starts, dep))
            touched = overlap > 0.0
            trip_energy[offset : offset + STEPS_PER_DAY_DECISION] += overlap * rate_per_hour
            empty = event_id[offset : offset + STEPS_PER_DAY_DECISION] == ""
            fill = touched & empty
            event_id[offset : offset + STEPS_PER_DAY_DECISION][fill] = str(trip.trip_id)
            event_type[offset : offset + STEPS_PER_DAY_DECISION][fill] = (
                "deadhead" if getattr(trip, "is_deadhead", False) else "trip"
            )

        for event in schedule.parking_events:
            if not event.can_charge or event.charge_power_kw <= 0.0:
                continue
            overlap = np.maximum(
                0.0,
                np.minimum(local_ends, float(event.end_time)) - np.maximum(local_starts, float(event.start_time)),
            )
            fraction = overlap / STEP_HOURS_DECISION
            park_power[offset : offset + STEPS_PER_DAY_DECISION] += float(event.charge_power_kw) * fraction

    return trip_energy, park_power, event_id, event_type


def shadow_soc_walk(
    schedules: list[DailySchedule],
    *,
    battery_kwh: float,
    soc_init: float,
    depot_charge_kw: float,
    layover_charge_kw: float,
    allow_layover_charging: bool,
    chemistry: str = DEFAULT_CHEMISTRY,
) -> dict:
    """Replay trips/parking with no lower SOC clamp.

    The charge replay mirrors the current linear CC-CV envelope in
    ``mobility.core.simulator`` and uses the charge powers already attached to
    each ``ParkingEvent``. The depot/layover arguments are retained for audit
    parity with ``simulate_block``.
    """
    # Charging policy is already encoded in ParkingEvent.can_charge and
    # ParkingEvent.charge_power_kw by the schedule builder/sim adapter, so the
    # shadow walk intentionally does not re-interpret allow_layover_charging.
    del depot_charge_kw, layover_charge_kw, allow_layover_charging
    ordered = _ordered_schedules(schedules)
    trip_energy, park_power, event_id, event_type = _step_arrays(ordered)
    soc_unclamped = np.zeros(trip_energy.shape[0], dtype=float)
    time_h = np.arange(trip_energy.shape[0], dtype=float) * STEP_HOURS_DECISION
    cv_threshold = float(CV_THRESHOLD[chemistry])
    inv_cap = 1.0 / float(battery_kwh) if battery_kwh > 0.0 else 0.0
    soc = float(soc_init)

    for step in range(trip_energy.shape[0]):
        soc -= trip_energy[step] * inv_cap
        pp = park_power[step]
        if pp > 0.0:
            if soc < cv_threshold:
                eff_pp = pp
            else:
                factor = (1.0 - soc) / (1.0 - cv_threshold)
                eff_pp = pp * max(0.0, factor)
            if eff_pp > 0.0:
                headroom = max(0.0, (1.0 - soc) * float(battery_kwh))
                max_charge = eff_pp * STEP_HOURS_DECISION
                actual = min(max_charge, headroom)
                soc += actual * inv_cap
        soc_unclamped[step] = soc

    return {
        "time_h": time_h,
        "soc_unclamped": soc_unclamped,
        "event_id": event_id,
        "event_type": event_type,
    }


def _all_trips(schedules: list[DailySchedule]) -> list:
    return [trip for schedule in _ordered_schedules(schedules) for trip in sorted(schedule.trips, key=lambda t: t.departure_time)]


def _first_trip_and_schedule(schedules: list[DailySchedule]) -> tuple[object, DailySchedule] | tuple[None, None]:
    for schedule in _ordered_schedules(schedules):
        trips = sorted(schedule.trips, key=lambda trip: trip.departure_time)
        if trips:
            return trips[0], schedule
    return None, None


def _pre_first_trip_charge_kwh(first_trip, first_schedule: DailySchedule) -> float:
    return float(
        sum(
            max(0.0, float(event.duration_hours)) * max(0.0, float(event.charge_power_kw))
            for event in first_schedule.parking_events
            if event.can_charge
            and float(event.end_time) <= float(first_trip.departure_time)
            and float(event.charge_power_kw) > 0.0
        )
    )


def _depot_potential_kwh(schedules: list[DailySchedule]) -> float:
    return float(
        sum(
            max(0.0, float(event.duration_hours)) * max(0.0, float(event.charge_power_kw))
            for schedule in schedules
            for event in schedule.parking_events
            if event.location_purpose == "depot_terminus" and event.can_charge
        )
    )


def block_preflight(
    schedules: list[DailySchedule],
    *,
    battery_kwh: float,
    consumption_kwh_per_km: float,
    depot_charge_kw: float,
    layover_charge_kw: float,
    allow_layover_charging: bool,
    soc_init: float,
    reserve_soc_fraction: float = 0.0,
) -> dict:
    """Cheap bus block checks; returns a reason candidate and never raises."""
    del consumption_kwh_per_km, depot_charge_kw, layover_charge_kw
    trips = _all_trips(schedules)
    usable_kwh = float(battery_kwh) * max(0.0, 1.0 - float(reserve_soc_fraction))
    if any(float(trip.energy_consumed_kwh) > usable_kwh for trip in trips):
        return {"infeasibility_reason": "single_trip_exceeds_battery"}

    first_trip, first_schedule = _first_trip_and_schedule(schedules)
    if first_trip is not None and first_schedule is not None:
        reserve_kwh = float(reserve_soc_fraction) * float(battery_kwh)
        first_available_kwh = min(
            float(battery_kwh),
            float(soc_init) * float(battery_kwh) + _pre_first_trip_charge_kwh(first_trip, first_schedule),
        )
        if first_available_kwh < float(first_trip.energy_consumed_kwh) + reserve_kwh:
            return {"infeasibility_reason": "starts_below_min_required"}

    if not allow_layover_charging:
        total_trip_kwh = sum(float(trip.energy_consumed_kwh) for trip in trips)
        physical_limit_kwh = float(soc_init) * float(battery_kwh) + _depot_potential_kwh(schedules)
        if total_trip_kwh > physical_limit_kwh + 1e-9:
            return {"infeasibility_reason": "depot_only_insufficient"}

    return {"infeasibility_reason": None}


def scan_block_infeasibility(
    soc: np.ndarray,
    schedules: list[DailySchedule],
    battery_kwh: float,
    *,
    soc_init: float,
    depot_charge_kw: float,
    layover_charge_kw: float,
    allow_layover_charging: bool,
    reserve_soc_fraction: float = 0.0,
    soc_floor: float = 1e-9,
    time_grid_h: np.ndarray | None = None,
) -> dict:
    """Post-simulation scan using clamped SOC plus an unclamped shadow walk."""
    soc_array = np.asarray(soc, dtype=float)
    shadow = shadow_soc_walk(
        schedules,
        battery_kwh=battery_kwh,
        soc_init=soc_init,
        depot_charge_kw=depot_charge_kw,
        layover_charge_kw=layover_charge_kw,
        allow_layover_charging=allow_layover_charging,
    )
    soc_unclamped = np.asarray(shadow["soc_unclamped"], dtype=float)
    shortfall_kwh = max(0.0, -float(np.nanmin(soc_unclamped)) * float(battery_kwh)) if soc_unclamped.size else 0.0
    clamped_hits = np.flatnonzero(soc_array <= soc_floor)
    shadow_hits = np.flatnonzero(soc_unclamped <= soc_floor)
    first_step = int(clamped_hits[0]) if clamped_hits.size else (int(shadow_hits[0]) if shadow_hits.size else None)
    infeasible = bool(shortfall_kwh > 1e-9 or clamped_hits.size > 0)

    preflight = block_preflight(
        schedules,
        battery_kwh=battery_kwh,
        consumption_kwh_per_km=0.0,
        depot_charge_kw=depot_charge_kw,
        layover_charge_kw=layover_charge_kw,
        allow_layover_charging=allow_layover_charging,
        soc_init=soc_init,
        reserve_soc_fraction=reserve_soc_fraction,
    )
    reason = preflight["infeasibility_reason"]
    if infeasible and reason is None:
        reason = "midday_depletion"
    if not infeasible:
        reason = None
        shortfall_kwh = 0.0

    if first_step is None:
        first_h = None
        first_trip_id = None
    else:
        grid = np.asarray(time_grid_h, dtype=float) if time_grid_h is not None else shadow["time_h"]
        first_h = float(grid[first_step]) if first_step < grid.shape[0] else None
        first_trip_id = str(shadow["event_id"][first_step]) or None

    return {
        "infeasible": bool(infeasible),
        "first_floor_hit_step": first_step,
        "first_floor_hit_h": first_h,
        "first_floor_trip_id": first_trip_id,
        "shortfall_kwh": float(shortfall_kwh),
        "infeasibility_reason": reason,
        "n_steps_at_floor": int(clamped_hits.size),
    }

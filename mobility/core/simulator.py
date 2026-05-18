"""Uncontrolled charging simulation engine.

Time resolution: 15-minute steps (96 steps per day).
Charging logic: plug-in immediately at parking, stop when parking ends or
SOC reaches 100%. Power follows a CC-CV approximation: constant power below
CV_THRESHOLD SOC, then linear taper to zero at SOC=1.0.
SOC carries over across days.

Unit conventions
----------------
- load_profile[step] is the AVERAGE POWER (kW) over that step,
  NOT energy. The step duration is controlled by STEP_HOURS.
- energy_kwh_step = load_profile[step] * STEP_HOURS
- All exported DataFrame columns carrying a physical quantity
  must use an explicit unit suffix:
    power   -> _kw
    energy  -> _kwh
    SOC     -> _soc (dimensionless, 0..1)
    distance-> _km
    time    -> _h or _min
"""

from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

from .constants import (
    CV_THRESHOLD,
    DEFAULT_CHEMISTRY,
    SOC_SAFETY_MARGIN,
    STEP_HOURS_PROFILE,
    STEPS_PER_DAY_DECISION,
    STEPS_PER_DAY_PROFILE,
    WARMUP_DAYS,
)
from .data_structures import DailySchedule

STEPS_PER_DAY = STEPS_PER_DAY_DECISION
STEP_HOURS = 24.0 / STEPS_PER_DAY
MINUTES_PER_HOUR = 60.0
MINUTES_PER_DAY = 24.0 * MINUTES_PER_HOUR

# Pre-computed step boundaries (shape: (96,) each)
_STEP_STARTS = np.arange(STEPS_PER_DAY, dtype=float) * STEP_HOURS
_STEP_ENDS = _STEP_STARTS + STEP_HOURS


def _soc_grid_minutes(soc_profile: np.ndarray) -> np.ndarray:
    """Return the minute grid associated with a decision-layer SOC profile."""
    num_points = int(soc_profile.shape[0])
    if num_points == STEPS_PER_DAY_DECISION + 1:
        return np.linspace(0.0, MINUTES_PER_DAY, num=num_points)
    if num_points == STEPS_PER_DAY_DECISION:
        return np.arange(num_points, dtype=float) * (MINUTES_PER_DAY / STEPS_PER_DAY_DECISION)
    raise ValueError(
        "soc_profile must have length STEPS_PER_DAY_DECISION or "
        "STEPS_PER_DAY_DECISION + 1."
    )


def _load_profile_step_hours(load_profile: np.ndarray) -> float:
    """Infer the step duration for a returned load profile."""
    num_points = int(load_profile.shape[0])
    if num_points == STEPS_PER_DAY_PROFILE:
        return STEP_HOURS_PROFILE
    if num_points <= 0:
        raise ValueError("load_profile must be non-empty.")
    return 24.0 / float(num_points)


def _fill_session_soc(
    schedule: DailySchedule,
    soc_profile: np.ndarray,
    load_profile: np.ndarray,
) -> None:
    """Backfill session-level SOC and charged energy fields in place."""
    soc_values = np.asarray(soc_profile, dtype=float)
    load_values = np.asarray(load_profile, dtype=float)
    soc_grid_minutes = _soc_grid_minutes(soc_values)

    load_step_hours = _load_profile_step_hours(load_values)
    load_step_minutes = load_step_hours * MINUTES_PER_HOUR
    load_step_starts = np.arange(load_values.shape[0], dtype=float) * load_step_minutes
    load_step_ends = load_step_starts + load_step_minutes
    step_energy_kwh = load_values * load_step_hours

    park_weight_per_step = np.zeros(load_values.shape[0], dtype=float)
    if load_values.shape[0] != STEPS_PER_DAY_PROFILE:
        for parking_event in schedule.parking_events:
            if not parking_event.can_charge or parking_event.charge_power_kw <= 0.0:
                continue
            start_min = parking_event.start_time * MINUTES_PER_HOUR
            end_min = parking_event.end_time * MINUTES_PER_HOUR
            overlap_minutes = np.maximum(
                0.0,
                np.minimum(load_step_ends, end_min) - np.maximum(load_step_starts, start_min),
            )
            overlap_fraction = overlap_minutes / load_step_minutes
            park_weight_per_step += parking_event.charge_power_kw * overlap_fraction

    for parking_event in schedule.parking_events:
        start_min = parking_event.start_time * MINUTES_PER_HOUR
        end_min = parking_event.end_time * MINUTES_PER_HOUR
        if end_min < start_min:
            raise ValueError("Cross-day parking events are not supported in Stage 0.")

        parking_event.soc_on_arrival = float(
            np.interp(start_min, soc_grid_minutes, soc_values)
        )
        parking_event.soc_on_departure = float(
            np.interp(end_min, soc_grid_minutes, soc_values)
        )

        overlap_minutes = np.maximum(
            0.0,
            np.minimum(load_step_ends, end_min) - np.maximum(load_step_starts, start_min),
        )
        overlap_fraction = overlap_minutes / load_step_minutes
        if load_values.shape[0] == STEPS_PER_DAY_PROFILE:
            parking_event.energy_charged_kwh = float(np.sum(step_energy_kwh * overlap_fraction))
            continue

        event_weight = np.zeros(load_values.shape[0], dtype=float)
        if parking_event.can_charge and parking_event.charge_power_kw > 0.0:
            event_weight = parking_event.charge_power_kw * overlap_fraction
        event_share = np.divide(
            event_weight,
            park_weight_per_step,
            out=np.zeros_like(event_weight),
            where=park_weight_per_step > 0.0,
        )
        parking_event.energy_charged_kwh = float(np.sum(step_energy_kwh * event_share))


def _fill_trip_soc(
    schedule: DailySchedule,
    soc_profile: np.ndarray,
) -> None:
    """Backfill trip-level SOC fields for downstream observability artifacts."""
    soc_values = np.asarray(soc_profile, dtype=float)
    soc_grid_minutes = _soc_grid_minutes(soc_values)

    for trip in schedule.trips:
        dep_min = trip.departure_time * MINUTES_PER_HOUR
        arr_min = trip.arrival_time * MINUTES_PER_HOUR
        trip.soc_before_trip = float(np.interp(dep_min, soc_grid_minutes, soc_values))
        trip.soc_after_trip = float(np.interp(arr_min, soc_grid_minutes, soc_values))


def compute_next_trip_soc_floor(
    schedule: DailySchedule,
    battery_kwh: float,
    safety: float = 0.05,
) -> None:
    """Set ParkingEvent.soc_min_required up to the next home stop or day end."""
    if battery_kwh <= 0.0:
        raise ValueError("battery_kwh must be positive.")

    parking_events = sorted(schedule.parking_events, key=lambda pe: pe.start_time)
    trips = sorted(schedule.trips, key=lambda trip: trip.departure_time)

    for index, parking_event in enumerate(parking_events):
        next_home_start = None
        for future_event in parking_events[index + 1:]:
            if future_event.location_purpose == "home":
                next_home_start = future_event.start_time
                break

        remaining_kwh = 0.0
        for trip in trips:
            if trip.departure_time < parking_event.end_time:
                continue
            if next_home_start is not None and trip.departure_time >= next_home_start:
                break
            remaining_kwh += trip.energy_consumed_kwh

        required_soc = (remaining_kwh / battery_kwh) + safety
        parking_event.soc_min_required = float(np.clip(required_soc, 0.0, 1.0))


def simulate_single_day(
    schedule: DailySchedule,
    battery_capacity_kwh: float,
    soc_start: float = 1.0,
    *,
    cv_threshold: float = CV_THRESHOLD[DEFAULT_CHEMISTRY],
) -> Tuple[np.ndarray, np.ndarray, float]:
    """Simulate one EV for one day under uncontrolled charging.

    Returns (soc_profile[96], load_profile[96], soc_end). ``cv_threshold`` is
    the CC-CV linear taper start SOC.
    """
    soc_profile = np.zeros(STEPS_PER_DAY)
    load_profile = np.zeros(STEPS_PER_DAY)

    # ---- Pre-build trip consumption per step as a flat array ----
    trip_energy_per_step = np.zeros(STEPS_PER_DAY)
    for trip in schedule.trips:
        dep, arr = trip.departure_time, trip.arrival_time
        dur = max(arr - dep, 0.01)
        energy = trip.energy_consumed_kwh
        # Rate of energy consumption per hour for this trip
        rate_per_hour = energy / dur
        # Overlap of this trip with each 15-min step
        overlap = np.maximum(
            0.0,
            np.minimum(_STEP_ENDS, arr) - np.maximum(_STEP_STARTS, dep),
        )
        trip_energy_per_step += overlap * rate_per_hour

    # ---- Pre-build parking charge info per step ----
    # For each step: max possible charge power (kW) and whether charging is available
    park_power_per_step = np.zeros(STEPS_PER_DAY)
    for parking_event in schedule.parking_events:
        if not parking_event.can_charge or parking_event.charge_power_kw <= 0:
            continue
        overlap = np.maximum(
            0.0,
            np.minimum(_STEP_ENDS, parking_event.end_time)
            - np.maximum(_STEP_STARTS, parking_event.start_time),
        )
        fraction = overlap / STEP_HOURS  # 0-1 fraction of step spent parking
        park_power_per_step += parking_event.charge_power_kw * fraction

    # ---- Sequential SOC walk (must be sequential due to SOC dependency) ----
    _soc_walk(
        trip_energy_per_step,
        park_power_per_step,
        battery_capacity_kwh,
        soc_start,
        soc_profile,
        load_profile,
        cv_threshold,
    )

    soc_with_start = np.concatenate((np.array([soc_start], dtype=float), soc_profile))
    _fill_session_soc(
        schedule,
        soc_with_start,
        load_profile,
    )
    _fill_trip_soc(schedule, soc_with_start)
    compute_next_trip_soc_floor(
        schedule,
        battery_capacity_kwh,
        safety=SOC_SAFETY_MARGIN,
    )

    return soc_profile, load_profile, float(soc_profile[-1])


def _soc_walk(
    trip_energy: np.ndarray,
    park_power: np.ndarray,
    cap: float,
    soc_start: float,
    soc_out: np.ndarray,
    load_out: np.ndarray,
    cv_threshold: float,
) -> None:
    """Tight SOC walk loop kept separate for possible future acceleration."""
    inv_cap = 1.0 / cap if cap > 0 else 0.0
    soc = soc_start

    for step in range(STEPS_PER_DAY):
        # Discharge
        soc -= trip_energy[step] * inv_cap
        if soc < 0.0:
            soc = 0.0

        # Charge (CC-CV: linear power taper above CV_THRESHOLD)
        pp = park_power[step]
        if pp > 0.0:
            if soc < cv_threshold:
                eff_pp = pp
            else:
                factor = (1.0 - soc) / (1.0 - cv_threshold)
                if factor < 0.0:
                    factor = 0.0
                eff_pp = pp * factor
            if eff_pp > 0.0:
                headroom = (1.0 - soc) * cap
                max_ch = eff_pp * STEP_HOURS
                actual = max_ch if max_ch <= headroom else headroom
                soc += actual * inv_cap
                load_out[step] = eff_pp * (actual / max_ch)

        soc_out[step] = soc


def _validate_warm_up_days(
    daily_schedules: List[DailySchedule],
    warm_up_days: int,
) -> int:
    """Validate the warm-up window for a multi-day simulation."""
    if warm_up_days < 0:
        raise ValueError("warm_up_days must be non-negative.")
    if warm_up_days >= len(daily_schedules):
        raise ValueError("warm_up_days must be smaller than len(daily_schedules).")
    return int(warm_up_days)


def simulate_single_ev(
    daily_schedules: List[DailySchedule],
    battery_capacity_kwh: float,
    soc_init: float = 1.0,
    *,
    warm_up_days: int = WARMUP_DAYS,
    chemistry: str = DEFAULT_CHEMISTRY,
) -> Tuple[np.ndarray, np.ndarray, float]:
    """Return (soc_post, load_post, soc_after_warmup).

    - ``warm_up_days == 0``: no warm-up. ``soc_after_warmup`` equals
      ``soc_init`` and the returned arrays remain bit-identical to the
      pre-Stage-3 multi-day wrapper.
    - ``0 < warm_up_days < len(daily_schedules)``: the first
      ``warm_up_days`` days are simulated to drive SOC burn-in, but their
      SOC/load arrays are discarded. ``soc_after_warmup`` is the SOC at the
      start of the first retained day, i.e. the final SOC after the last
      warm-up day.
    - ``warm_up_days >= len(daily_schedules)`` or negative values raise
      ``ValueError``.

    Warm-up days still mutate ``ParkingEvent`` in place through
    ``_fill_session_soc`` and ``compute_next_trip_soc_floor``. Callers that
    inspect session-level fields such as ``energy_charged_kwh`` must apply
    their own ``sched.day >= warm_up_days`` filtering if they only want the
    steady-state portion.
    """
    warm_up_days = _validate_warm_up_days(daily_schedules, warm_up_days)
    try:
        cv_threshold = CV_THRESHOLD[chemistry]
    except KeyError:
        raise ValueError(
            f"Unknown chemistry {chemistry!r}; expected one of {sorted(CV_THRESHOLD)}."
        ) from None
    num_days = len(daily_schedules)
    retained_days = num_days - warm_up_days
    soc_all = np.empty(retained_days * STEPS_PER_DAY)
    load_all = np.empty(retained_days * STEPS_PER_DAY)
    soc = soc_init
    soc_after_warmup = float(soc_init)

    for day_index, schedule in enumerate(daily_schedules):
        soc_day, load_day, soc = simulate_single_day(
            schedule,
            battery_capacity_kwh,
            soc_start=soc,
            cv_threshold=cv_threshold,
        )

        if warm_up_days == 0:
            start = day_index * STEPS_PER_DAY
            end = start + STEPS_PER_DAY
            soc_all[start:end] = soc_day
            load_all[start:end] = load_day
            continue

        if day_index < warm_up_days:
            if day_index == warm_up_days - 1:
                soc_after_warmup = float(soc)
            continue

        retained_day_index = day_index - warm_up_days
        start = retained_day_index * STEPS_PER_DAY
        end = start + STEPS_PER_DAY
        soc_all[start:end] = soc_day
        load_all[start:end] = load_day

    return soc_all, load_all, soc_after_warmup


def simulate_fleet(
    fleet_schedules: Dict[str, List[DailySchedule]],
    ev_fleet: pd.DataFrame,
    soc_init: float = 1.0,
    progress_interval: int = 0,
    *,
    warm_up_days: int = WARMUP_DAYS,
    chemistry_default: str = DEFAULT_CHEMISTRY,
) -> Dict[str, dict]:
    """Simulate the entire fleet.

    Parameters
    ----------
    fleet_schedules : dict ev_id -> list of DailySchedule
    ev_fleet : DataFrame with EV_ID, battery_capacity_kwh
    soc_init : initial SOC for all EVs
    progress_interval : print progress every N EVs (0 = silent)
    warm_up_days : number of leading days used only for SOC burn-in
    chemistry_default : fallback chemistry when ev_fleet has no chemistry
    column or a per-EV chemistry value is missing/NaN

    Returns
    -------
    results : dict ev_id -> {"soc": ndarray, "load": ndarray, "soc_after_warmup": float}

    Warm-up days still run the in-place ParkingEvent SOC/energy backfill even
    though their arrays are stripped from the returned steady-state profiles.
    A per-EV ``chemistry`` column on ``ev_fleet`` is optional.
    """
    battery_map = dict(zip(ev_fleet["EV_ID"], ev_fleet["battery_capacity_kwh"]))
    chemistry_map = None
    if "chemistry" in ev_fleet.columns:
        chemistry_map = dict(zip(ev_fleet["EV_ID"], ev_fleet["chemistry"]))
    results: Dict[str, dict] = {}
    total = len(fleet_schedules)

    for index, (ev_id, schedules) in enumerate(fleet_schedules.items(), 1):
        if len(schedules) <= warm_up_days:
            raise ValueError(
                f"EV {ev_id} has len(schedules)={len(schedules)} which must be greater "
                f"than warm_up_days={warm_up_days}."
            )
        cap = battery_map.get(ev_id, 60.0)
        if cap is None or (isinstance(cap, float) and np.isnan(cap)) or cap <= 0:
            cap = 60.0
        chemistry = chemistry_default
        if chemistry_map is not None:
            chemistry_value = chemistry_map.get(ev_id, chemistry_default)
            if not pd.isna(chemistry_value):
                chemistry = str(chemistry_value)
        soc_arr, load_arr, soc_after_warmup = simulate_single_ev(
            schedules,
            cap,
            soc_init,
            warm_up_days=warm_up_days,
            chemistry=chemistry,
        )
        results[ev_id] = {
            "soc": soc_arr,
            "load": load_arr,
            "soc_after_warmup": float(soc_after_warmup),
        }

        if progress_interval > 0 and (index % progress_interval == 0 or index == total):
            print(f"  Simulated {index:,}/{total:,} EVs", flush=True)

    return results

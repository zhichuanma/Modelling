"""Build trip-chain pools from NTS data and assign them to EVs."""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Callable, Dict, List, Optional, Tuple
import warnings

import numpy as np
import pandas as pd

from mobility.cars.destination import DestinationSampler
from mobility.cars import holiday_rules
from mobility.cars.week_pattern import (
    build_leisure_pool_index,
    build_library_index,
    sample_person_week,
)
from mobility.core.data_structures import DailySchedule, ParkingEvent, Trip
from mobility.core.seasonal import get_seasonal_factor

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# NTS purpose label -> station label used by station_matcher
# ---------------------------------------------------------------------------
PURPOSE_TO_STATION_LABEL = {
    "home": "home",  # home charging (handled separately)
    "work": "work",
    "education": "education",
    "shopping": "shopping",
    "personal_business": "personal_business",
    "social": "social",
    "leisure": "leisure",
    "holiday": "holiday",
    "other": "leisure",  # fallback
}

# Pre-built tuple form of a chain for fast schedule construction.
# Each element: (departure, arrival, distance_km, purpose_from, purpose_to)
ChainTuple = List[Tuple[float, float, float, str, str]]


def build_trip_chain_pools(
    trips_df: pd.DataFrame,
) -> Dict[str, List[ChainTuple]]:
    """Group NTS trips into person-day chains, split by weekday/weekend."""
    pools: Dict[str, List[ChainTuple]] = {"weekday": [], "weekend": []}

    df = trips_df.sort_values(["IndividualID", "DayID", "JourSeq"])

    ind = df["IndividualID"].values
    day = df["DayID"].values
    dep = df["departure_time"].values
    arr = df["arrival_time"].values
    dist = df["distance_km"].values
    pfrom = df["purpose_from"].values
    pto = df["purpose_to"].values
    dtype = df["day_type"].values

    n = len(df)
    if n == 0:
        return pools

    chain: ChainTuple = []
    prev_ind, prev_day = ind[0], day[0]
    cur_dtype = dtype[0]

    for i in range(n):
        if ind[i] != prev_ind or day[i] != prev_day:
            if chain:
                pools[cur_dtype].append(chain)
            chain = []
            prev_ind, prev_day = ind[i], day[i]
            cur_dtype = dtype[i]

        chain.append(
            (
                float(dep[i]),
                float(arr[i]),
                float(dist[i]),
                PURPOSE_TO_STATION_LABEL.get(pfrom[i], "other"),
                PURPOSE_TO_STATION_LABEL.get(pto[i], "other"),
            )
        )

    if chain:
        pools[cur_dtype].append(chain)

    return pools


def _add_time_jitter(
    value: float,
    jitter_minutes: float = 10.0,
    *,
    rng: Optional[np.random.Generator] = None,
) -> float:
    """Add uniform random jitter in minutes to a decimal-hour time."""
    local_rng = rng if rng is not None else np.random.default_rng()
    delta = float(local_rng.uniform(-jitter_minutes, jitter_minutes)) / 60.0
    return max(0.0, min(23.75, value + delta))


def chain_to_daily_schedule(
    chain: ChainTuple,
    ev_id: str,
    day: int,
    day_type: str,
    consumption_kwh_per_km: float,
    jitter_minutes: float = 10.0,
    *,
    home_lsoa: str = "",
    start_lsoa: str = "",
    sampler: Optional[DestinationSampler] = None,
    rng: Optional[np.random.Generator] = None,
) -> DailySchedule:
    """Convert one ChainTuple into a DailySchedule.

    Day-start LSOA selection (home-detection branching)
    ---------------------------------------------------
    ``start_lsoa`` carries the previous day's overnight LSOA, threaded by
    ``assign_year_schedules`` / ``assign_chains_to_fleet``. It is only used
    when the NTS chain itself declares a non-home origin for today's first
    trip (``chain[0].purpose_from != "home"``), i.e. NTS recorded a true
    overnight stay away from home (~6.7% of day boundaries empirically).

    When NTS declares ``purpose_from == "home"`` for today's first trip,
    we treat that as the source of truth — even if the previous day ended
    away from home. This preserves NTS's "silent return" semantics for the
    ~3% of day boundaries where the diary implicitly assumes the person
    returned home overnight without recording the return trip. Forcing
    physical continuity here would contradict the NTS field.

    ``start_lsoa`` is also ignored when empty (day 0, or previous day had
    no parking/trips), falling back to ``home_lsoa``.
    """
    schedule = DailySchedule(ev_id=ev_id, day=day, day_type=day_type)
    local_rng = rng if rng is not None else np.random.default_rng()
    use_layer1 = sampler is not None and home_lsoa != ""
    nts_declares_home_start = bool(chain) and chain[0][3] == "home"
    day_start_lsoa = home_lsoa if nts_declares_home_start or not start_lsoa else start_lsoa
    current_lsoa = day_start_lsoa

    for dep_t, arr_t, dist_km, p_from, p_to in chain:
        dep = _add_time_jitter(dep_t, jitter_minutes, rng=local_rng)
        arr = _add_time_jitter(arr_t, jitter_minutes, rng=local_rng)
        if arr < dep:
            arr = dep + 0.05

        trip_kwargs = {
            "trip_id": f"{ev_id}_d{day}_{len(schedule.trips)}",
            "departure_time": dep,
            "arrival_time": arr,
            "origin_purpose": p_from,
            "destination_purpose": p_to,
        }

        if not use_layer1:
            trip = Trip(
                distance_km=dist_km,
                energy_consumed_kwh=dist_km * consumption_kwh_per_km,
                **trip_kwargs,
            )
        else:
            next_lsoa = (
                home_lsoa
                if p_to == "home"
                else sampler.sample_destination_lsoa(
                    current_lsoa,
                    p_to,
                    local_rng,
                    home_lsoa,
                )
            )
            sampled_distance_km = sampler.distance_km(current_lsoa, next_lsoa)
            trip_distance_km, fallback_distance, fallback_reason = _resolve_trip_distance_km(
                origin_lsoa=current_lsoa,
                destination_lsoa=next_lsoa,
                sampled_distance_km=sampled_distance_km,
                nts_distance_km=dist_km,
                duration_h=max(arr - dep, 1e-3),
            )
            if fallback_distance:
                logger.debug(
                    "Layer-1 fallback for ev_id=%s day=%s trip=%s origin_lsoa=%s "
                    "destination_lsoa=%s purpose=%s nts_distance_km=%.3f "
                    "sampled_distance_km=%.3f reason=%s",
                    ev_id,
                    day,
                    len(schedule.trips),
                    current_lsoa,
                    next_lsoa,
                    p_to,
                    dist_km,
                    sampled_distance_km,
                    fallback_reason,
                )

            trip = Trip(
                distance_km=trip_distance_km,
                energy_consumed_kwh=trip_distance_km * consumption_kwh_per_km,
                origin_lsoa=current_lsoa,
                destination_lsoa=next_lsoa,
                distance_km_nts=dist_km,
                fallback_distance=fallback_distance,
                **trip_kwargs,
            )
            current_lsoa = next_lsoa

        schedule.trips.append(trip)

    schedule.trips.sort(key=lambda trip: trip.departure_time)
    for i in range(1, len(schedule.trips)):
        prev = schedule.trips[i - 1]
        curr = schedule.trips[i]
        if curr.departure_time < prev.arrival_time:
            curr.departure_time = prev.arrival_time
            if curr.arrival_time < curr.departure_time:
                curr.arrival_time = curr.departure_time + 0.05

    _generate_parking_events(schedule, start_lsoa=day_start_lsoa if use_layer1 else "")
    return schedule


def _first_monday_of_year(year: int) -> dt.date:
    """Return the Monday starting the calendar year's first schedule week."""
    jan_1 = dt.date(year, 1, 1)
    return jan_1 - dt.timedelta(days=jan_1.weekday())


def _coerce_positive_float(value: object) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(numeric) or numeric <= 0.0:
        return None
    return numeric


def _resolve_ev_consumption_kwh_per_km(
    ev_row: pd.Series,
    *,
    consumption_kwh_per_km_default: float,
) -> float:
    battery_kwh = _coerce_positive_float(ev_row.get("battery_capacity_kwh"))
    consumption_kwh_per_km = _coerce_positive_float(ev_row.get("consumption_kwh_per_km"))

    if consumption_kwh_per_km is not None:
        return float(consumption_kwh_per_km)
    if battery_kwh is not None:
        return float(battery_kwh / 250.0)
    return float(consumption_kwh_per_km_default)


def assign_year_schedules(
    person_fleet: pd.DataFrame,
    ev_fleet: pd.DataFrame,
    library_df: pd.DataFrame,
    *,
    year: int,
    n_weeks: int = 52,
    sampler: Optional[DestinationSampler] = None,
    consumption_kwh_per_km_default: float = 0.18,
    jitter_minutes: float = 10.0,
    rng: np.random.Generator,
    progress_interval: int = 0,
    region: str = "england",
    apply_seasonal_correction: bool = True,
    profile_callback: Optional[Callable[[dict], None]] = None,
    library_index_cache=None,
    leisure_pool_index_cache=None,
) -> Dict[str, List[DailySchedule]]:
    """Assign person-linked weekly patterns across a calendar year.

    When ``apply_seasonal_correction`` is True (default), each trip's
    ``energy_consumed_kwh`` is multiplied by SEASONAL_CONSUMPTION_FACTOR[season]
    where season is derived from ``sched.date.month`` via MONTH_TO_SEASON. Set
    to False to produce outputs bit-identical to the pre-Stage-5 pipeline.
    """
    required_person_cols = {"ev_id", "person_id", "nts_household_id", "nts_region"}
    missing_person_cols = sorted(required_person_cols.difference(person_fleet.columns))
    if missing_person_cols:
        raise ValueError(f"person_fleet missing required columns: {missing_person_cols}")

    if "EV_ID" not in ev_fleet.columns:
        raise ValueError("ev_fleet must include EV_ID")
    if n_weeks < 1:
        raise ValueError("n_weeks must be at least 1")

    library_index = library_index_cache if library_index_cache is not None else build_library_index(library_df)
    leisure_pool_index = (
        leisure_pool_index_cache
        if leisure_pool_index_cache is not None
        else build_leisure_pool_index(library_df, library_index=library_index)
    )
    monday_of_year_1 = _first_monday_of_year(year)

    ev_runtime = ev_fleet.copy()
    ev_runtime["EV_ID"] = ev_runtime["EV_ID"].fillna("").astype(str)
    if ev_runtime["EV_ID"].duplicated().any():
        raise ValueError("ev_fleet contains duplicate EV_ID values")
    ev_runtime["resolved_home_lsoa"] = _resolve_home_lsoa_values(ev_runtime)
    ev_lookup = ev_runtime.set_index("EV_ID", drop=False)

    fleet_rows = person_fleet.copy()
    fleet_rows["ev_id"] = fleet_rows["ev_id"].fillna("").astype(str)
    fleet_rows["person_id"] = fleet_rows["person_id"].fillna("").astype(str)

    fleet_schedules: Dict[str, List[DailySchedule]] = {}
    total = len(fleet_rows)

    for i, row in enumerate(fleet_rows.itertuples(index=False), start=1):
        vehicle_profile_start = time.perf_counter()
        ev_id = str(row.ev_id)
        person_id = str(row.person_id)
        if ev_id not in ev_lookup.index:
            raise KeyError(f"ev_id not found in ev_fleet: {ev_id}")
        if person_id not in library_index:
            raise KeyError(f"person_id not found in person_week_library: {person_id}")

        ev_row = ev_lookup.loc[ev_id]
        consumption_kwh_per_km = _resolve_ev_consumption_kwh_per_km(
            ev_row,
            consumption_kwh_per_km_default=consumption_kwh_per_km_default,
        )
        home_lsoa = str(ev_row["resolved_home_lsoa"])

        daily_schedules: List[DailySchedule] = []
        prev_overnight_lsoa = ""
        for week_idx in range(n_weeks):
            week_start = monday_of_year_1 + dt.timedelta(days=7 * week_idx)
            is_holiday_week = holiday_rules.is_holiday_week(week_start, region)
            chains_7 = sample_person_week(
                person_id,
                week_start,
                library_index,
                leisure_pool_index,
                rng,
                is_holiday_week=is_holiday_week,
            )

            for day_of_week, chain in enumerate(chains_7):
                date_d = week_start + dt.timedelta(days=day_of_week)
                day_idx = (week_idx * 7) + day_of_week
                day_type = "weekend" if day_of_week >= 5 else "weekday"
                sched = chain_to_daily_schedule(
                    chain,
                    ev_id,
                    day_idx,
                    day_type,
                    consumption_kwh_per_km=consumption_kwh_per_km,
                    jitter_minutes=jitter_minutes,
                    home_lsoa=home_lsoa,
                    start_lsoa=prev_overnight_lsoa,
                    sampler=sampler,
                    rng=rng,
                )
                sched.date = date_d
                if apply_seasonal_correction:
                    factor = get_seasonal_factor(date_d.month)
                    if factor != 1.0:
                        for trip in sched.trips:
                            trip.energy_consumed_kwh *= factor
                daily_schedules.append(sched)

                overnight = next(
                    (event for event in reversed(sched.parking_events) if event.end_time >= 24.0),
                    None,
                )
                if overnight is not None:
                    prev_overnight_lsoa = overnight.location_lsoa
                elif sched.trips:
                    prev_overnight_lsoa = sched.trips[-1].destination_lsoa

        _smooth_cross_day_parking(daily_schedules)
        fleet_schedules[ev_id] = daily_schedules

        if profile_callback is not None:
            profile_callback(
                {
                    "year": year,
                    "region": region,
                    "vehicle_index": i,
                    "vehicle_count": total,
                    "ev_id": ev_id,
                    "person_id": person_id,
                    "home_lsoa": home_lsoa,
                    "schedule_days": len(daily_schedules),
                    "trip_count": sum(len(schedule.trips) for schedule in daily_schedules),
                    "parking_event_count": sum(
                        len(schedule.parking_events) for schedule in daily_schedules
                    ),
                    "elapsed_seconds": time.perf_counter() - vehicle_profile_start,
                }
            )

        if progress_interval > 0 and (i % progress_interval == 0 or i == total):
            print(f"  Assigned year schedules: {i:,}/{total:,} EVs", flush=True)

    return fleet_schedules


def _generate_parking_events(
    schedule: DailySchedule,
    *,
    start_lsoa: str = "",
) -> None:
    """Fill parking_events for a DailySchedule based on its trips."""
    trips = schedule.trips
    if not trips:
        return

    first_trip = trips[0]
    if first_trip.departure_time > 0:
        schedule.parking_events.append(
            ParkingEvent(
                start_time=0.0,
                end_time=first_trip.departure_time,
                duration_hours=first_trip.departure_time,
                location_purpose=first_trip.origin_purpose,
                location_lsoa=start_lsoa,
            )
        )

    for i in range(len(trips) - 1):
        park_start = trips[i].arrival_time
        park_end = trips[i + 1].departure_time
        if park_end <= park_start:
            continue
        schedule.parking_events.append(
            ParkingEvent(
                start_time=park_start,
                end_time=park_end,
                duration_hours=park_end - park_start,
                location_purpose=trips[i].destination_purpose,
                location_lsoa=trips[i].destination_lsoa,
            )
        )

    last_trip = trips[-1]
    if last_trip.arrival_time < 24.0:
        schedule.parking_events.append(
            ParkingEvent(
                start_time=last_trip.arrival_time,
                end_time=24.0,
                duration_hours=24.0 - last_trip.arrival_time,
                location_purpose=last_trip.destination_purpose,
                location_lsoa=last_trip.destination_lsoa,
            )
        )


def assign_chains_to_fleet(
    ev_fleet: pd.DataFrame,
    pools: Dict[str, List[ChainTuple]],
    num_days: int = 7,
    consumption_kwh_per_km_default: float = 0.18,
    jitter_minutes: float = 10.0,
    seed: Optional[int] = 42,
    progress_interval: int = 0,
    *,
    sampler: Optional[DestinationSampler] = None,
    rng: Optional[np.random.Generator] = None,
) -> Dict[str, List[DailySchedule]]:
    """Assign NTS trip chains to each EV for `num_days` of simulation."""
    warnings.warn(
        "assign_chains_to_fleet is deprecated; use assign_year_schedules",
        DeprecationWarning,
        stacklevel=2,
    )
    local_rng = rng if rng is not None else np.random.default_rng(seed)

    weekday_chains = pools["weekday"]
    weekend_chains = pools["weekend"]
    if not weekday_chains or not weekend_chains:
        raise ValueError("Trip chain pools are empty. Check NTS data loading.")

    day_types = ["weekend" if (d % 7) >= 5 else "weekday" for d in range(num_days)]

    n_evs = len(ev_fleet)
    wd_indices = local_rng.integers(0, len(weekday_chains), size=n_evs * num_days)
    we_indices = local_rng.integers(0, len(weekend_chains), size=n_evs * num_days)

    ev_ids = ev_fleet["EV_ID"].values
    batteries = ev_fleet["battery_capacity_kwh"].values
    if "consumption_kwh_per_km" in ev_fleet.columns:
        consumptions = ev_fleet["consumption_kwh_per_km"].values
    else:
        consumptions = np.full(n_evs, np.nan)
    home_lsoas = _resolve_home_lsoa_values(ev_fleet)

    fleet_schedules: Dict[str, List[DailySchedule]] = {}
    total = n_evs

    for i in range(n_evs):
        ev_id = ev_ids[i]
        battery = batteries[i]
        if np.isnan(battery) or battery <= 0:
            battery = 60.0
        c = consumptions[i]
        if np.isnan(c) or c <= 0:
            consumption = battery / 250.0 if battery > 0 else consumption_kwh_per_km_default
        else:
            consumption = float(c)

        daily_schedules: List[DailySchedule] = []
        prev_overnight_lsoa = ""
        for day_idx in range(num_days):
            dt = day_types[day_idx]
            if dt == "weekend":
                chain = weekend_chains[int(we_indices[i * num_days + day_idx])]
            else:
                chain = weekday_chains[int(wd_indices[i * num_days + day_idx])]

            schedule = chain_to_daily_schedule(
                chain,
                ev_id,
                day_idx,
                dt,
                consumption_kwh_per_km=consumption,
                jitter_minutes=jitter_minutes,
                home_lsoa=str(home_lsoas[i]),
                start_lsoa=prev_overnight_lsoa,
                sampler=sampler,
                rng=local_rng,
            )
            daily_schedules.append(schedule)

            overnight = next(
                (event for event in reversed(schedule.parking_events) if event.end_time >= 24.0),
                None,
            )
            if overnight is not None:
                prev_overnight_lsoa = overnight.location_lsoa
            elif schedule.trips:
                prev_overnight_lsoa = schedule.trips[-1].destination_lsoa

        _smooth_cross_day_parking(daily_schedules)
        fleet_schedules[ev_id] = daily_schedules

        if progress_interval > 0 and ((i + 1) % progress_interval == 0 or i + 1 == total):
            print(f"  Assigned chains: {i + 1:,}/{total:,} EVs", flush=True)

    return fleet_schedules


def _resolve_trip_distance_km(
    *,
    origin_lsoa: str,
    destination_lsoa: str,
    sampled_distance_km: float,
    nts_distance_km: float,
    duration_h: float,
) -> tuple[float, bool, str]:
    """Choose between Layer-1 OD distance and the original NTS distance."""
    if origin_lsoa == destination_lsoa and nts_distance_km > 0.5:
        return float(nts_distance_km), True, "same_lsoa_long_nts_distance"

    speed_kmh = sampled_distance_km / duration_h
    if speed_kmh > 130.0:
        return float(nts_distance_km), True, "sampled_speed_above_130_kmh"
    if speed_kmh < 2.0 and nts_distance_km > 1.0:
        return float(nts_distance_km), True, "sampled_speed_below_2_kmh"

    relative_gap = abs(sampled_distance_km - nts_distance_km) / max(nts_distance_km, 1.0)
    if relative_gap > 5.0:
        return float(nts_distance_km), True, "relative_gap_above_5x"

    return float(sampled_distance_km), False, ""


def _resolve_home_lsoa_values(ev_fleet: pd.DataFrame) -> np.ndarray:
    """Resolve one home LSOA per EV, warning once on `LSOA_code` fallback."""
    if "home_lsoa" in ev_fleet.columns:
        home_lsoas = ev_fleet["home_lsoa"].fillna("").astype(str).to_numpy(dtype=object)
        if "LSOA_code" in ev_fleet.columns:
            fallback = ev_fleet["LSOA_code"].fillna("").astype(str).to_numpy(dtype=object)
            missing_mask = np.equal(home_lsoas, "")
            home_lsoas[missing_mask] = fallback[missing_mask]
        return home_lsoas

    if "LSOA_code" in ev_fleet.columns:
        warnings.warn(
            "ev_fleet has no home_lsoa column; falling back to LSOA_code",
            RuntimeWarning,
            stacklevel=2,
        )
        return ev_fleet["LSOA_code"].fillna("").astype(str).to_numpy(dtype=object)

    return np.full(len(ev_fleet), "", dtype=object)


def _smooth_cross_day_parking(daily_schedules: List[DailySchedule]) -> None:
    """Ensure overnight parking location is consistent across day boundaries."""
    for d in range(1, len(daily_schedules)):
        prev = daily_schedules[d - 1]
        curr = daily_schedules[d]

        if not curr.trips:
            continue

        overnight_event = next(
            (event for event in reversed(prev.parking_events) if event.end_time >= 24.0),
            None,
        )
        if overnight_event is None:
            continue

        overnight_event.location_purpose = curr.trips[0].origin_purpose
        overnight_event.location_lsoa = curr.trips[0].origin_lsoa

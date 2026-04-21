"""Build trip-chain pools from NTS data and assign them to EVs."""

import random
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from mobility.core.data_structures import DailySchedule, ParkingEvent, Trip

# ---------------------------------------------------------------------------
# NTS purpose label  →  station label used by station_matcher
# ---------------------------------------------------------------------------
PURPOSE_TO_STATION_LABEL = {
    "home":              "home",         # home charging (handled separately)
    "work":              "work",
    "education":         "education",
    "shopping":          "shopping",
    "personal_business": "personal_business",
    "social":            "social",
    "leisure":           "leisure",
    "holiday":           "holiday",
    "other":             "leisure",       # fallback
}

# Pre-built tuple form of a chain for fast schedule construction.
# Each element: (departure, arrival, distance_km, purpose_from, purpose_to)
ChainTuple = List[Tuple[float, float, float, str, str]]


def build_trip_chain_pools(
    trips_df: pd.DataFrame,
) -> Dict[str, List[ChainTuple]]:
    """Group NTS trips into person-day chains, split by weekday/weekend.

    Returns dict with keys "weekday" and "weekend", each mapping to a list
    of ChainTuples (lightweight tuples instead of DataFrames for speed).
    """
    pools: Dict[str, List[ChainTuple]] = {"weekday": [], "weekend": []}

    # Sort once globally
    df = trips_df.sort_values(["IndividualID", "DayID", "JourSeq"])

    # Extract columns as numpy arrays for fast iteration
    ind = df["IndividualID"].values
    day = df["DayID"].values
    dep = df["departure_time"].values
    arr = df["arrival_time"].values
    dist = df["distance_km"].values
    pfrom = df["purpose_from"].values
    pto = df["purpose_to"].values
    dtype = df["day_type"].values

    # Group by (IndividualID, DayID) using change detection
    n = len(df)
    if n == 0:
        return pools

    chain: ChainTuple = []
    prev_ind, prev_day = ind[0], day[0]
    cur_dtype = dtype[0]

    for i in range(n):
        if ind[i] != prev_ind or day[i] != prev_day:
            # Flush previous chain
            if chain:
                pools[cur_dtype].append(chain)
            chain = []
            prev_ind, prev_day = ind[i], day[i]
            cur_dtype = dtype[i]

        chain.append((
            float(dep[i]), float(arr[i]), float(dist[i]),
            PURPOSE_TO_STATION_LABEL.get(pfrom[i], "other"),
            PURPOSE_TO_STATION_LABEL.get(pto[i], "other"),
        ))

    # Flush last chain
    if chain:
        pools[cur_dtype].append(chain)

    return pools


def _add_time_jitter(value: float, jitter_minutes: float = 10.0) -> float:
    """Add uniform random jitter (±jitter_minutes) to a decimal-hour time,
    clamped to [0, 23.75]."""
    delta = random.uniform(-jitter_minutes, jitter_minutes) / 60.0
    return max(0.0, min(23.75, value + delta))


def chain_to_daily_schedule(
    chain: ChainTuple,
    ev_id: str,
    day: int,
    day_type: str,
    consumption_kwh_per_km: float,
    jitter_minutes: float = 10.0,
) -> DailySchedule:
    """Convert one ChainTuple into a DailySchedule.

    Applies ±jitter_minutes random perturbation to departure/arrival times.
    Generates parking events between consecutive trips and before the first /
    after the last trip.
    """
    schedule = DailySchedule(ev_id=ev_id, day=day, day_type=day_type)

    for dep_t, arr_t, dist_km, p_from, p_to in chain:
        dep = _add_time_jitter(dep_t, jitter_minutes)
        arr = _add_time_jitter(arr_t, jitter_minutes)
        if arr < dep:
            arr = dep + 0.05

        trip = Trip(
            trip_id=f"{ev_id}_d{day}_{len(schedule.trips)}",
            departure_time=dep,
            arrival_time=arr,
            distance_km=dist_km,
            origin_purpose=p_from,
            destination_purpose=p_to,
            energy_consumed_kwh=dist_km * consumption_kwh_per_km,
        )
        schedule.trips.append(trip)

    # Sort trips by departure time and fix any overlaps introduced by jitter
    schedule.trips.sort(key=lambda t: t.departure_time)
    for i in range(1, len(schedule.trips)):
        prev = schedule.trips[i - 1]
        curr = schedule.trips[i]
        if curr.departure_time < prev.arrival_time:
            curr.departure_time = prev.arrival_time
            if curr.arrival_time < curr.departure_time:
                curr.arrival_time = curr.departure_time + 0.05

    # Generate parking events between trips
    _generate_parking_events(schedule)

    return schedule


def _generate_parking_events(schedule: DailySchedule) -> None:
    """Fill parking_events for a DailySchedule based on its trips."""
    trips = schedule.trips
    if not trips:
        return

    first_trip = trips[0]
    if first_trip.departure_time > 0:
        schedule.parking_events.append(ParkingEvent(
            start_time=0.0,
            end_time=first_trip.departure_time,
            duration_hours=first_trip.departure_time,
            location_purpose=first_trip.origin_purpose,
        ))

    for i in range(len(trips) - 1):
        park_start = trips[i].arrival_time
        park_end = trips[i + 1].departure_time
        if park_end <= park_start:
            continue
        schedule.parking_events.append(ParkingEvent(
            start_time=park_start,
            end_time=park_end,
            duration_hours=park_end - park_start,
            location_purpose=trips[i].destination_purpose,
        ))

    last_trip = trips[-1]
    if last_trip.arrival_time < 24.0:
        schedule.parking_events.append(ParkingEvent(
            start_time=last_trip.arrival_time,
            end_time=24.0,
            duration_hours=24.0 - last_trip.arrival_time,
            location_purpose=last_trip.destination_purpose,
        ))


def assign_chains_to_fleet(
    ev_fleet: pd.DataFrame,
    pools: Dict[str, List[ChainTuple]],
    num_days: int = 7,
    consumption_kwh_per_km_default: float = 0.18,
    jitter_minutes: float = 10.0,
    seed: Optional[int] = 42,
    progress_interval: int = 0,
) -> Dict[str, List[DailySchedule]]:
    """Assign NTS trip chains to each EV for num_days of simulation.

    Parameters
    ----------
    ev_fleet : DataFrame with EV_ID, battery_capacity_kwh, ac_power_kw etc.
    pools : output of build_trip_chain_pools (ChainTuple format)
    num_days : number of days to simulate (default 7 = one week)
    consumption_kwh_per_km_default : fallback if not derivable
    jitter_minutes : time perturbation in minutes
    seed : random seed for reproducibility
    progress_interval : print progress every N EVs (0 = silent)

    Returns
    -------
    dict mapping ev_id → list of DailySchedule (one per day)
    """
    if seed is not None:
        random.seed(seed)
        np.random.seed(seed)

    weekday_chains = pools["weekday"]
    weekend_chains = pools["weekend"]
    if not weekday_chains or not weekend_chains:
        raise ValueError("Trip chain pools are empty. Check NTS data loading.")

    # Pre-compute which days are weekend
    day_types = ["weekend" if (d % 7) >= 5 else "weekday" for d in range(num_days)]

    # Pre-select random chain indices for all EVs × all days at once
    n_evs = len(ev_fleet)
    wd_indices = np.random.randint(0, len(weekday_chains), size=n_evs * num_days)
    we_indices = np.random.randint(0, len(weekend_chains), size=n_evs * num_days)

    # Extract fleet arrays to avoid iterrows
    ev_ids = ev_fleet["EV_ID"].values
    batteries = ev_fleet["battery_capacity_kwh"].values
    # Per-EV consumption from EV_UK_LSOA_2025_with_energy.csv (kWh/km).
    # Falls back to battery/250 if column is missing or value is NaN.
    if "consumption_kwh_per_km" in ev_fleet.columns:
        consumptions = ev_fleet["consumption_kwh_per_km"].values
    else:
        consumptions = np.full(n_evs, np.nan)

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
        for day_idx in range(num_days):
            dt = day_types[day_idx]
            if dt == "weekend":
                chain = weekend_chains[we_indices[i * num_days + day_idx]]
            else:
                chain = weekday_chains[wd_indices[i * num_days + day_idx]]

            sched = chain_to_daily_schedule(
                chain, ev_id, day_idx, dt,
                consumption_kwh_per_km=consumption,
                jitter_minutes=jitter_minutes,
            )
            daily_schedules.append(sched)

        _smooth_cross_day_parking(daily_schedules)
        fleet_schedules[ev_id] = daily_schedules

        if progress_interval > 0 and ((i + 1) % progress_interval == 0 or i + 1 == total):
            print(f"  Assigned chains: {i+1:,}/{total:,} EVs", flush=True)

    return fleet_schedules


def _smooth_cross_day_parking(daily_schedules: List[DailySchedule]) -> None:
    """Ensure overnight parking location is consistent across day boundaries.

    For each pair of consecutive days, sets day d+1's first parking event
    location to match day d's last parking event location.  This avoids
    the "teleportation" artefact where the EV jumps to a different location
    at midnight.  Also updates the first trip's origin_purpose to match.
    """
    for d in range(1, len(daily_schedules)):
        prev = daily_schedules[d - 1]
        curr = daily_schedules[d]

        # Determine overnight location from previous day
        if prev.parking_events:
            overnight_loc = prev.parking_events[-1].location_purpose
        elif prev.trips:
            overnight_loc = prev.trips[-1].destination_purpose
        else:
            continue

        # Fix current day's first parking event (the one starting at 0:00)
        if curr.parking_events and curr.parking_events[0].start_time == 0.0:
            curr.parking_events[0].location_purpose = overnight_loc

        # Fix first trip's origin to stay consistent
        if curr.trips:
            curr.trips[0].origin_purpose = overnight_loc

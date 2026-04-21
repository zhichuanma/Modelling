"""Bus-specific adapter around mobility.core.simulator.

Each block from all_blocks.parquet becomes a single-day DailySchedule:
- trips sorted by start_h; energy = distance_km * consumption_kwh_per_km
- parking events before first trip and after last trip = depot time (can_charge)
- mid-day layovers are parking events but cannot charge (opportunity charging off)
"""

from __future__ import annotations

from typing import Dict, Tuple

import numpy as np
import pandas as pd

from mobility.core.data_structures import DailySchedule, ParkingEvent, Trip
from mobility.core.simulator import STEP_HOURS, STEPS_PER_DAY, simulate_single_day


# Typical single-deck urban e-bus defaults (BYD ADL Enviro200EV class)
DEFAULT_BATTERY_KWH = 300.0
DEFAULT_CONSUMPTION_KWH_PER_KM = 1.2
DEFAULT_DEPOT_CHARGE_KW = 100.0  # DC depot charger


def block_to_daily_schedule(
    block_df: pd.DataFrame,
    ev_id: str,
    consumption_kwh_per_km: float = DEFAULT_CONSUMPTION_KWH_PER_KM,
    depot_charge_kw: float = DEFAULT_DEPOT_CHARGE_KW,
    allow_layover_charging: bool = False,
    layover_charge_kw: float = 0.0,
) -> DailySchedule:
    """Build a one-day DailySchedule for a single bus (one block_id).

    Trips with start_h >= 24 or end_h >= 24 are dropped (overnight wrap).
    """
    df = block_df.sort_values("start_h").copy()
    df = df[(df["start_h"] < 24.0) & (df["end_h"] < 24.0)]
    if df.empty:
        return DailySchedule(ev_id=ev_id, day=0, day_type="weekday")

    sched = DailySchedule(ev_id=ev_id, day=0, day_type="weekday")
    for _, r in df.iterrows():
        dep = float(r["start_h"])
        arr = float(r["end_h"])
        if arr <= dep:
            arr = dep + 0.05
        sched.trips.append(Trip(
            trip_id=str(r["trip_id"]),
            departure_time=dep,
            arrival_time=arr,
            distance_km=float(r["distance_km"]),
            origin_purpose="depot",
            destination_purpose="depot",
            energy_consumed_kwh=float(r["distance_km"]) * consumption_kwh_per_km,
        ))

    # Fix any overlap introduced by sorting/coercion
    for i in range(1, len(sched.trips)):
        prev, curr = sched.trips[i - 1], sched.trips[i]
        if curr.departure_time < prev.arrival_time:
            curr.departure_time = prev.arrival_time
            if curr.arrival_time < curr.departure_time:
                curr.arrival_time = curr.departure_time + 0.05

    first = sched.trips[0]
    last = sched.trips[-1]

    # Depot parking: before the first trip
    if first.departure_time > 0:
        sched.parking_events.append(ParkingEvent(
            start_time=0.0,
            end_time=first.departure_time,
            duration_hours=first.departure_time,
            location_purpose="depot",
            can_charge=True,
            charge_power_kw=depot_charge_kw,
        ))

    # Mid-day layovers between trips
    for i in range(len(sched.trips) - 1):
        a = sched.trips[i].arrival_time
        b = sched.trips[i + 1].departure_time
        if b <= a:
            continue
        sched.parking_events.append(ParkingEvent(
            start_time=a,
            end_time=b,
            duration_hours=b - a,
            location_purpose="layover",
            can_charge=allow_layover_charging,
            charge_power_kw=layover_charge_kw if allow_layover_charging else 0.0,
        ))

    # Depot parking: after the last trip
    if last.arrival_time < 24.0:
        sched.parking_events.append(ParkingEvent(
            start_time=last.arrival_time,
            end_time=24.0,
            duration_hours=24.0 - last.arrival_time,
            location_purpose="depot",
            can_charge=True,
            charge_power_kw=depot_charge_kw,
        ))

    return sched


def simulate_bus_fleet(
    all_blocks: pd.DataFrame,
    battery_kwh: float = DEFAULT_BATTERY_KWH,
    consumption_kwh_per_km: float = DEFAULT_CONSUMPTION_KWH_PER_KM,
    depot_charge_kw: float = DEFAULT_DEPOT_CHARGE_KW,
    allow_layover_charging: bool = False,
    layover_charge_kw: float = 0.0,
    soc_init: float = 1.0,
    progress_interval: int = 0,
) -> Tuple[pd.DataFrame, np.ndarray]:
    """Run single-day SOC simulation over every block_id in all_blocks.

    Returns:
      per_bus : DataFrame indexed by block_id with km/day, energy/day, soc_end
      fleet_load : ndarray shape (96,) — aggregated charging power (kW) in each 15-min step
    """
    groups = all_blocks.groupby("block_id", sort=False)
    total = len(groups)
    records = []
    fleet_load = np.zeros(STEPS_PER_DAY)

    for i, (bid, g) in enumerate(groups, 1):
        sched = block_to_daily_schedule(
            g, ev_id=str(bid),
            consumption_kwh_per_km=consumption_kwh_per_km,
            depot_charge_kw=depot_charge_kw,
            allow_layover_charging=allow_layover_charging,
            layover_charge_kw=layover_charge_kw,
        )
        if not sched.trips:
            continue
        soc, load, soc_end = simulate_single_day(
            sched, battery_capacity_kwh=battery_kwh, soc_start=soc_init
        )
        fleet_load += load
        total_km = sum(t.distance_km for t in sched.trips)
        energy_demand = total_km * consumption_kwh_per_km
        energy_charged = float(load.sum() * STEP_HOURS)
        records.append({
            "block_id": bid,
            "agency_id": g["agency_id"].iloc[0],
            "block_source": g["block_source"].iloc[0],
            "n_trips": len(sched.trips),
            "total_km": total_km,
            "energy_demand_kwh": energy_demand,
            "energy_charged_kwh": energy_charged,
            "soc_end": soc_end,
        })
        if progress_interval and (i % progress_interval == 0 or i == total):
            print(f"  {i:>7,}/{total:,} blocks", flush=True)

    per_bus = pd.DataFrame.from_records(records).set_index("block_id")
    return per_bus, fleet_load


def load_profile_times() -> np.ndarray:
    """Return the 15-min step start times in decimal hours (0.0, 0.25, ..., 23.75)."""
    return np.arange(STEPS_PER_DAY) * STEP_HOURS

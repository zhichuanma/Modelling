"""Match parking events to charging stations (or home chargers).

Matching logic (layered):
  1. Home parking  → always has home charger (7 kW default), no public station
  2. Work parking  → find station with label='work' in the EV's LSOA
  3. Other parking → find station with matching label in the EV's LSOA
  4. No match      → cannot charge

Effective charge power = min(station rated power, EV onboard charger power).
"""

import warnings
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

from mobility.core.constants import HOME_CHARGER_KW
from mobility.core.data_structures import DailySchedule, ParkingEvent


# Lightweight index: (lsoa, label) → (station_id, capacity_kw)
StationIndex = Dict[Tuple[str, str], Tuple[int, float]]


def _build_station_index(stations_df: pd.DataFrame) -> StationIndex:
    """Build a flat index: (lsoa_code, label) → (best_station_id, capacity_kw).

    For each (lsoa, label) pair, keeps only the station with highest capacity.
    """
    valid = stations_df.dropna(subset=["lsoa_code"])
    # Sort so that last row per group is the max capacity
    valid = valid.sort_values("TotalCapacity_kW")
    # Keep last (highest capacity) per (lsoa_code, label)
    best = valid.drop_duplicates(subset=["lsoa_code", "label"], keep="last")

    # Build dict from arrays — no iterrows
    lsoas = best["lsoa_code"].values
    labels = best["label"].values
    sids = best["StationID"].values.astype(int)
    caps = best["TotalCapacity_kW"].values.astype(float)

    idx: StationIndex = {
        (lsoas[i], labels[i]): (int(sids[i]), float(caps[i]))
        for i in range(len(best))
    }
    return idx


def match_stations_for_schedule(
    schedule: DailySchedule,
    ev_lsoa: str,
    ev_ac_power_kw: float,
    home_charger_kw: float,  # DEPRECATED: ignored since Stage 6; home power is driven by constants.HOME_CHARGER_KW
    station_index: StationIndex,
) -> None:
    """Annotate each ParkingEvent in the schedule with charging info.

    Modifies parking events in-place.

    Notes
    -----
    The `home_charger_kw` parameter is kept for backward compatibility but is
    ignored; home parking events are always charged at
    ``constants.HOME_CHARGER_KW`` (Stage 6). This parameter will be removed
    in a future stage once all call sites are migrated.
    """
    if home_charger_kw != HOME_CHARGER_KW:
        warnings.warn(
            f"home_charger_kw={home_charger_kw!r} is ignored since Stage 6; "
            f"using constants.HOME_CHARGER_KW={HOME_CHARGER_KW} instead.",
            DeprecationWarning,
            stacklevel=2,
        )

    for pe in schedule.parking_events:
        if pe.location_purpose == "home":
            pe.can_charge = True
            pe.matched_station_id = None
            pe.charge_power_kw = HOME_CHARGER_KW
            continue

        hit = station_index.get((ev_lsoa, pe.location_purpose))
        if hit is not None:
            pe.can_charge = True
            pe.matched_station_id = hit[0]
            pe.charge_power_kw = min(hit[1], ev_ac_power_kw)
        else:
            pe.can_charge = False
            pe.matched_station_id = None
            pe.charge_power_kw = 0.0


def match_stations_for_fleet(
    fleet_schedules: Dict[str, list],
    ev_fleet: pd.DataFrame,
    stations_df: pd.DataFrame,
    home_charger_kw: float = 7.0,  # DEPRECATED: ignored since Stage 6; home power is driven by constants.HOME_CHARGER_KW
) -> None:
    """Match charging stations for all EVs across all days.

    Modifies fleet_schedules in-place.

    Notes
    -----
    The `home_charger_kw` parameter is kept for backward compatibility but is
    ignored; home parking events are always charged at
    ``constants.HOME_CHARGER_KW`` (Stage 6). This parameter will be removed
    in a future stage once all call sites are migrated.
    """
    if home_charger_kw != HOME_CHARGER_KW:
        warnings.warn(
            f"home_charger_kw={home_charger_kw!r} is ignored since Stage 6; "
            f"using constants.HOME_CHARGER_KW={HOME_CHARGER_KW} instead.",
            DeprecationWarning,
            stacklevel=2,
        )

    station_index = _build_station_index(stations_df)

    # Build quick EV lookup — plain dict for speed
    ev_lsoa_map = dict(zip(ev_fleet["EV_ID"], ev_fleet["LSOA_code"]))
    ev_ac_map = dict(zip(ev_fleet["EV_ID"], ev_fleet["ac_power_kw"]))

    for ev_id, daily_schedules in fleet_schedules.items():
        ev_lsoa = ev_lsoa_map.get(ev_id, "")
        ev_ac = ev_ac_map.get(ev_id, 7.0)
        if ev_ac is None or (isinstance(ev_ac, float) and np.isnan(ev_ac)):
            ev_ac = 7.0

        for schedule in daily_schedules:
            match_stations_for_schedule(
                schedule, ev_lsoa, ev_ac, home_charger_kw, station_index
            )

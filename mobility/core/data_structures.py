"""Core data classes for the EV mobility simulation.

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

import datetime as dt
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class EVSpec:
    """Specification of a single electric vehicle."""

    ev_id: str                      # e.g. "cars_1"
    lsoa: str                       # LSOA code where the EV is registered
    model: str                      # e.g. "TESLA MODEL 3"
    battery_capacity_kwh: float     # e.g. 75.0
    consumption_kwh_per_km: float   # derived: battery_capacity_kwh / assumed_range_km
    max_onboard_charger_kw: float   # AC_Power_kW from data
    home_charger_kw: float = 7.0    # default home wallbox
    chemistry: str = "NMC"


@dataclass
class Trip:
    """A single trip leg within a daily schedule."""

    trip_id: str
    departure_time: float           # hour of day (decimal, e.g. 8.5 = 08:30)
    arrival_time: float             # hour of day (decimal)
    distance_km: float
    origin_purpose: str             # mapped label, e.g. "home", "work"
    destination_purpose: str        # mapped label
    energy_consumed_kwh: float = 0.0  # filled by simulator
    origin_lsoa: str = ""
    destination_lsoa: str = ""
    distance_km_nts: float = 0.0
    fallback_distance: bool = False
    is_deadhead: bool = False
    deadhead_class: str = ""


@dataclass
class ParkingEvent:
    """A parking/charging session between trips."""

    start_time: float               # hour of day (decimal)
    end_time: float                 # hour of day (decimal)
    duration_hours: float
    location_purpose: str           # e.g. "home", "work", "shopping"
    location_lsoa: str = ""
    soc_on_arrival: float = 0.0     # 0-1 fraction
    can_charge: bool = False
    matched_station_id: Optional[int] = None
    charge_power_kw: float = 0.0
    energy_charged_kwh: float = 0.0
    soc_on_departure: float = 0.0   # 0-1 fraction
    soc_min_required: float = 0.0   # 0-1 fraction


@dataclass
class DailySchedule:
    """A full day's travel and parking schedule for one EV."""

    ev_id: str
    day: int                        # simulation day index (0-based)
    day_type: str                   # "weekday" or "weekend"
    trips: List[Trip] = field(default_factory=list)
    parking_events: List[ParkingEvent] = field(default_factory=list)
    date: Optional[dt.date] = None

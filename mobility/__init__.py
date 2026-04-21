"""mobility - unified EV charging simulation package.

Sub-packages:
    core  - vehicle-agnostic data structures, SOC simulator, analyzer
    cars  - NTS-based passenger car pipeline
    bus   - GTFS-based bus pipeline
    coach - TransXChange-based coach pipeline

For convenience, the most commonly used passenger-car + core symbols are
re-exported at the top level so notebooks can do `import mobility as em`.

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

from .core import (
    DailySchedule,
    EVSpec,
    ParkingEvent,
    Trip,
    STEP_HOURS,
    STEPS_PER_DAY,
    simulate_fleet,
    simulate_single_day,
    simulate_single_ev,
    aggregate_load_profile,
    average_daily_load_profile,
    compute_statistics,
    export_results,
    fleet_summary,
    single_ev_soc_series,
)
from .cars import (
    NTS_PURPOSE_MAP,
    PURPOSE_TO_STATION_LABEL,
    assign_chains_to_fleet,
    build_trip_chain_pools,
    chain_to_daily_schedule,
    load_all,
    load_ev_fleet,
    load_nts_trips,
    load_stations,
    match_stations_for_fleet,
)

__all__ = [
    "EVSpec",
    "Trip",
    "ParkingEvent",
    "DailySchedule",
    "STEPS_PER_DAY",
    "STEP_HOURS",
    "simulate_single_day",
    "simulate_single_ev",
    "simulate_fleet",
    "aggregate_load_profile",
    "average_daily_load_profile",
    "compute_statistics",
    "fleet_summary",
    "single_ev_soc_series",
    "export_results",
    "NTS_PURPOSE_MAP",
    "load_nts_trips",
    "load_ev_fleet",
    "load_stations",
    "load_all",
    "PURPOSE_TO_STATION_LABEL",
    "build_trip_chain_pools",
    "chain_to_daily_schedule",
    "assign_chains_to_fleet",
    "match_stations_for_fleet",
]

"""mobility.core — vehicle-agnostic charging physics and data structures."""

from .data_structures import DailySchedule, EVSpec, ParkingEvent, Trip
from .simulator import (
    STEP_HOURS,
    STEPS_PER_DAY,
    simulate_fleet,
    simulate_single_day,
    simulate_single_ev,
)
from .analyzer import (
    aggregate_load_profile,
    average_daily_load_profile,
    compute_statistics,
    export_results,
    fleet_summary,
    single_ev_soc_series,
)

__all__ = [
    "EVSpec", "Trip", "ParkingEvent", "DailySchedule",
    "STEPS_PER_DAY", "STEP_HOURS",
    "simulate_single_day", "simulate_single_ev", "simulate_fleet",
    "aggregate_load_profile", "average_daily_load_profile",
    "compute_statistics", "fleet_summary", "single_ev_soc_series",
    "export_results",
]

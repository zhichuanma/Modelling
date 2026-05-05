"""mobility.coach — TransXChange-based coach schedule pipeline."""

from .coach_fleet import load_coach_fleet, sample_coach_ev
from .data_loader import (
    load_all_coach_journeys,
    load_all_coach_stop_sequences,
    summarize_journey_quality,
)
from .distance import haversine_km, vehicle_journey_distance_km
from .feasibility import journey_feasibility
from .selection import (
    render_journey_identity_card,
    sample_contrast_journey,
    sample_protagonist_journey,
)
from .sim_adapter import simulate_coach_journey
from .stop_geometry import load_unified_stops
from .trip_chain_coach import journey_to_daily_schedules

__all__ = [
    "load_unified_stops",
    "vehicle_journey_distance_km",
    "haversine_km",
    "load_coach_fleet",
    "sample_coach_ev",
    "journey_feasibility",
    "journey_to_daily_schedules",
    "simulate_coach_journey",
    "sample_protagonist_journey",
    "sample_contrast_journey",
    "render_journey_identity_card",
    "load_all_coach_journeys",
    "load_all_coach_stop_sequences",
    "summarize_journey_quality",
]

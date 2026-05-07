"""Public bus modelling surface."""

from .annual_simulation import (
    annual_load_matrix_to_frame,
    simulate_block_year,
    simulate_fleet_year,
    write_annual_results,
)
from .calendar import (
    FEED_YEAR_END,
    FEED_YEAR_START,
    active_dates_for_service,
    build_service_date_index,
    load_service_calendar,
)
from .data_loader import attach_lsoa, filter_to_clean_blocks, load_all_blocks, summarize_block_quality
from .feasibility import block_preflight, scan_block_infeasibility, shadow_soc_walk
from .selection import render_block_identity_card, sample_contrast_block, sample_protagonist_block
from .sim_adapter import simulate_block, simulate_fleet_blocks
from .vehicle_sampling import load_bus_vehicle_params, sample_bus_vehicle_specs
from .year_schedule import block_to_year_schedules

__all__ = [
    "FEED_YEAR_START",
    "FEED_YEAR_END",
    "load_service_calendar",
    "active_dates_for_service",
    "build_service_date_index",
    "load_all_blocks",
    "attach_lsoa",
    "summarize_block_quality",
    "filter_to_clean_blocks",
    "shadow_soc_walk",
    "block_preflight",
    "scan_block_infeasibility",
    "simulate_block",
    "simulate_fleet_blocks",
    "block_to_year_schedules",
    "simulate_block_year",
    "simulate_fleet_year",
    "annual_load_matrix_to_frame",
    "write_annual_results",
    "sample_protagonist_block",
    "sample_contrast_block",
    "render_block_identity_card",
    "load_bus_vehicle_params",
    "sample_bus_vehicle_specs",
]

"""Public bus modelling surface."""

from .annual_simulation import (
    annual_load_matrix_to_frame,
    simulate_block_year,
    simulate_fleet_year,
    write_annual_results,
)
from .block_instances import build_block_instances
from .calendar import (
    FEED_YEAR_END,
    FEED_YEAR_START,
    active_dates_for_service,
    build_service_date_index,
    load_service_calendar,
)
from .charger_registry import build_charger_registry
from .chain_resolver import build_resolution_summary, query_charger_eligibility, resolve_chain
from .chain_soc import chain_soc_walk
from .data_loader import attach_lsoa, filter_to_clean_blocks, load_all_blocks, summarize_block_quality
from .depot_registry import build_depot_registry
from .event_ledger import build_event_ledger
from .feasibility import block_preflight, scan_block_infeasibility, shadow_soc_walk
from .selection import render_block_identity_card, sample_contrast_block, sample_protagonist_block
from .sim_adapter import simulate_block, simulate_fleet_blocks
from .txc_parser import parse_txc_garages
from .vehicle_assignment import assign_vehicles_greedy
from .vehicle_inventory import bridge_ev_lsoa_to_fleet, load_ev_lsoa_inventory
from .vehicle_sampling import load_bus_vehicle_params, sample_bus_vehicle_specs
from .year_schedule import block_to_year_schedules

__all__ = [
    "FEED_YEAR_START",
    "FEED_YEAR_END",
    "load_service_calendar",
    "active_dates_for_service",
    "build_service_date_index",
    "parse_txc_garages",
    "build_depot_registry",
    "bridge_ev_lsoa_to_fleet",
    "load_ev_lsoa_inventory",
    "build_charger_registry",
    "build_block_instances",
    "assign_vehicles_greedy",
    "build_event_ledger",
    "chain_soc_walk",
    "query_charger_eligibility",
    "resolve_chain",
    "build_resolution_summary",
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

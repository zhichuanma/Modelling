"""Run-summary helpers for annual depot-load pipeline outputs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


REQUIRED_LIMITATIONS = [
    "This model only allows depot charging; public charging and opportunity charging are not modelled.",
    "Inferred operational depot anchors are not verified physical garage locations.",
    "For non-closed blocks where depot=end_lsoa, deadhead direction and distance may be biased.",
    "This version does not model multi-day SOC carry-over. Each vehicle-day starts at usable_soc_max.",
    "ev_stock_scale represents a representative annual duty assignment at current EV stock scale, not full UK bus electrification.",
    "If trip-level layovers are unavailable, midday depot charging windows may be missed.",
    "matched_vehicle_day_feasible_share is conditional on matching: its denominator is matched vehicle-days only; unmatched sampled blocks never enter the SOC/load stages. Read it together with matched_sample_share and the unmatched reason counts.",
    "Depots without a resolvable LSOA (opdepot_*_missing) have unknown coordinates: their deadhead is recorded as 0 km (incomplete) and their charging load is reported under unknown-depot load isolation; it must not be mapped spatially.",
]

# Plan v2 §10.1: under carryover, the no-multi-day line is replaced by these.
CARRYOVER_LIMITATIONS = [
    "Models multi-day SOC carry-over using per-vehicle ledger stitching at a default operational day boundary (soc_day_boundary_hour).",
    "SOC continuity is subject to the assumed home-depot assignment and the idle-vehicle charging policy.",
    "Assignment feasibility uses a conservative pre-block energy screen; matched vehicle-days that end SOC-infeasible in the exact walk are retained and counted (n_matched_but_walk_infeasible).",
    "Headline summary metrics exclude the warm-up service dates (is_warmup=True rows stay in all output tables).",
]

_DAILY_RESET_LIMITATION = "This version does not model multi-day SOC carry-over. Each vehicle-day starts at usable_soc_max."


def limitations_for_soc_mode(soc_mode: str) -> list[str]:
    if str(soc_mode) != "carryover":
        return list(REQUIRED_LIMITATIONS)
    out = [item for item in REQUIRED_LIMITATIONS if item != _DAILY_RESET_LIMITATION]
    return out + list(CARRYOVER_LIMITATIONS)


def build_run_summary_markdown(
    *,
    preflight_summary: dict[str, Any],
    block_templates: pd.DataFrame,
    block_instances: pd.DataFrame,
    depot_registry: pd.DataFrame,
    ev_bus_specs: pd.DataFrame,
    vehicle_day_assignments: pd.DataFrame,
    assignment_diagnostics: pd.DataFrame | None = None,
    vehicle_day_soc_summary: pd.DataFrame,
    depot_load_15min: pd.DataFrame,
    depot_daily_summary: pd.DataFrame,
    feed_year_start: str,
    feed_year_end: str,
    scenario_mode: str,
    bus_trip_records: pd.DataFrame | None = None,
    bus_charging_events: pd.DataFrame | None = None,
    bus_ev_state_records: pd.DataFrame | None = None,
    preaggregated_stats: Mapping[str, Any] | None = None,
    soc_mode: str = "daily_reset",
) -> str:
    stats = preaggregated_stats or {}
    total_charge = _sum(depot_load_15min, "charge_kwh")
    total_energy = float(stats.get("total_energy_kwh", _sum(vehicle_day_soc_summary, "total_energy_kwh")))
    total_deadhead = float(stats.get("total_deadhead_km", _sum(vehicle_day_soc_summary, "total_deadhead_km")))
    if "matched_vehicle_day_feasible_share" in stats:
        feasible_share = float(stats["matched_vehicle_day_feasible_share"])
    elif not vehicle_day_soc_summary.empty and "depot_only_feasible" in vehicle_day_soc_summary.columns:
        feasible_share = float(vehicle_day_soc_summary["depot_only_feasible"].astype(bool).mean())
    else:
        feasible_share = np.nan
    lines = [
        "# Bus annual depot-only load run summary",
        "",
        "## Key counts",
        f"- n_trip_rows_input: {preflight_summary.get('n_trip_rows', 0)}",
        f"- n_block_templates: {len(block_templates)}",
        f"- n_block_instances_annual: {len(block_instances)}",
        f"- n_ev_specs_valid: {len(ev_bus_specs)}",
        f"- scenario_mode: {scenario_mode}",
        f"- soc_mode: {soc_mode}",
        f"- feed_year_start: {feed_year_start}",
        f"- feed_year_end: {feed_year_end}",
        f"- assignment_method: {_assignment_method(vehicle_day_assignments)}",
        f"- n_vehicle_day_assignments: {len(vehicle_day_assignments)}",
        f"- n_unassigned_block_instances_under_ev_stock_scale: {_assignment_unassigned_total(vehicle_day_assignments, assignment_diagnostics)}",
        f"- mean_daily_assignment_coverage_share: {_format_float(_assignment_mean_coverage(vehicle_day_assignments, assignment_diagnostics))}",
        *_matched_share_lines(assignment_diagnostics),
        *_unmatched_reason_lines(assignment_diagnostics),
        f"- n_bus_trip_records: {_count_from_stats(stats, 'n_bus_trip_records', bus_trip_records)}",
        f"- n_bus_charging_events: {_count_from_stats(stats, 'n_bus_charging_events', bus_charging_events)}",
        f"- n_bus_ev_state_records: {_count_from_stats(stats, 'n_bus_ev_state_records', bus_ev_state_records)}",
        f"- n_depots: {len(depot_registry)}",
        f"- n_physical_depots: {_count_true(depot_registry, 'is_physical_depot')}",
        f"- n_operational_anchors: {_count_true(depot_registry, 'is_operational_anchor')}",
        f"- total_charge_kwh: {total_charge:.3f}",
        f"- total_energy_kwh: {total_energy:.3f}",
        f"- total_deadhead_km: {total_deadhead:.3f}",
        f"- matched_vehicle_day_feasible_share: {_format_float(feasible_share)}",
        f"- lsoa_attach_success_rate: {_format_float(_lsoa_attach_success(block_templates))}",
        f"- minibus_count: {preflight_summary.get('minibus_row_count', 0)}",
        f"- sanity_filter_drop_count: {preflight_summary.get('n_ev_specs_dropped_by_sanity', 0)}",
        "",
        "## Unknown-depot load isolation",
        *_unknown_depot_lines(depot_load_15min, vehicle_day_assignments),
        *_carryover_section_lines(soc_mode, stats),
        *_calendar_decay_lines(stats),
        "",
        "## Depot confidence distribution",
    ]
    lines.extend(_value_counts_lines(depot_registry, "depot_confidence"))
    lines.extend(["", "## Top 10 depots by annual charge kWh"])
    lines.extend(_top_depots_by_charge(depot_load_15min))
    lines.extend(["", "## Top 10 depots by peak kW"])
    lines.extend(_top_depots_by_peak(depot_load_15min))
    lines.extend(["", "## Top 10 blocks by energy shortfall"])
    lines.extend(_top_blocks_by_shortfall_from_stats(stats) or _top_blocks_by_shortfall(vehicle_day_soc_summary))
    lines.extend(["", "## Region distribution of assigned block instances"])
    lines.extend(_value_counts_lines(vehicle_day_assignments, "region_key"))
    lines.extend(["", "## Main modelling assumptions"])
    lines.extend(
        [
            "- Charging mode is depot_only.",
            "- Effective depot charging power is min(vehicle.ac_charge_kw_max, depot_power_kw).",
            "- EV inventory rows are treated as EV bus instances or parameter donors; count is audit-only and is not expanded.",
            "- Default scenario load is unweighted ev_stock_scale representative annual load.",
            "- Bus trip, charging, and 15-minute EV state records are exported using private-car-aligned observability fields.",
        ]
    )
    lines.extend(["", "## Main limitations"])
    lines.extend(f"- {item}" for item in limitations_for_soc_mode(soc_mode))
    lines.append("")
    return "\n".join(lines)


def _carryover_section_lines(soc_mode: str, stats: Mapping[str, Any]) -> list[str]:
    """Carry-over configuration & observability (plan v2 §9 run-summary fields)."""
    if str(soc_mode) != "carryover":
        return []
    lines = ["", "## Carry-over configuration"]
    for key in (
        "soc_day_boundary_hour",
        "warmup_days",
        "warmup_start_date",
        "warmup_end_date",
        "idle_vehicle_charging_policy",
    ):
        if key in stats:
            lines.append(f"- {key}: {stats[key]}")
    for key in (
        "n_idle_charging_events",
        "idle_charge_kwh",
        "idle_charge_share",
        "n_temporal_overlap_exclusions",
        "n_matched_but_walk_infeasible",
        "n_pre_block_windows_carryover",
        "pre_block_charge_kwh",
        "overnight_truncation_kwh",
        "total_charge_kwh_excl_warmup",
        "total_energy_kwh_excl_warmup",
        "matched_vehicle_day_feasible_share_excl_warmup",
    ):
        if key in stats:
            value = stats[key]
            lines.append(f"- {key}: {_format_float(value) if isinstance(value, float) else value}")
    lines.append("- note: headline metrics above include warm-up days; the *_excl_warmup restatements are the §14.5 reporting basis.")
    return lines


def _calendar_decay_lines(stats: Mapping[str, Any]) -> list[str]:
    """GTFS feed-tail coverage caveat (plan v2 §15): flag suspiciously thin dates."""
    if "calendar_decay_suspect_dates" not in stats:
        return []
    suspect = list(stats.get("calendar_decay_suspect_dates") or [])
    lines = ["", "## Calendar-coverage caveat"]
    if "calendar_decay_median_active_blocks" in stats:
        lines.append(f"- median_active_block_instances_per_date: {stats['calendar_decay_median_active_blocks']}")
    if "calendar_decay_floor" in stats:
        lines.append(f"- coverage_floor (flag below): {stats['calendar_decay_floor']}")
    if suspect:
        lines.append(f"- n_calendar_decay_suspect_dates: {len(suspect)}")
        lines.append(f"- calendar_decay_suspect_dates: {', '.join(str(item) for item in suspect[:20])}{' ...' if len(suspect) > 20 else ''}")
        lines.append("- caveat: loads on flagged dates reflect GTFS calendar expiry, not real seasonality; do not read them as demand.")
    else:
        lines.append("- none within the run window (feed-tail decay handled by the run-window truncation).")
    return lines


def write_run_summary(markdown: str, out_dir: str | Path) -> Path:
    path = Path(out_dir) / "run_summary.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    return path


def _sum(frame: pd.DataFrame, column: str) -> float:
    if frame.empty or column not in frame.columns:
        return 0.0
    return float(pd.to_numeric(frame[column], errors="coerce").fillna(0.0).sum())


def _len_or_zero(frame: pd.DataFrame | None) -> int:
    return 0 if frame is None else int(len(frame))


def _count_from_stats(stats: Mapping[str, Any], key: str, frame: pd.DataFrame | None) -> int:
    if key in stats:
        return int(stats[key])
    return _len_or_zero(frame)


def _count_true(frame: pd.DataFrame, column: str) -> int:
    if frame.empty or column not in frame.columns:
        return 0
    return int(frame[column].fillna(False).astype(bool).sum())


def _format_float(value: float) -> str:
    return "nan" if not np.isfinite(float(value)) else f"{float(value):.4f}"


def _lsoa_attach_success(block_templates: pd.DataFrame) -> float:
    if block_templates.empty or "start_lsoa" not in block_templates.columns or "end_lsoa" not in block_templates.columns:
        return np.nan
    success = block_templates["start_lsoa"].astype(str).str.strip().ne("") & block_templates["end_lsoa"].astype(str).str.strip().ne("")
    return float(success.mean())


_DAILY_DIAGNOSTIC_COLUMNS = {
    "service_date",
    "n_available_block_instances_for_service_date",
    "n_assigned_block_instances_for_service_date",
    "n_unassigned_block_instances_for_service_date",
    "daily_assignment_coverage_share",
}


def _daily_assignment_diagnostics(assignments: pd.DataFrame, diagnostics: pd.DataFrame | None = None) -> pd.DataFrame:
    """Daily assignment diagnostics, preferring the dedicated diagnostics table.

    Falls back to the legacy denormalized columns on the assignments table.
    """
    if diagnostics is not None and not diagnostics.empty and _DAILY_DIAGNOSTIC_COLUMNS.issubset(diagnostics.columns):
        return diagnostics.loc[:, sorted(_DAILY_DIAGNOSTIC_COLUMNS)].drop_duplicates("service_date")
    if assignments.empty or not _DAILY_DIAGNOSTIC_COLUMNS.issubset(assignments.columns):
        return pd.DataFrame()
    return assignments.loc[:, sorted(_DAILY_DIAGNOSTIC_COLUMNS)].drop_duplicates("service_date")


def _assignment_unassigned_total(assignments: pd.DataFrame, diagnostics: pd.DataFrame | None = None) -> int:
    daily = _daily_assignment_diagnostics(assignments, diagnostics)
    if daily.empty:
        return 0
    return int(pd.to_numeric(daily["n_unassigned_block_instances_for_service_date"], errors="coerce").fillna(0).sum())


def _assignment_mean_coverage(assignments: pd.DataFrame, diagnostics: pd.DataFrame | None = None) -> float:
    daily = _daily_assignment_diagnostics(assignments, diagnostics)
    if daily.empty:
        return np.nan
    return float(pd.to_numeric(daily["daily_assignment_coverage_share"], errors="coerce").mean())


def _assignment_method(assignments: pd.DataFrame) -> str:
    if assignments.empty or "assignment_method" not in assignments.columns:
        return "none"
    methods = assignments["assignment_method"].dropna().astype(str).unique()
    return methods[0] if len(methods) == 1 else ",".join(sorted(methods))


def _diagnostics_sum(diagnostics: pd.DataFrame, column: str) -> float:
    return float(pd.to_numeric(diagnostics[column], errors="coerce").fillna(0).sum())


def _matched_share_lines(diagnostics: pd.DataFrame | None) -> list[str]:
    """Overall matched shares over sampled / active blocks, the honest companions
    to matched_vehicle_day_feasible_share (whose denominator is matched days only)."""
    if diagnostics is None or diagnostics.empty:
        return []
    lines: list[str] = []
    matched_col = "n_matched_feasible_block_instances_for_service_date"
    if matched_col not in diagnostics.columns:
        return []
    matched = _diagnostics_sum(diagnostics, matched_col)
    for denom_col, label in (
        ("n_sampled_block_instances_for_service_date", "matched_sample_share"),
        ("n_active_block_instances_for_service_date", "matched_active_block_share"),
    ):
        if denom_col in diagnostics.columns:
            denom = _diagnostics_sum(diagnostics, denom_col)
            share = matched / denom if denom else np.nan
            lines.append(f"- {label}: {_format_float(share)}")
    return lines


def _unmatched_reason_lines(diagnostics: pd.DataFrame | None) -> list[str]:
    """Unmatched-reason counts. The two top-level buckets (no_feasible_vehicle,
    lost_matching_competition) sum to total unmatched; in constrained mode the
    radius reasons are a sub-decomposition of no_feasible_vehicle, NOT an
    additional bucket — printed indented to avoid double-counting."""
    if diagnostics is None or diagnostics.empty:
        return []
    lines: list[str] = []
    if "n_unmatched_no_feasible_vehicle" in diagnostics.columns:
        n_no_feasible = int(_diagnostics_sum(diagnostics, "n_unmatched_no_feasible_vehicle"))
        lines.append(f"- n_unmatched_sampled_blocks_no_feasible_vehicle: {n_no_feasible}")
        radius_parts = [
            (column, int(_diagnostics_sum(diagnostics, column)))
            for column in (
                "n_unmatched_no_vehicle_in_radius",
                "n_unmatched_no_feasible_vehicle_in_radius",
                "n_unmatched_vehicle_busy_overnight",
            )
            if column in diagnostics.columns
        ]
        if any(count for _, count in radius_parts):
            for column, count in radius_parts:
                lines.append(f"  - of which {column.removeprefix('n_unmatched_')}: {count}")
    if "n_unmatched_lost_matching_competition" in diagnostics.columns:
        total = int(_diagnostics_sum(diagnostics, "n_unmatched_lost_matching_competition"))
        lines.append(f"- n_unmatched_sampled_blocks_lost_matching_competition: {total}")
    return lines


def _unknown_depot_mask(frame: pd.DataFrame) -> pd.Series:
    mask = pd.Series(False, index=frame.index)
    if "depot_id" in frame.columns:
        mask |= frame["depot_id"].astype(str).str.endswith("_missing")
    if "depot_lsoa" in frame.columns:
        lsoa = frame["depot_lsoa"].fillna("").astype(str).str.strip().str.lower()
        mask |= lsoa.isin(("", "missing"))
    return mask


def _unknown_depot_lines(depot_load_15min: pd.DataFrame, assignments: pd.DataFrame) -> list[str]:
    """Isolate load at depots with no resolvable LSOA/coordinates (opdepot_*_missing).

    Their deadhead is recorded as 0 km (incomplete), so their load is optimistic
    and must not be mapped spatially.
    """
    lines: list[str] = []
    if not depot_load_15min.empty and "charge_kwh" in depot_load_15min.columns:
        unknown = _unknown_depot_mask(depot_load_15min)
        total = _sum(depot_load_15min, "charge_kwh")
        unknown_kwh = float(pd.to_numeric(depot_load_15min.loc[unknown, "charge_kwh"], errors="coerce").fillna(0.0).sum())
        share = unknown_kwh / total if total else np.nan
        n_unknown = int(depot_load_15min.loc[unknown, "depot_id"].nunique()) if "depot_id" in depot_load_15min.columns else 0
        lines.append(f"- unknown_depot_charge_kwh: {unknown_kwh:.3f}")
        lines.append(f"- unknown_depot_charge_share: {_format_float(share)}")
        lines.append(f"- n_unknown_depots_with_load: {n_unknown}")
    if not assignments.empty and "deadhead_estimate_incomplete" in assignments.columns:
        incomplete = assignments["deadhead_estimate_incomplete"].fillna(False).astype(bool)
        lines.append(f"- n_vehicle_days_deadhead_incomplete: {int(incomplete.sum())}")
        lines.append(f"- share_vehicle_days_deadhead_incomplete: {_format_float(float(incomplete.mean()))}")
    return lines or ["- none: 0"]


def _value_counts_lines(frame: pd.DataFrame, column: str) -> list[str]:
    if frame.empty or column not in frame.columns:
        return ["- none: 0"]
    return [f"- {key}: {int(value)}" for key, value in frame[column].fillna("missing").astype(str).value_counts().items()]


def _top_depots_by_charge(load: pd.DataFrame) -> list[str]:
    if load.empty:
        return ["- none"]
    grouped = load.groupby("depot_id", as_index=False)["charge_kwh"].sum().sort_values("charge_kwh", ascending=False).head(10)
    return [f"- {row.depot_id}: {row.charge_kwh:.3f}" for row in grouped.itertuples(index=False)]


def _top_depots_by_peak(load: pd.DataFrame) -> list[str]:
    if load.empty:
        return ["- none"]
    grouped = load.groupby("depot_id", as_index=False)["average_kw"].max().sort_values("average_kw", ascending=False).head(10)
    return [f"- {row.depot_id}: {row.average_kw:.3f}" for row in grouped.itertuples(index=False)]


def _top_blocks_by_shortfall(summary: pd.DataFrame) -> list[str]:
    if summary.empty or "energy_shortfall_kwh" not in summary.columns:
        return ["- none"]
    top = summary.sort_values("energy_shortfall_kwh", ascending=False).head(10)
    return [f"- {row.block_instance_id}: {float(row.energy_shortfall_kwh):.3f}" for row in top.itertuples(index=False)]


def _top_blocks_by_shortfall_from_stats(stats: Mapping[str, Any]) -> list[str] | None:
    rows = stats.get("top_blocks_by_shortfall")
    if not rows:
        return None
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return None
    out: list[str] = []
    for row in rows[:10]:
        if isinstance(row, Mapping):
            block_id = row.get("block_instance_id", "missing")
            shortfall = row.get("energy_shortfall_kwh", 0.0)
        else:
            try:
                block_id, shortfall = row
            except (TypeError, ValueError):
                continue
        out.append(f"- {block_id}: {float(shortfall):.3f}")
    return out or ["- none"]


__all__ = [
    "CARRYOVER_LIMITATIONS",
    "REQUIRED_LIMITATIONS",
    "build_run_summary_markdown",
    "limitations_for_soc_mode",
    "write_run_summary",
]

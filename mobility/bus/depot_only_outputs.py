"""Stage 7 depot-load aggregation and run summary outputs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .depot_only_events import DEPOT_CHARGING_EVENT_TYPES


SLOT_MINUTES = 15
WEIGHTING_MODE = "unweighted_ev_stock_scenario"


def aggregate_depot_load_15min(events: pd.DataFrame) -> pd.DataFrame:
    """Aggregate only depot charging events into true 15-minute slots."""
    if events.empty:
        return pd.DataFrame(columns=_load_columns())
    charge_events = events[
        events["event_type"].isin(DEPOT_CHARGING_EVENT_TYPES)
        & pd.to_numeric(events["charge_kwh_added"], errors="coerce").fillna(0.0).gt(0.0)
    ].copy()
    records: list[dict[str, Any]] = []
    for row in charge_events.itertuples(index=False):
        records.extend(_split_event_to_slots(row))
    if not records:
        return pd.DataFrame(columns=_load_columns())
    slots = pd.DataFrame.from_records(records)
    grouped = (
        slots.groupby(
            [
                "depot_id",
                "operational_depot_lsoa",
                "region_key",
                "time_slot",
                "slot_start",
                "slot_end",
                "sample_mode",
                "weighting_mode",
            ],
            as_index=False,
            sort=True,
        )
        .agg(charge_kwh=("charge_kwh", "sum"), n_active_cases=("simulation_case_id", "nunique"))
    )
    grouped["average_kw"] = grouped["charge_kwh"] / (SLOT_MINUTES / 60.0)
    return grouped.loc[:, _load_columns()].reset_index(drop=True)


def depot_load_energy_matches_events(
    depot_load_15min: pd.DataFrame,
    events: pd.DataFrame,
    *,
    atol: float = 1e-6,
    rtol: float = 1e-9,
) -> bool:
    load_energy = float(pd.to_numeric(depot_load_15min.get("charge_kwh", pd.Series(dtype=float)), errors="coerce").fillna(0.0).sum())
    event_energy = float(
        pd.to_numeric(
            events.loc[events["event_type"].isin(DEPOT_CHARGING_EVENT_TYPES), "charge_kwh_added"]
            if not events.empty and "event_type" in events.columns
            else pd.Series(dtype=float),
            errors="coerce",
        )
        .fillna(0.0)
        .sum()
    )
    return bool(abs(load_energy - event_energy) <= float(atol) + float(rtol) * abs(event_energy))


def build_run_summary(
    *,
    preflight_summary: dict[str, Any],
    sampled_blocks: pd.DataFrame,
    block_sample_diagnostics: pd.DataFrame,
    operational_depot_registry: pd.DataFrame,
    simulation_cases: pd.DataFrame,
    vehicle_day_events: pd.DataFrame,
    case_soc_summary: pd.DataFrame,
    depot_load_15min: pd.DataFrame,
    sample_mode: str,
    weighting_mode: str = WEIGHTING_MODE,
) -> str:
    total_charge = float(pd.to_numeric(depot_load_15min.get("charge_kwh", pd.Series(dtype=float)), errors="coerce").fillna(0.0).sum())
    feasible_share = (
        float(case_soc_summary["depot_only_feasible"].astype(bool).mean())
        if not case_soc_summary.empty and "depot_only_feasible" in case_soc_summary.columns
        else np.nan
    )
    dropped = max(0, int(preflight_summary.get("n_valid_ev_bus_instances", 0)) - int(len(simulation_cases)))
    lines = [
        "# Depot-only EV bus stock scenario",
        "",
        "This pipeline uses all valid bus/minibus vehicle instances in the EV inventory as the current EV bus stock. In full mode it samples the same number of representative bus block templates from national bus duties, pairs one EV bus instance with one sampled block, and charges only at the block-inferred operational depot LSOA anchor.",
        "",
        "The output estimates SOC, energy use, infeasibility, and 15-minute depot charging load for the current EV bus stock scale under a depot-only assumption. It does not infer real physical depots and it is not a UK all-bus electrification total.",
        "",
        "## Key counts",
        f"- n_trip_rows_raw: {preflight_summary.get('n_trip_rows_raw', 0)}",
        f"- n_block_templates_available: {preflight_summary.get('n_block_templates_available', 0)}",
        f"- n_blocks_sampled: {len(sampled_blocks)}",
        f"- n_ev_instances_available: {preflight_summary.get('n_valid_ev_bus_instances', 0)}",
        f"- n_simulation_cases_created: {len(simulation_cases)}",
        f"- n_cases_successful: {len(case_soc_summary)}",
        f"- n_cases_dropped: {dropped}",
        f"- sample_mode: {sample_mode}",
        f"- weighting_mode: {weighting_mode}",
        f"- n_depots_inferred: {int(operational_depot_registry['depot_id'].nunique()) if not operational_depot_registry.empty else 0}",
        f"- depot_only_feasible_share: {_fmt_float(feasible_share)}",
        f"- total_depot_charge_kwh: {_fmt_float(total_charge)}",
        f"- depot_load_energy_matches_event_ledger: {depot_load_energy_matches_events(depot_load_15min, vehicle_day_events)}",
        "",
        "## Depot confidence distribution",
    ]
    lines.extend(_value_counts_lines(operational_depot_registry, "depot_confidence"))
    lines.extend(["", "## Region sample distribution"])
    lines.extend(_value_counts_lines(sampled_blocks, "region_key"))
    lines.extend(["", "## Vehicle model distribution"])
    lines.extend(_value_counts_lines(simulation_cases, "vehicle_model", limit=20))
    lines.extend(["", "## Vehicle subtype distribution"])
    lines.extend(_value_counts_lines(simulation_cases, "vehicle_subtype"))
    lines.extend(["", "## Minibus count note", f"- {preflight_summary.get('minibus_count_note', '')}"])
    lines.extend(["", "## Top 10 depots by charge kWh"])
    lines.extend(_top_charge_lines(depot_load_15min))
    lines.extend(["", "## Top 10 blocks by energy shortfall"])
    lines.extend(_top_shortfall_lines(case_soc_summary))
    lines.extend(["", "## Main modelling assumptions"])
    lines.extend(
        [
            "- EV inventory rows with valid bus/minibus subtype and sane technical parameters are treated as vehicle instances.",
            "- Vehicle technical parameters donate battery, consumption, and AC charging limits; vehicle source_lsoa is audit-only.",
            "- Full mode samples block templates without replacement in proportion to observed duty strata.",
            "- Depot charging uses fixed depot power, default 100 kW, capped by each vehicle AC charging limit.",
            "- sample_weight is diagnostic-only; the main depot_load_15min output is unweighted.",
        ]
    )
    lines.extend(["", "## Main data quality issues"])
    lines.extend(
        [
            f"- low_consumption_filtered_count: {preflight_summary.get('low_consumption_filtered_count', 0)}",
            f"- invalid_battery_vehicle_count: {preflight_summary.get('invalid_battery_vehicle_count', 0)}",
            f"- EV_ID unique: {preflight_summary.get('ev_id_is_unique')}",
            f"- count matches (LSOA, Model) group size: {preflight_summary.get('count_matches_lsoa_model_group_size')}",
            f"- LSOA attach both-end hit rate: {_fmt_float((preflight_summary.get('lsoa_attach') or {}).get('both_endpoint_lsoa_hit_rate', np.nan))}",
        ]
    )
    if not block_sample_diagnostics.empty and "any_region_cap_breach" in block_sample_diagnostics.columns:
        lines.append(f"- region dominance guard breach: {bool(block_sample_diagnostics['any_region_cap_breach'].fillna(False).any())}")
    lines.extend(["", "## Main limitations"])
    lines.extend(
        [
            "- The depot is an operational charging anchor, not a real garage.",
            "- depot=end_lsoa fallback may understate return deadhead and create asymmetric morning/evening deadhead.",
            "- This is a single representative day model with no multi-day SOC warm-up or carry-over.",
            "- Initial SOC is set to the usable upper bound, so pre-service charging is usually a no-op.",
            "- Aggregate block mode can understate midday depot charging windows when trip-level terminal information is unavailable.",
            "- The default output is not a national all-bus electrification total.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_run_summary(path: str | Path, content: str) -> None:
    Path(path).write_text(content, encoding="utf-8")


def _split_event_to_slots(row: Any) -> list[dict[str, Any]]:
    start = pd.Timestamp(row.start_datetime)
    end = pd.Timestamp(getattr(row, "charging_end_datetime", pd.NaT))
    if pd.isna(end):
        end = pd.Timestamp(row.end_datetime)
    end = min(end, pd.Timestamp(row.end_datetime))
    charge = float(getattr(row, "charge_kwh_added", 0.0) or 0.0)
    if charge <= 0.0 or end <= start:
        return []
    duration_min = (end - start).total_seconds() / 60.0
    energy_per_min = charge / duration_min
    cursor = start.floor(f"{SLOT_MINUTES}min")
    records: list[dict[str, Any]] = []
    while cursor < end:
        slot_end = cursor + pd.Timedelta(minutes=SLOT_MINUTES)
        overlap_start = max(start, cursor)
        overlap_end = min(end, slot_end)
        overlap_min = max(0.0, (overlap_end - overlap_start).total_seconds() / 60.0)
        if overlap_min > 0.0:
            records.append(
                {
                    "depot_id": str(row.depot_id),
                    "operational_depot_lsoa": str(row.operational_depot_lsoa),
                    "region_key": str(getattr(row, "region_key", "unknown")),
                    "time_slot": cursor.isoformat(),
                    "slot_start": cursor,
                    "slot_end": slot_end,
                    "charge_kwh": overlap_min * energy_per_min,
                    "simulation_case_id": str(row.simulation_case_id),
                    "sample_mode": str(getattr(row, "sample_mode", "")),
                    "weighting_mode": str(getattr(row, "weighting_mode", WEIGHTING_MODE)),
                }
            )
        cursor = slot_end
    return records


def _load_columns() -> list[str]:
    return [
        "depot_id",
        "operational_depot_lsoa",
        "region_key",
        "time_slot",
        "slot_start",
        "slot_end",
        "charge_kwh",
        "average_kw",
        "n_active_cases",
        "sample_mode",
        "weighting_mode",
    ]


def _value_counts_lines(frame: pd.DataFrame, column: str, *, limit: int | None = None) -> list[str]:
    if frame.empty or column not in frame.columns:
        return ["- none"]
    counts = frame[column].fillna("missing").astype(str).value_counts()
    if limit is not None:
        counts = counts.head(limit)
    return [f"- {key}: {int(value)}" for key, value in counts.items()]


def _top_charge_lines(load: pd.DataFrame) -> list[str]:
    if load.empty:
        return ["- none"]
    top = load.groupby(["depot_id", "operational_depot_lsoa"], as_index=False)["charge_kwh"].sum()
    top = top.sort_values("charge_kwh", ascending=False, kind="stable").head(10)
    return [f"- {row.depot_id} ({row.operational_depot_lsoa}): {_fmt_float(row.charge_kwh)}" for row in top.itertuples(index=False)]


def _top_shortfall_lines(summary: pd.DataFrame) -> list[str]:
    if summary.empty or "energy_shortfall_kwh" not in summary.columns:
        return ["- none"]
    top = summary.sort_values("energy_shortfall_kwh", ascending=False, kind="stable").head(10)
    return [
        f"- {row.simulation_case_id} block={row.block_template_id} vehicle={row.vehicle_id}: {_fmt_float(row.energy_shortfall_kwh)} kWh"
        for row in top.itertuples(index=False)
        if float(row.energy_shortfall_kwh or 0.0) > 0.0
    ] or ["- none"]


def _fmt_float(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "nan"
    if not np.isfinite(number):
        return "nan"
    return f"{number:.6g}"


__all__ = [
    "SLOT_MINUTES",
    "WEIGHTING_MODE",
    "aggregate_depot_load_15min",
    "build_run_summary",
    "depot_load_energy_matches_events",
    "write_run_summary",
]

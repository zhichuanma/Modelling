"""Preflight checks for the annual depot-only bus load pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .annual_ev_specs import build_ev_bus_specs, ev_specs_summary


BLOCK_REQUIRED_COLUMNS = (
    "agency_id",
    "service_id",
    "block_id",
    "block_source",
    "start_h",
    "end_h",
    "start_lat",
    "start_lon",
    "end_lat",
    "end_lon",
    "distance_km",
)


def summarize_block_input(blocks: pd.DataFrame) -> dict[str, Any]:
    has_start_end_time = {"start_h", "end_h"}.issubset(blocks.columns)
    has_start_end_lat_lon = {"start_lat", "start_lon", "end_lat", "end_lon"}.issubset(blocks.columns)
    has_distance = "distance_km" in blocks.columns
    block_count = int(blocks["block_id"].nunique()) if "block_id" in blocks.columns else 0
    return {
        "n_trip_rows": int(len(blocks)),
        "available_columns": list(map(str, blocks.columns)),
        "has_block_id": bool("block_id" in blocks.columns),
        "has_agency_id": bool("agency_id" in blocks.columns),
        "has_service_id": bool("service_id" in blocks.columns),
        "has_start_end_time": bool(has_start_end_time),
        "has_start_end_lat_lon": bool(has_start_end_lat_lon),
        "has_distance": bool(has_distance),
        "has_start_lsoa": bool("start_lsoa" in blocks.columns),
        "has_end_lsoa": bool("end_lsoa" in blocks.columns),
        "n_distinct_blocks": block_count,
        "input_appears_trip_level": bool(len(blocks) > block_count if block_count else False),
    }


def run_preflight(
    blocks: pd.DataFrame | str | Path,
    ev_inventory: pd.DataFrame | str | Path,
    *,
    calendar_available: bool,
    lsoa_attach_available: bool,
) -> tuple[dict[str, Any], pd.DataFrame]:
    block_df = pd.read_parquet(blocks) if isinstance(blocks, (str, Path)) else blocks.copy()
    ev_raw = pd.read_csv(ev_inventory) if isinstance(ev_inventory, (str, Path)) else ev_inventory.copy()
    specs, ev_diagnostics = build_ev_bus_specs(ev_raw)
    summary = summarize_block_input(block_df)
    summary.update(ev_specs_summary(ev_raw, specs, ev_diagnostics))
    summary["calendar_available"] = bool(calendar_available)
    summary["lsoa_attach_available"] = bool(lsoa_attach_available)
    summary["missing_block_columns"] = sorted(set(BLOCK_REQUIRED_COLUMNS) - set(block_df.columns))
    summary["preflight_ok"] = bool(
        not summary["missing_block_columns"]
        and summary["n_ev_specs_valid_after_sanity"] > 0
        and calendar_available
    )
    return summary, ev_diagnostics


def write_preflight_summary(summary: dict[str, Any], out_dir: str | Path) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "preflight_summary.json"
    md_path = out / "preflight_summary.md"

    def clean(value: Any) -> Any:
        if isinstance(value, float) and (np.isnan(value) or np.isinf(value)):
            return None
        if isinstance(value, dict):
            return {key: clean(val) for key, val in value.items()}
        if isinstance(value, list):
            return [clean(val) for val in value]
        return value

    json_path.write_text(json.dumps(clean(summary), indent=2, sort_keys=True), encoding="utf-8")
    lines = ["# Bus annual depot-load preflight", ""]
    for key in sorted(summary):
        value = summary[key]
        if isinstance(value, list):
            value = ", ".join(map(str, value[:30])) + (" ..." if len(value) > 30 else "")
        lines.append(f"- {key}: {value}")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


__all__ = ["BLOCK_REQUIRED_COLUMNS", "run_preflight", "summarize_block_input", "write_preflight_summary"]

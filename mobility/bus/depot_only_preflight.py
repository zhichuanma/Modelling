"""Stage 0 preflight and block-template preparation for depot-only bus stock runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from mobility.core.spatial import DEFAULT_ONSPD_PATH, load_lsoa_centroids

from .annual_block_templates import TEMPLATE_GROUP_COLUMNS, build_block_templates
from .annual_lsoa_region import attach_lsoa_and_region
from .ev_bus_instances import build_ev_bus_instances


TRIP_LEVEL_REQUIRED_COLUMNS = set(TEMPLATE_GROUP_COLUMNS) | {
    "trip_id",
    "start_h",
    "end_h",
    "distance_km",
    "start_stop",
    "end_stop",
    "start_lat",
    "start_lon",
    "end_lat",
    "end_lon",
}
BLOCK_LEVEL_REQUIRED_COLUMNS = {
    "block_template_id",
    "block_id",
    "agency_id",
    "block_source",
    "start_h",
    "end_h",
    "duration_h",
    "passenger_distance_km",
    "start_lat",
    "start_lon",
    "end_lat",
    "end_lon",
}


def read_blocks(path: str | Path) -> pd.DataFrame:
    return pd.read_parquet(path)


def read_ev_inventory(path: str | Path) -> pd.DataFrame:
    return pd.read_csv(path)


def summarize_block_input(blocks: pd.DataFrame) -> dict[str, Any]:
    n_distinct_blocks = int(blocks["block_id"].nunique()) if "block_id" in blocks.columns else 0
    appears_trip = bool("trip_id" in blocks.columns and (len(blocks) > n_distinct_blocks if n_distinct_blocks else True))
    return {
        "n_trip_rows_raw": int(len(blocks)),
        "available_block_columns": list(map(str, blocks.columns)),
        "input_appears_trip_level": appears_trip,
        "has_block_template_id": bool("block_template_id" in blocks.columns),
        "has_start_lsoa": bool("start_lsoa" in blocks.columns),
        "has_end_lsoa": bool("end_lsoa" in blocks.columns),
        "n_distinct_block_ids_raw": n_distinct_blocks,
    }


def build_or_validate_block_templates(blocks: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    summary = summarize_block_input(blocks)
    if summary["input_appears_trip_level"]:
        missing = sorted(TRIP_LEVEL_REQUIRED_COLUMNS - set(blocks.columns))
        if missing:
            raise ValueError(f"trip-level blocks are missing required columns: {missing}")
        templates, diagnostics = build_block_templates(blocks)
        mode = "trip_level_aggregated"
    else:
        missing = sorted(BLOCK_LEVEL_REQUIRED_COLUMNS - set(blocks.columns))
        if missing:
            raise ValueError(f"block-level templates are missing required columns: {missing}")
        templates = blocks.copy().reset_index(drop=True)
        diagnostics = pd.DataFrame(
            [
                {
                    "n_trip_rows": int(len(blocks)),
                    "n_block_templates": int(len(templates)),
                    "has_trip_sequence_columns": all(
                        col in templates.columns for col in ("trip_start_times", "trip_end_times", "trip_distances_km")
                    ),
                }
            ]
        )
        mode = "block_level_input"
    summary["block_template_build_mode"] = mode
    summary["missing_block_columns"] = []
    summary["n_block_templates_available"] = int(len(templates))
    return templates, diagnostics, summary


def attach_lsoas_to_templates(
    block_templates: pd.DataFrame,
    *,
    onspd_path: str | Path = DEFAULT_ONSPD_PATH,
    centroids: pd.DataFrame | None = None,
    region_lookup: pd.DataFrame | None = None,
    max_distance_km: float | None = 5.0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if centroids is None:
        centroids = load_lsoa_centroids(Path(onspd_path))
    attached, base_diag = attach_lsoa_and_region(
        block_templates,
        onspd_path=onspd_path,
        centroids=centroids,
        max_distance_km=max_distance_km,
        region_lookup=region_lookup,
    )
    attached = attached.copy()
    attached["candidate_terminal_lsoas"] = [
        _candidate_terminal_lsoas(row) for _, row in attached.iterrows()
    ]
    diagnostics = lsoa_attach_diagnostics(attached, base_diag)
    return attached, diagnostics


def lsoa_attach_diagnostics(block_templates: pd.DataFrame, base_diag: pd.DataFrame | None = None) -> pd.DataFrame:
    n = int(len(block_templates))
    start = block_templates.get("start_lsoa", pd.Series(dtype=object)).fillna("").astype(str).str.strip()
    end = block_templates.get("end_lsoa", pd.Series(dtype=object)).fillna("").astype(str).str.strip()
    methods = pd.concat(
        [
            block_templates.get("start_lsoa_method", pd.Series(dtype=object)).fillna("missing"),
            block_templates.get("end_lsoa_method", pd.Series(dtype=object)).fillna("missing"),
        ],
        ignore_index=True,
    )
    method_counts = methods.astype(str).value_counts().to_dict()
    trip_codes = [
        str(code).strip()
        for column in ("trip_start_lsoas", "trip_end_lsoas")
        for values in block_templates.get(column, pd.Series(dtype=object))
        for code in _as_list(values)
    ]
    record = {
        "n_block_templates": n,
        "start_lsoa_hit_count": int(start.ne("").sum()),
        "end_lsoa_hit_count": int(end.ne("").sum()),
        "start_lsoa_hit_rate": float(start.ne("").mean()) if n else np.nan,
        "end_lsoa_hit_rate": float(end.ne("").mean()) if n else np.nan,
        "both_endpoint_lsoa_hit_rate": float((start.ne("") & end.ne("")).mean()) if n else np.nan,
        "polygon_endpoint_count": int(method_counts.get("polygon", 0)),
        "centroid_fallback_endpoint_count": int(method_counts.get("centroid_fallback", 0)),
        "missing_endpoint_count": int(method_counts.get("no_match", 0) + method_counts.get("missing", 0)),
        "trip_endpoint_lsoa_hit_rate": (
            float(sum(1 for code in trip_codes if code) / len(trip_codes)) if trip_codes else np.nan
        ),
        "lsoa_attach_method_note": "polygon when boundary data exists, otherwise centroid_fallback from ONSPD",
    }
    if base_diag is not None and not base_diag.empty:
        for key, value in base_diag.iloc[0].to_dict().items():
            record[f"base_{key}"] = value
    return pd.DataFrame([record])


def run_stage0_preflight(
    blocks: pd.DataFrame | str | Path,
    ev_inventory: pd.DataFrame | str | Path,
    *,
    onspd_path: str | Path = DEFAULT_ONSPD_PATH,
    centroids: pd.DataFrame | None = None,
    region_lookup: pd.DataFrame | None = None,
    max_distance_km: float | None = 5.0,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    block_df = read_blocks(blocks) if isinstance(blocks, (str, Path)) else blocks.copy()
    ev_raw = read_ev_inventory(ev_inventory) if isinstance(ev_inventory, (str, Path)) else ev_inventory.copy()
    templates, template_diag, block_summary = build_or_validate_block_templates(block_df)
    templates_lsoa, lsoa_diag = attach_lsoas_to_templates(
        templates,
        onspd_path=onspd_path,
        centroids=centroids,
        region_lookup=region_lookup,
        max_distance_km=max_distance_km,
    )
    ev_instances, invalid_vehicle_rows, ev_summary = build_ev_bus_instances(ev_raw)
    summary: dict[str, Any] = {}
    summary.update(block_summary)
    summary.update(ev_summary)
    summary["n_sampled_blocks_full_mode"] = int(ev_summary["n_valid_ev_bus_instances"])
    summary["n_simulation_cases_full_mode"] = int(ev_summary["n_valid_ev_bus_instances"])
    summary["stage0_complete"] = True
    summary["template_diagnostics"] = template_diag.iloc[0].to_dict() if not template_diag.empty else {}
    summary["lsoa_attach"] = lsoa_diag.iloc[0].to_dict() if not lsoa_diag.empty else {}
    return templates_lsoa, lsoa_diag, invalid_vehicle_rows, summary


def write_preflight_outputs(
    out_dir: str | Path,
    *,
    summary: dict[str, Any],
    block_templates: pd.DataFrame,
    lsoa_attach_diagnostics_frame: pd.DataFrame,
    invalid_vehicle_rows: pd.DataFrame,
) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "preflight_summary.json").write_text(json.dumps(_json_clean(summary), indent=2, sort_keys=True), encoding="utf-8")
    (out / "preflight_summary.md").write_text(_preflight_markdown(summary), encoding="utf-8")
    block_templates.to_parquet(out / "block_templates.parquet", index=False)
    lsoa_attach_diagnostics_frame.to_parquet(out / "lsoa_attach_diagnostics.parquet", index=False)
    invalid_vehicle_rows.to_parquet(out / "invalid_vehicle_rows.parquet", index=False)


def _candidate_terminal_lsoas(row: pd.Series) -> list[str]:
    values: list[str] = []
    for column in ("start_lsoa", "end_lsoa"):
        value = _clean_code(row.get(column, ""))
        if value:
            values.append(value)
    for column in ("trip_start_lsoas", "trip_end_lsoas"):
        for value in _as_list(row.get(column, [])):
            code = _clean_code(value)
            if code:
                values.append(code)
    return values


def _clean_code(value: Any) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"", "nan", "none", "<na>"} else text


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if value is None:
        return []
    try:
        if pd.isna(value):
            return []
    except ValueError:
        return []
    return [value]


def _json_clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_clean(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_json_clean(item) for item in value]
    if isinstance(value, tuple):
        return [_json_clean(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if not np.isfinite(float(value)) else float(value)
    if isinstance(value, float) and not np.isfinite(value):
        return None
    if pd.isna(value) if not isinstance(value, (list, tuple, dict)) else False:
        return None
    return value


def _preflight_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Depot-only EV bus stock preflight",
        "",
        "This pipeline uses every valid bus/minibus EV inventory row as one current EV bus stock instance. Count is audit-only and is not expanded.",
        "",
    ]
    for key in sorted(summary):
        value = summary[key]
        if isinstance(value, dict):
            value = json.dumps(_json_clean(value), sort_keys=True)
        lines.append(f"- {key}: {value}")
    return "\n".join(lines) + "\n"


__all__ = [
    "BLOCK_LEVEL_REQUIRED_COLUMNS",
    "TRIP_LEVEL_REQUIRED_COLUMNS",
    "attach_lsoas_to_templates",
    "build_or_validate_block_templates",
    "lsoa_attach_diagnostics",
    "read_blocks",
    "read_ev_inventory",
    "run_stage0_preflight",
    "summarize_block_input",
    "write_preflight_outputs",
]

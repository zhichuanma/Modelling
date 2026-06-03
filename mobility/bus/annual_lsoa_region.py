"""LSOA and region attribution for annual depot-load block templates."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from mobility.core.spatial import DEFAULT_ONSPD_PATH

from .data_loader import attach_lsoa


ENGLAND_REGION_CODES = {
    "E12000001": "North East",
    "E12000002": "North West",
    "E12000003": "Yorkshire and The Humber",
    "E12000004": "East Midlands",
    "E12000005": "West Midlands",
    "E12000006": "East of England",
    "E12000007": "London",
    "E12000008": "South East",
    "E12000009": "South West",
}
COUNTRY_CODES = {
    "E92000001": "England",
    "W92000004": "Wales",
    "S92000003": "Scotland",
    "N92000002": "Northern Ireland",
}


def load_lsoa_region_lookup(onspd_path: str | Path = DEFAULT_ONSPD_PATH) -> pd.DataFrame:
    """Load a compact LSOA/Data Zone to GOR-or-country lookup from ONSPD."""
    source = Path(onspd_path)
    raw = pd.read_csv(
        source,
        dtype="string",
        usecols=lambda column: str(column).strip() in {"lsoa21", "ctry", "rgn"},
    )
    raw.columns = [str(col).strip() for col in raw.columns]
    if "lsoa21" not in raw.columns:
        raise ValueError(f"ONSPD lookup at {source} does not contain lsoa21.")
    for col in ("lsoa21", "ctry", "rgn"):
        if col not in raw.columns:
            raw[col] = pd.NA
    frame = raw.loc[:, ["lsoa21", "ctry", "rgn"]].dropna(subset=["lsoa21"]).copy()
    frame["lsoa_code"] = frame["lsoa21"].astype(str).str.strip()
    frame["ctry"] = frame["ctry"].fillna("").astype(str).str.strip()
    frame["rgn"] = frame["rgn"].fillna("").astype(str).str.strip()
    grouped = frame.groupby("lsoa_code", as_index=False, sort=True).agg(ctry=("ctry", "first"), rgn=("rgn", "first"))
    grouped["region_key"] = grouped.apply(_region_from_lookup_row, axis=1)
    grouped["region_source"] = np.where(grouped["rgn"].str.startswith("E12"), "onspd_rgn", "onspd_country")
    return grouped.loc[:, ["lsoa_code", "region_key", "region_source"]]


def _region_from_lookup_row(row: pd.Series) -> str:
    rgn = str(row.get("rgn", "") or "").strip()
    ctry = str(row.get("ctry", "") or "").strip()
    if rgn in ENGLAND_REGION_CODES:
        return ENGLAND_REGION_CODES[rgn]
    if ctry in COUNTRY_CODES and ctry != "E92000001":
        return COUNTRY_CODES[ctry]
    if ctry == "E92000001":
        return "England_unknown_region"
    return prefix_region_key(str(row.get("lsoa_code", "") or ""))


def prefix_region_key(lsoa_code: str) -> str:
    code = str(lsoa_code or "").strip().upper()
    if code.startswith("S"):
        return "Scotland"
    if code.startswith("W"):
        return "Wales"
    if code.startswith("N"):
        return "Northern Ireland"
    if code.startswith("E"):
        return "England_unknown_region"
    return "unknown"


def attach_lsoa_and_region(
    block_templates: pd.DataFrame,
    *,
    onspd_path: str | Path = DEFAULT_ONSPD_PATH,
    centroids: pd.DataFrame | None = None,
    boundary_index: dict | None = None,
    boundary_paths: tuple | None = None,
    max_distance_km: float | None = 5.0,
    region_lookup: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Attach start/end LSOA and a GOR/country ``region_key`` to templates."""
    if block_templates.empty:
        out = block_templates.copy()
        for col in ("start_lsoa", "end_lsoa", "region_key", "region_source", "region_lookup_success"):
            out[col] = []
        diagnostics = pd.DataFrame([_diagnostic_record(out, onspd_path=onspd_path)])
        return out, diagnostics

    needed = ["block_template_id", "start_lat", "start_lon", "end_lat", "end_lon"]
    missing = sorted(set(needed) - set(block_templates.columns))
    if missing:
        raise ValueError(f"block_templates is missing required columns: {missing}")

    probe = block_templates.loc[:, needed].copy()
    attached = attach_lsoa(
        probe,
        onspd_path=Path(onspd_path),
        centroids=centroids,
        boundary_index=boundary_index,
        boundary_paths=boundary_paths,
        max_distance_km=max_distance_km,
    )
    out = block_templates.copy()
    out["start_lsoa"] = attached["start_lsoa"].fillna("").astype(str)
    out["end_lsoa"] = attached["end_lsoa"].fillna("").astype(str)
    out["start_lsoa_method"] = attached["start_lsoa_match_method"].fillna("no_match").astype(str)
    out["end_lsoa_method"] = attached["end_lsoa_match_method"].fillna("no_match").astype(str)
    out["lsoa_attach_distance_m"] = (
        pd.concat(
            [
                pd.to_numeric(attached["start_lsoa_distance_km"], errors="coerce"),
                pd.to_numeric(attached["end_lsoa_distance_km"], errors="coerce"),
            ],
            axis=1,
        ).max(axis=1)
        * 1000.0
    )
    out["manual_review_flag"] = out["start_lsoa"].eq("") | out["end_lsoa"].eq("")
    out = _attach_trip_endpoint_lsoas(
        out,
        onspd_path=onspd_path,
        centroids=centroids,
        boundary_index=boundary_index,
        boundary_paths=boundary_paths,
        max_distance_km=max_distance_km,
    )

    lookup = region_lookup.copy() if region_lookup is not None else _safe_load_region_lookup(onspd_path)
    if lookup is not None and not lookup.empty:
        lookup = lookup.drop_duplicates("lsoa_code", keep="first")
        merged = out[["end_lsoa"]].rename(columns={"end_lsoa": "lsoa_code"}).merge(
            lookup,
            on="lsoa_code",
            how="left",
        )
        out["region_key"] = merged["region_key"].fillna(out["end_lsoa"].map(prefix_region_key)).astype(str)
        out["region_source"] = merged["region_source"].fillna("prefix_fallback").astype(str)
        out["region_lookup_success"] = merged["region_key"].notna()
    else:
        out["region_key"] = out["end_lsoa"].map(prefix_region_key).astype(str)
        out["region_source"] = "prefix_fallback"
        out["region_lookup_success"] = out["end_lsoa"].astype(str).str.strip().ne("")

    diagnostics = pd.DataFrame([_diagnostic_record(out, onspd_path=onspd_path)])
    return out.reset_index(drop=True), diagnostics


def _attach_trip_endpoint_lsoas(
    block_templates: pd.DataFrame,
    *,
    onspd_path: str | Path,
    centroids: pd.DataFrame | None,
    boundary_index: dict | None,
    boundary_paths: tuple | None,
    max_distance_km: float | None,
) -> pd.DataFrame:
    """Attach LSOA list columns for per-trip endpoints.

    These columns are what the event-ledger stage uses to identify eligible
    midday depot parking windows. Without them, interior layovers cannot be
    distinguished from ordinary terminal layovers.
    """
    required = {"trip_start_lats", "trip_start_lons", "trip_end_lats", "trip_end_lons"}
    out = block_templates.copy()
    if not required.issubset(out.columns):
        return out

    rows: list[dict[str, Any]] = []
    for template_pos, row in out.iterrows():
        start_lats = _as_list(row.get("trip_start_lats", []))
        start_lons = _as_list(row.get("trip_start_lons", []))
        end_lats = _as_list(row.get("trip_end_lats", []))
        end_lons = _as_list(row.get("trip_end_lons", []))
        n = max(len(start_lats), len(start_lons), len(end_lats), len(end_lons))
        for trip_pos in range(n):
            rows.append(
                {
                    "_template_pos": template_pos,
                    "_trip_pos": trip_pos,
                    "start_lat": _list_get(start_lats, trip_pos, np.nan),
                    "start_lon": _list_get(start_lons, trip_pos, np.nan),
                    "end_lat": _list_get(end_lats, trip_pos, np.nan),
                    "end_lon": _list_get(end_lons, trip_pos, np.nan),
                }
            )
    if not rows:
        out["trip_start_lsoas"] = [[] for _ in range(len(out))]
        out["trip_end_lsoas"] = [[] for _ in range(len(out))]
        return out

    endpoint_frame = pd.DataFrame.from_records(rows)
    attached = attach_lsoa(
        endpoint_frame,
        onspd_path=Path(onspd_path),
        centroids=centroids,
        boundary_index=boundary_index,
        boundary_paths=boundary_paths,
        max_distance_km=max_distance_km,
    )
    out["trip_start_lsoas"] = [[] for _ in range(len(out))]
    out["trip_end_lsoas"] = [[] for _ in range(len(out))]
    out["trip_start_lsoa_methods"] = [[] for _ in range(len(out))]
    out["trip_end_lsoa_methods"] = [[] for _ in range(len(out))]
    grouped = attached.sort_values(["_template_pos", "_trip_pos"], kind="stable").groupby("_template_pos", sort=False)
    for template_pos, frame in grouped:
        out.at[template_pos, "trip_start_lsoas"] = frame["start_lsoa"].fillna("").astype(str).tolist()
        out.at[template_pos, "trip_end_lsoas"] = frame["end_lsoa"].fillna("").astype(str).tolist()
        out.at[template_pos, "trip_start_lsoa_methods"] = frame["start_lsoa_match_method"].fillna("no_match").astype(str).tolist()
        out.at[template_pos, "trip_end_lsoa_methods"] = frame["end_lsoa_match_method"].fillna("no_match").astype(str).tolist()
    return out


def _safe_load_region_lookup(onspd_path: str | Path) -> pd.DataFrame | None:
    try:
        return load_lsoa_region_lookup(onspd_path)
    except (FileNotFoundError, KeyError, ValueError, pd.errors.EmptyDataError):
        return None


def _diagnostic_record(out: pd.DataFrame, *, onspd_path: str | Path) -> dict[str, Any]:
    n = int(len(out))
    start_success = int(out.get("start_lsoa", pd.Series(dtype=str)).astype(str).str.strip().ne("").sum()) if n else 0
    end_success = int(out.get("end_lsoa", pd.Series(dtype=str)).astype(str).str.strip().ne("").sum()) if n else 0
    region_success = int(out.get("region_lookup_success", pd.Series(dtype=bool)).fillna(False).sum()) if n else 0
    trip_lsoas = [
        code
        for col in ("trip_start_lsoas", "trip_end_lsoas")
        for values in out.get(col, pd.Series(dtype=object))
        for code in _as_list(values)
    ]
    n_trip_endpoint_lsoas = len(trip_lsoas)
    n_trip_endpoint_matches = sum(1 for code in trip_lsoas if str(code).strip())
    return {
        "n_block_templates": n,
        "start_lsoa_success_rate": start_success / n if n else np.nan,
        "end_lsoa_success_rate": end_success / n if n else np.nan,
        "region_lookup_success_rate": region_success / n if n else np.nan,
        "trip_endpoint_lsoa_success_rate": n_trip_endpoint_matches / n_trip_endpoint_lsoas
        if n_trip_endpoint_lsoas
        else np.nan,
        "n_trip_endpoint_lsoa_assignments": n_trip_endpoint_lsoas,
        "region_lookup_path": str(onspd_path),
    }


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if pd.isna(value):
        return []
    return [value]


def _list_get(values: list[Any], index: int, default: Any) -> Any:
    return values[index] if index < len(values) and not pd.isna(values[index]) else default


__all__ = [
    "COUNTRY_CODES",
    "ENGLAND_REGION_CODES",
    "attach_lsoa_and_region",
    "load_lsoa_region_lookup",
    "prefix_region_key",
]

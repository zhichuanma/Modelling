"""Stage 2 operational depot-anchor inference for depot-only bus stock runs."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

import numpy as np
import pandas as pd


ANCHOR_LIMITATION_NOTE = (
    "Operational depot is a block-terminal/end-LSOA charging anchor inferred for scenario analysis; "
    "it is not a real, verified, physical garage location."
)


def infer_operational_depot(row: pd.Series) -> dict[str, Any]:
    start_lsoa = _clean(row.get("start_lsoa", ""))
    end_lsoa = _clean(row.get("end_lsoa", ""))
    agency_id = _clean(row.get("agency_id", ""))
    block_template_id = _clean(row.get("block_template_id", ""))

    if start_lsoa and end_lsoa and start_lsoa == end_lsoa:
        depot_lsoa = start_lsoa
        confidence = "high"
        method = "closed_block_start_lsoa_equals_end_lsoa"
    else:
        depot_lsoa, method = _terminal_mode_lsoa(row, end_lsoa=end_lsoa)
        if depot_lsoa:
            confidence = "medium" if method.startswith("trip_terminal") else "low"
        elif end_lsoa:
            depot_lsoa = end_lsoa
            confidence = "low"
            method = "end_lsoa_fallback"
        else:
            depot_lsoa = ""
            confidence = "missing"
            method = "missing_lsoa_manual_review"

    depot_id = f"opdepot_{agency_id}_{depot_lsoa}" if depot_lsoa else f"opdepot_{agency_id}_missing"
    return {
        "block_template_id": block_template_id,
        "agency_id": agency_id,
        "operational_depot_lsoa": depot_lsoa,
        "depot_id": depot_id,
        "depot_confidence": confidence,
        "depot_inference_method": method,
        "manual_review_flag": bool(confidence == "missing"),
        "anchor_limitation_note": ANCHOR_LIMITATION_NOTE,
    }


def infer_operational_depots(
    sampled_blocks: pd.DataFrame,
    *,
    centroids: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Infer depot anchors for sampled blocks and build a registry."""
    if sampled_blocks.empty:
        empty_registry = pd.DataFrame(columns=_registry_columns())
        empty_diag = pd.DataFrame(columns=_diagnostic_columns())
        return sampled_blocks.copy(), empty_registry, empty_diag
    diagnostics = pd.DataFrame([infer_operational_depot(row) for _, row in sampled_blocks.iterrows()])
    inference_cols = [
        "operational_depot_lsoa",
        "depot_id",
        "depot_confidence",
        "depot_inference_method",
        "manual_review_flag",
        "anchor_limitation_note",
    ]
    base_blocks = sampled_blocks.drop(columns=[col for col in inference_cols if col in sampled_blocks.columns], errors="ignore")
    blocks = base_blocks.merge(diagnostics.drop(columns=["agency_id"], errors="ignore"), on="block_template_id", how="left")
    registry = _build_registry(blocks, centroids=centroids)
    coord_cols = ["depot_id", "depot_lat", "depot_lon", "depot_coordinate_source"]
    blocks = blocks.merge(registry.loc[:, coord_cols].drop_duplicates("depot_id", keep="first"), on="depot_id", how="left")
    return blocks.reset_index(drop=True), registry.reset_index(drop=True), diagnostics.reset_index(drop=True)


def _terminal_mode_lsoa(row: pd.Series, *, end_lsoa: str) -> tuple[str, str]:
    candidates = []
    for column in ("trip_start_lsoas", "trip_end_lsoas"):
        candidates.extend(_clean(value) for value in _as_list(row.get(column, [])))
    if len(candidates) <= 2:
        candidates = [_clean(value) for value in _as_list(row.get("candidate_terminal_lsoas", []))]
    candidates = [value for value in candidates if value]
    if len(candidates) <= 2:
        return "", ""
    counts = Counter(candidates)
    if not counts:
        return "", ""
    max_count = max(counts.values())
    tied = sorted([code for code, count in counts.items() if count == max_count])
    if len(tied) == 1:
        return tied[0], "trip_terminal_unique_mode"
    if end_lsoa and end_lsoa in tied:
        return end_lsoa, "trip_terminal_tie_break_final_end_lsoa"
    dwell_choice = _longest_dwell_lsoa(row, tied)
    if dwell_choice:
        return dwell_choice, "trip_terminal_tie_break_longest_dwell"
    return tied[0], "trip_terminal_tie_break_lexicographic"


def _longest_dwell_lsoa(row: pd.Series, tied: list[str]) -> str:
    starts = _as_float_list(row.get("trip_start_times", []))
    ends = _as_float_list(row.get("trip_end_times", []))
    start_lsoas = [_clean(value) for value in _as_list(row.get("trip_start_lsoas", []))]
    end_lsoas = [_clean(value) for value in _as_list(row.get("trip_end_lsoas", []))]
    dwell_by_lsoa: dict[str, float] = defaultdict(float)
    n = min(len(starts), len(ends), len(start_lsoas), len(end_lsoas))
    for idx in range(n - 1):
        if end_lsoas[idx] and end_lsoas[idx] == start_lsoas[idx + 1] and end_lsoas[idx] in tied:
            dwell = starts[idx + 1] - ends[idx]
            if np.isfinite(dwell) and dwell > 0:
                dwell_by_lsoa[end_lsoas[idx]] += float(dwell)
    if not dwell_by_lsoa:
        return ""
    return sorted(dwell_by_lsoa.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _build_registry(blocks: pd.DataFrame, *, centroids: pd.DataFrame | None) -> pd.DataFrame:
    if blocks.empty:
        return pd.DataFrame(columns=_registry_columns())
    coord_records = []
    centroid_lookup = _centroid_lookup(centroids)
    for depot_id, group in blocks.groupby("depot_id", sort=True):
        depot_lsoa = _clean(group["operational_depot_lsoa"].iloc[0])
        lat_values: list[float] = []
        lon_values: list[float] = []
        for row in group.itertuples(index=False):
            if _clean(getattr(row, "start_lsoa", "")) == depot_lsoa and _finite(getattr(row, "start_lat", np.nan)) and _finite(getattr(row, "start_lon", np.nan)):
                lat_values.append(float(getattr(row, "start_lat")))
                lon_values.append(float(getattr(row, "start_lon")))
            if _clean(getattr(row, "end_lsoa", "")) == depot_lsoa and _finite(getattr(row, "end_lat", np.nan)) and _finite(getattr(row, "end_lon", np.nan)):
                lat_values.append(float(getattr(row, "end_lat")))
                lon_values.append(float(getattr(row, "end_lon")))
        if lat_values and lon_values:
            lat = float(np.nanmedian(lat_values))
            lon = float(np.nanmedian(lon_values))
            coord_source = "block_terminal_endpoint_median"
        elif depot_lsoa in centroid_lookup:
            lat, lon = centroid_lookup[depot_lsoa]
            coord_source = "lsoa_centroid"
        else:
            lat, lon = np.nan, np.nan
            coord_source = "missing"
        coord_records.append({"depot_id": depot_id, "depot_lat": lat, "depot_lon": lon, "depot_coordinate_source": coord_source})
    coords = pd.DataFrame.from_records(coord_records)
    agg = blocks.groupby("depot_id", as_index=False, sort=True).agg(
        agency_id=("agency_id", "first"),
        operational_depot_lsoa=("operational_depot_lsoa", "first"),
        depot_confidence=("depot_confidence", _best_confidence),
        source_block_template_count=("block_template_id", "nunique"),
        manual_review_flag=("manual_review_flag", "max"),
        region_key=("region_key", _mode_string),
    )
    registry = agg.merge(coords, on="depot_id", how="left")
    registry["is_operational_anchor"] = registry["operational_depot_lsoa"].astype(str).str.strip().ne("")
    registry["anchor_limitation_note"] = ANCHOR_LIMITATION_NOTE
    registry["manual_review_flag"] = registry["manual_review_flag"].astype(bool) | registry["depot_lat"].isna() | registry["depot_lon"].isna()
    return registry.loc[:, _registry_columns()]


def _best_confidence(values: pd.Series) -> str:
    order = {"high": 0, "medium": 1, "low": 2, "missing": 3}
    cleaned = values.fillna("missing").astype(str)
    return sorted(cleaned, key=lambda value: order.get(value, 9))[0]


def _mode_string(values: pd.Series) -> str:
    cleaned = values.dropna().astype(str)
    if cleaned.empty:
        return ""
    mode = cleaned.mode()
    return str(mode.iloc[0]) if not mode.empty else str(cleaned.iloc[0])


def _centroid_lookup(centroids: pd.DataFrame | None) -> dict[str, tuple[float, float]]:
    if centroids is None or centroids.empty or "lsoa_code" not in centroids.columns:
        return {}
    required = {"lat", "lon"}
    if not required.issubset(centroids.columns):
        return {}
    out: dict[str, tuple[float, float]] = {}
    for row in centroids[["lsoa_code", "lat", "lon"]].itertuples(index=False):
        if _finite(row.lat) and _finite(row.lon):
            out[str(row.lsoa_code)] = (float(row.lat), float(row.lon))
    return out


def _clean(value: Any) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "<na>"} else text


def _finite(value: Any) -> bool:
    try:
        return bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False


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


def _as_float_list(value: Any) -> list[float]:
    return [float(item) if _finite(item) else np.nan for item in _as_list(value)]


def _registry_columns() -> list[str]:
    return [
        "depot_id",
        "agency_id",
        "operational_depot_lsoa",
        "region_key",
        "depot_lat",
        "depot_lon",
        "depot_confidence",
        "is_operational_anchor",
        "source_block_template_count",
        "manual_review_flag",
        "depot_coordinate_source",
        "anchor_limitation_note",
    ]


def _diagnostic_columns() -> list[str]:
    return [
        "block_template_id",
        "agency_id",
        "operational_depot_lsoa",
        "depot_id",
        "depot_confidence",
        "depot_inference_method",
        "manual_review_flag",
        "anchor_limitation_note",
    ]


__all__ = [
    "ANCHOR_LIMITATION_NOTE",
    "infer_operational_depot",
    "infer_operational_depots",
]

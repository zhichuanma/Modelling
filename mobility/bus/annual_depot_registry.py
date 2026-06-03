"""Operational depot-anchor registry for annual depot-only bus load runs."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


LIMITATION_NOTE = (
    "Operational depot anchors are inferred charging anchors, not verified physical garage locations. "
    "When depot is inferred from end_lsoa, depot-to-route and route-to-depot deadhead may be biased."
)


def _clean_lsoa(value: Any) -> str:
    text = "" if pd.isna(value) else str(value).strip()
    return "" if text.lower() in {"nan", "none"} else text


def _finite(value: Any) -> bool:
    try:
        return bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False


def infer_template_depot(row: pd.Series) -> dict[str, Any]:
    start_lsoa = _clean_lsoa(row.get("start_lsoa", ""))
    end_lsoa = _clean_lsoa(row.get("end_lsoa", ""))
    agency_id = "" if pd.isna(row.get("agency_id", "")) else str(row.get("agency_id", ""))

    if start_lsoa and end_lsoa and start_lsoa == end_lsoa:
        depot_lsoa = start_lsoa
        confidence = "high"
        source = "block_level_closed_loop_anchor"
        method = "start_lsoa_equals_end_lsoa"
    elif end_lsoa:
        depot_lsoa = end_lsoa
        confidence = "low"
        source = "block_terminal_end_lsoa_mode_anchor"
        method = "end_lsoa_fallback"
    elif start_lsoa:
        depot_lsoa = start_lsoa
        confidence = "low"
        source = "lsoa_centroid_fallback"
        method = "start_lsoa_fallback"
    else:
        depot_lsoa = ""
        confidence = "missing"
        source = "missing"
        method = "missing_lsoa"

    depot_id = f"opdepot_{agency_id}_{depot_lsoa}" if depot_lsoa else f"opdepot_{agency_id}_missing"
    return {
        "block_template_id": row.get("block_template_id", ""),
        "agency_id": agency_id,
        "depot_id": depot_id,
        "depot_lsoa": depot_lsoa,
        "depot_source": source,
        "depot_confidence": confidence,
        "depot_assignment_method": method,
        "manual_review_flag": bool(confidence in {"low", "missing"}),
    }


def build_operational_depot_registry(
    block_templates_lsoa: pd.DataFrame,
    block_instances: pd.DataFrame | None = None,
    *,
    centroids: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Infer one operational depot anchor per block template, then aggregate."""
    if block_templates_lsoa.empty:
        empty_registry = pd.DataFrame(
            columns=[
                "depot_id",
                "agency_id",
                "depot_lat",
                "depot_lon",
                "depot_lsoa",
                "depot_source",
                "depot_confidence",
                "is_physical_depot",
                "is_operational_anchor",
                "source_block_template_count",
                "source_block_instance_count",
                "manual_review_flag",
                "limitation_note",
            ]
        )
        return empty_registry, pd.DataFrame(), pd.DataFrame()

    assignments = pd.DataFrame([infer_template_depot(row) for _, row in block_templates_lsoa.iterrows()])
    coord_cols = ["block_template_id", "start_lat", "start_lon", "end_lat", "end_lon", "start_lsoa", "end_lsoa"]
    assignments = assignments.merge(block_templates_lsoa.loc[:, [col for col in coord_cols if col in block_templates_lsoa.columns]], on="block_template_id", how="left")

    coord_records: list[dict[str, Any]] = []
    centroid_lookup = _centroid_lookup(centroids)
    for depot_id, group in assignments.groupby("depot_id", sort=True):
        depot_lsoa = _clean_lsoa(group["depot_lsoa"].iloc[0])
        lat_values: list[float] = []
        lon_values: list[float] = []
        for row in group.itertuples(index=False):
            if _clean_lsoa(getattr(row, "end_lsoa", "")) == depot_lsoa and _finite(getattr(row, "end_lat", np.nan)) and _finite(getattr(row, "end_lon", np.nan)):
                lat_values.append(float(getattr(row, "end_lat")))
                lon_values.append(float(getattr(row, "end_lon")))
            if _clean_lsoa(getattr(row, "start_lsoa", "")) == depot_lsoa and _finite(getattr(row, "start_lat", np.nan)) and _finite(getattr(row, "start_lon", np.nan)):
                lat_values.append(float(getattr(row, "start_lat")))
                lon_values.append(float(getattr(row, "start_lon")))
        if lat_values and lon_values:
            lat = float(np.nanmedian(lat_values))
            lon = float(np.nanmedian(lon_values))
            coord_source = "modal_terminal_endpoint_median"
        elif depot_lsoa in centroid_lookup:
            lat, lon = centroid_lookup[depot_lsoa]
            coord_source = "lsoa_centroid"
        else:
            lat, lon = np.nan, np.nan
            coord_source = "missing"
        coord_records.append({"depot_id": depot_id, "depot_lat": lat, "depot_lon": lon, "depot_coordinate_source": coord_source})

    coords = pd.DataFrame.from_records(coord_records)
    template_counts = assignments.groupby("depot_id", as_index=False).agg(
        agency_id=("agency_id", "first"),
        depot_lsoa=("depot_lsoa", "first"),
        depot_source=("depot_source", _mode_string),
        depot_confidence=("depot_confidence", _best_confidence),
        source_block_template_count=("block_template_id", "nunique"),
        manual_review_flag=("manual_review_flag", "max"),
    )
    if block_instances is not None and not block_instances.empty:
        inst = block_instances.loc[:, ["block_template_id", "block_instance_id"]].merge(
            assignments.loc[:, ["block_template_id", "depot_id"]],
            on="block_template_id",
            how="left",
        )
        instance_counts = inst.groupby("depot_id", as_index=False).agg(source_block_instance_count=("block_instance_id", "nunique"))
    else:
        instance_counts = pd.DataFrame({"depot_id": template_counts["depot_id"], "source_block_instance_count": 0})

    registry = (
        template_counts.merge(instance_counts, on="depot_id", how="left")
        .merge(coords, on="depot_id", how="left")
        .sort_values(["agency_id", "depot_lsoa", "depot_id"], kind="stable")
        .reset_index(drop=True)
    )
    registry["is_physical_depot"] = False
    registry["is_operational_anchor"] = registry["depot_confidence"].ne("missing")
    registry["source_block_instance_count"] = registry["source_block_instance_count"].fillna(0).astype(int)
    registry["manual_review_flag"] = registry["manual_review_flag"].astype(bool) | registry["depot_lat"].isna() | registry["depot_lon"].isna()
    registry["limitation_note"] = LIMITATION_NOTE
    registry = registry.loc[
        :,
        [
            "depot_id",
            "agency_id",
            "depot_lat",
            "depot_lon",
            "depot_lsoa",
            "depot_source",
            "depot_confidence",
            "is_physical_depot",
            "is_operational_anchor",
            "source_block_template_count",
            "source_block_instance_count",
            "manual_review_flag",
            "limitation_note",
            "depot_coordinate_source",
        ],
    ]

    block_depot = assignments.loc[
        :,
        [
            "block_template_id",
            "agency_id",
            "depot_id",
            "depot_lsoa",
            "depot_source",
            "depot_confidence",
            "depot_assignment_method",
            "manual_review_flag",
        ],
    ].reset_index(drop=True)
    diagnostics = block_depot.copy()
    return registry, diagnostics, block_depot


def attach_depots_to_instances(block_instances: pd.DataFrame, block_depot: pd.DataFrame) -> pd.DataFrame:
    if block_instances.empty:
        return block_instances.copy()
    cols = ["block_template_id", "depot_id", "depot_lsoa", "depot_source", "depot_confidence"]
    return block_instances.merge(block_depot.loc[:, cols], on="block_template_id", how="left")


def _mode_string(values: pd.Series) -> str:
    cleaned = values.dropna().astype(str)
    if cleaned.empty:
        return ""
    mode = cleaned.mode()
    return str(mode.iloc[0]) if not mode.empty else str(cleaned.iloc[0])


def _best_confidence(values: pd.Series) -> str:
    order = {"high": 0, "medium": 1, "low": 2, "missing": 3}
    cleaned = values.dropna().astype(str)
    if cleaned.empty:
        return "missing"
    return sorted(cleaned, key=lambda value: order.get(value, 9))[0]


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


__all__ = [
    "LIMITATION_NOTE",
    "attach_depots_to_instances",
    "build_operational_depot_registry",
    "infer_template_depot",
]

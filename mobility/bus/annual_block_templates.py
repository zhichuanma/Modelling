"""Build annual-depot block templates from trip-level bus blocks."""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

import numpy as np
import pandas as pd


TEMPLATE_GROUP_COLUMNS = ("agency_id", "service_id", "block_id", "block_source")


def hours_to_time_label(hours: float) -> str:
    """Format GTFS-style absolute hours without wrapping at 24:00."""
    if not np.isfinite(float(hours)):
        return ""
    total_seconds = int(round(float(hours) * 3600.0))
    sign = "-" if total_seconds < 0 else ""
    total_seconds = abs(total_seconds)
    hh, rem = divmod(total_seconds, 3600)
    mm, ss = divmod(rem, 60)
    return f"{sign}{hh:02d}:{mm:02d}:{ss:02d}"


def stable_template_id(values: Iterable[Any]) -> str:
    key = "|".join("" if pd.isna(value) else str(value) for value in values)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:14]
    return f"bt_{digest}"


def _as_float_list(series: pd.Series) -> list[float]:
    return [float(value) if pd.notna(value) else np.nan for value in series.to_numpy()]


def _as_str_list(series: pd.Series) -> list[str]:
    return ["" if pd.isna(value) else str(value) for value in series.to_numpy()]


def _first_value(frame: pd.DataFrame, column: str, default: Any = "") -> Any:
    if column not in frame.columns or frame.empty:
        return default
    return frame[column].iloc[0]


def _last_value(frame: pd.DataFrame, column: str, default: Any = "") -> Any:
    if column not in frame.columns or frame.empty:
        return default
    return frame[column].iloc[-1]


def _ordered_trips(blocks: pd.DataFrame) -> pd.DataFrame:
    sort_cols = [col for col in ("agency_id", "service_id", "block_id", "block_source", "start_h", "end_h", "trip_id") if col in blocks.columns]
    out = blocks.copy()
    for col in ("start_h", "end_h", "distance_km", "start_lat", "start_lon", "end_lat", "end_lon"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.sort_values(sort_cols, kind="stable").reset_index(drop=True)


def build_block_templates(blocks: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Aggregate trip-level ``all_blocks`` rows to block templates.

    The input is expected to retain GTFS absolute hours, including values above
    24 for cross-midnight trips. This function preserves those values in both
    scalar and list columns.
    """
    required = set(TEMPLATE_GROUP_COLUMNS) | {
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
    missing = sorted(required - set(blocks.columns))
    if missing:
        raise ValueError(f"blocks is missing required columns: {missing}")

    ordered = _ordered_trips(blocks)
    records: list[dict[str, Any]] = []
    groupby_keys = list(TEMPLATE_GROUP_COLUMNS)
    for key_values, frame in ordered.groupby(groupby_keys, sort=True, dropna=False):
        if not isinstance(key_values, tuple):
            key_values = (key_values,)
        first_start = float(frame["start_h"].iloc[0])
        last_end = float(frame["end_h"].iloc[-1])
        record = {
            "block_template_id": stable_template_id(key_values),
            "agency_id": str(key_values[0]),
            "service_id": str(key_values[1]),
            "block_id": str(key_values[2]),
            "block_source": str(key_values[3]),
            "n_trips": int(len(frame)),
            "start_h": first_start,
            "end_h": last_end,
            "start_time": hours_to_time_label(first_start),
            "end_time": hours_to_time_label(last_end),
            "duration_h": float(last_end - first_start),
            "passenger_distance_km": float(pd.to_numeric(frame["distance_km"], errors="coerce").fillna(0.0).sum()),
            "start_stop": str(_first_value(frame, "start_stop", "")),
            "end_stop": str(_last_value(frame, "end_stop", "")),
            "start_lat": float(_first_value(frame, "start_lat", np.nan)),
            "start_lon": float(_first_value(frame, "start_lon", np.nan)),
            "end_lat": float(_last_value(frame, "end_lat", np.nan)),
            "end_lon": float(_last_value(frame, "end_lon", np.nan)),
            "trip_ids": _as_str_list(frame["trip_id"]),
            "trip_start_times": _as_float_list(frame["start_h"]),
            "trip_end_times": _as_float_list(frame["end_h"]),
            "trip_start_lats": _as_float_list(frame["start_lat"]),
            "trip_start_lons": _as_float_list(frame["start_lon"]),
            "trip_end_lats": _as_float_list(frame["end_lat"]),
            "trip_end_lons": _as_float_list(frame["end_lon"]),
            "trip_distances_km": _as_float_list(frame["distance_km"]),
            "trip_start_stops": _as_str_list(frame["start_stop"]),
            "trip_end_stops": _as_str_list(frame["end_stop"]),
        }
        if "route_id" in frame.columns:
            record["route_ids"] = _as_str_list(frame["route_id"])
        if "shape_id" in frame.columns:
            record["shape_ids"] = _as_str_list(frame["shape_id"])
        records.append(record)

    templates = pd.DataFrame.from_records(records)
    if not templates.empty:
        templates = templates.sort_values(
            ["agency_id", "service_id", "block_id", "block_source", "start_h"],
            kind="stable",
        ).reset_index(drop=True)

    diagnostics = pd.DataFrame(
        [
            {
                "n_trip_rows": int(len(blocks)),
                "n_block_templates": int(len(templates)),
                "n_cross_midnight_templates": int((templates["end_h"] >= 24.0).sum()) if not templates.empty else 0,
                "min_trips_per_template": int(templates["n_trips"].min()) if not templates.empty else 0,
                "max_trips_per_template": int(templates["n_trips"].max()) if not templates.empty else 0,
                "has_trip_sequence_columns": True,
            }
        ]
    )
    return templates, diagnostics


__all__ = ["TEMPLATE_GROUP_COLUMNS", "build_block_templates", "hours_to_time_label", "stable_template_id"]

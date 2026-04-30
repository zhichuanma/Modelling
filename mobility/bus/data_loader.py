"""Data loading and quality summaries for the bus block table."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


DEFAULT_PATH = Path(__file__).resolve().parents[2] / "outputs" / "all_blocks.parquet"

BASE_COLUMNS = (
    "trip_id",
    "agency_id",
    "route_id",
    "service_id",
    "direction_id",
    "block_id",
    "block_source",
    "start_h",
    "end_h",
    "distance_km",
    "start_stop",
    "end_stop",
    "start_lat",
    "start_lon",
    "end_lat",
    "end_lon",
    "shape_id",
)

KEY_COLUMNS = tuple(col for col in BASE_COLUMNS if col != "shape_id")


def _ensure_distance_source(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    has_shape = out["shape_id"].notna() & out["shape_id"].astype(str).str.strip().ne("")
    out["distance_source"] = np.where(has_shape, "shape", "stop_haversine")
    return out


def _pct(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return float("nan")
    return float(numerator) / float(denominator) * 100.0


def load_all_blocks(path: Path = DEFAULT_PATH) -> pd.DataFrame:
    """Read ``all_blocks.parquet``, validate the frozen schema, add distance source."""
    df = pd.read_parquet(path)
    missing = [col for col in BASE_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(f"all_blocks is missing required columns: {missing}")

    null_counts = df.loc[:, KEY_COLUMNS].isna().sum()
    bad = null_counts[null_counts > 0]
    if not bad.empty:
        raise ValueError(f"all_blocks has nulls in key columns: {bad.to_dict()}")

    return _ensure_distance_source(df.loc[:, BASE_COLUMNS])


def summarize_block_quality(df: pd.DataFrame) -> pd.DataFrame:
    """Return one-row block quality metrics with source-specific continuity."""
    data = _ensure_distance_source(df) if "distance_source" not in df.columns else df.copy()
    ordered = data.sort_values(["block_id", "start_h", "end_h"])

    block_source = ordered.groupby("block_id", sort=False)["block_source"].first()
    n_blocks = int(block_source.shape[0])
    n_trips = int(len(ordered))
    native_blocks = int((block_source == "native").sum())

    prev_end_stop = ordered.groupby("block_id", sort=False)["end_stop"].shift()
    has_prev = prev_end_stop.notna()
    continuity = ordered.loc[has_prev, ["block_source"]].copy()
    continuity["is_continuous"] = (
        prev_end_stop.loc[has_prev].astype(str).values
        == ordered.loc[has_prev, "start_stop"].astype(str).values
    )

    continuity_by_source = continuity.groupby("block_source")["is_continuous"].mean() * 100.0

    block_cross_midnight = ordered.groupby("block_id", sort=False)["end_h"].max() >= 24.0

    prev_end_h = ordered.groupby("block_id", sort=False)["end_h"].shift()
    layover_h = ordered.loc[has_prev, "start_h"].astype(float) - prev_end_h.loc[has_prev].astype(float)
    layover_h = layover_h[layover_h >= 0.0]

    record = {
        "n_blocks": n_blocks,
        "n_trips": n_trips,
        "pct_native": _pct(native_blocks, n_blocks),
        "pct_inferred": _pct(n_blocks - native_blocks, n_blocks),
        "pct_shape_distance": _pct((data["distance_source"] == "shape").sum(), n_trips),
        "pct_stop_haversine_distance": _pct(
            (data["distance_source"] == "stop_haversine").sum(),
            n_trips,
        ),
        "stop_continuity_native": float(continuity_by_source.get("native", np.nan)),
        "stop_continuity_inferred": float(continuity_by_source.get("inferred", np.nan)),
        "pct_cross_midnight_blocks": _pct(block_cross_midnight.sum(), n_blocks),
        "layover_h_p50": float(layover_h.quantile(0.50)) if len(layover_h) else np.nan,
        "layover_h_p95": float(layover_h.quantile(0.95)) if len(layover_h) else np.nan,
    }
    return pd.DataFrame([record])


def filter_to_clean_blocks(
    df: pd.DataFrame,
    *,
    block_source: tuple[str, ...] = ("native",),
    max_total_km: float = 1000.0,
    min_total_km: float = 30.0,
    allow_cross_midnight: bool = False,
) -> pd.DataFrame:
    """Filter at block level and record the exact criteria in ``DataFrame.attrs``."""
    data = _ensure_distance_source(df) if "distance_source" not in df.columns else df.copy()
    grouped = data.groupby("block_id", sort=False)
    stats = grouped.agg(
        block_source=("block_source", "first"),
        total_km=("distance_km", "sum"),
        has_cross_midnight=("end_h", lambda s: bool((s >= 24.0).any())),
    )

    keep = (
        stats["block_source"].isin(block_source)
        & stats["total_km"].between(min_total_km, max_total_km, inclusive="both")
    )
    if not allow_cross_midnight:
        keep &= ~stats["has_cross_midnight"]

    kept_blocks = stats.index[keep]
    out = data[data["block_id"].isin(kept_blocks)].copy()
    out.attrs["filters"] = {
        "block_source": tuple(block_source),
        "min_total_km": float(min_total_km),
        "max_total_km": float(max_total_km),
        "allow_cross_midnight": bool(allow_cross_midnight),
        "input_blocks": int(stats.shape[0]),
        "output_blocks": int(len(kept_blocks)),
        "dropped_blocks": int(stats.shape[0] - len(kept_blocks)),
    }
    return out

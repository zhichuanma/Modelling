"""Deterministic block selection helpers for the bus narrative notebook."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .data_loader import _ensure_distance_source


def _block_stats(df: pd.DataFrame) -> pd.DataFrame:
    data = _ensure_distance_source(df) if "distance_source" not in df.columns else df.copy()
    stats = data.groupby("block_id", sort=False).agg(
        agency_id=("agency_id", "first"),
        block_source=("block_source", "first"),
        n_trips=("trip_id", "count"),
        total_km=("distance_km", "sum"),
        start_h=("start_h", "min"),
        end_h=("end_h", "max"),
        service_id=("service_id", "first"),
        n_routes=("route_id", "nunique"),
    )
    stats["span_h"] = stats["end_h"] - stats["start_h"]
    stats["has_cross_midnight"] = stats["end_h"] >= 24.0
    return stats


def _candidate_stats(
    df: pd.DataFrame,
    *,
    n_trips_range: tuple[int, int],
    total_km_range: tuple[float, float],
    block_source: str,
    require_no_cross_midnight: bool,
    agency_top_k: int | None,
) -> pd.DataFrame:
    stats = _block_stats(df)
    keep = (
        stats["block_source"].eq(block_source)
        & stats["n_trips"].between(n_trips_range[0], n_trips_range[1], inclusive="both")
        & stats["total_km"].between(total_km_range[0], total_km_range[1], inclusive="both")
    )
    if require_no_cross_midnight:
        keep &= ~stats["has_cross_midnight"]
    stats = stats.loc[keep].copy()

    if agency_top_k is not None and agency_top_k > 0 and not stats.empty:
        top_agencies = stats["agency_id"].value_counts().head(agency_top_k).index
        stats = stats[stats["agency_id"].isin(top_agencies)]
    return stats.sort_index()


def _choose_block_id(candidates: pd.Index, rng: np.random.Generator) -> str:
    if len(candidates) == 0:
        raise ValueError("No bus blocks satisfy the requested selection criteria.")
    index = int(rng.integers(0, len(candidates)))
    return str(candidates[index])


def sample_protagonist_block(
    df: pd.DataFrame,
    rng: np.random.Generator,
    *,
    n_trips_range: tuple[int, int] = (10, 30),
    total_km_range: tuple[float, float] = (30.0, 1000.0),
    block_source: str = "native",
    require_no_cross_midnight: bool = True,
    agency_top_k: int | None = 20,
) -> str:
    stats = _candidate_stats(
        df,
        n_trips_range=n_trips_range,
        total_km_range=total_km_range,
        block_source=block_source,
        require_no_cross_midnight=require_no_cross_midnight,
        agency_top_k=agency_top_k,
    )
    return _choose_block_id(stats.index, rng)


def sample_contrast_block(
    df: pd.DataFrame,
    rng: np.random.Generator,
    protagonist_id: str,
    *,
    require_different_agency_or_km: bool = True,
    km_diff_threshold: float = 0.30,
    **kwargs,
) -> str:
    defaults = {
        "n_trips_range": (10, 30),
        "total_km_range": (30.0, 1000.0),
        "block_source": "native",
        "require_no_cross_midnight": True,
        "agency_top_k": 20,
    }
    defaults.update(kwargs)
    stats = _candidate_stats(df, **defaults)
    if protagonist_id not in stats.index:
        full_stats = _block_stats(df)
        if protagonist_id not in full_stats.index:
            raise ValueError(f"Unknown protagonist block_id: {protagonist_id}")
        protagonist = full_stats.loc[protagonist_id]
    else:
        protagonist = stats.loc[protagonist_id]

    stats = stats.drop(index=protagonist_id, errors="ignore")
    if require_different_agency_or_km:
        denom_km = max(float(protagonist["total_km"]), 1e-9)
        km_gap = (stats["total_km"] - float(protagonist["total_km"])).abs() / denom_km
        stats = stats[(stats["agency_id"] != protagonist["agency_id"]) | (km_gap >= km_diff_threshold)]
    return _choose_block_id(stats.index, rng)


def _distance_source_breakdown(block_df: pd.DataFrame) -> str:
    counts = block_df["distance_source"].value_counts(normalize=True).sort_index() * 100.0
    return ", ".join(f"{source}={value:.1f}%" for source, value in counts.items())


def _stop_continuity(block_df: pd.DataFrame) -> float:
    ordered = block_df.sort_values(["start_h", "end_h"])
    if len(ordered) <= 1:
        return float("nan")
    return float((ordered["end_stop"].shift().iloc[1:].values == ordered["start_stop"].iloc[1:].values).mean())


def render_block_identity_card(
    df: pd.DataFrame,
    block_id: str,
) -> pd.DataFrame:
    data = _ensure_distance_source(df) if "distance_source" not in df.columns else df.copy()
    block_df = data[data["block_id"].astype(str) == str(block_id)].copy()
    if block_df.empty:
        raise ValueError(f"Unknown block_id: {block_id}")

    ordered = block_df.sort_values(["start_h", "end_h"])
    service_ids = sorted(str(value) for value in ordered["service_id"].dropna().unique())
    record = {
        "block_id": str(block_id),
        "agency_id": str(ordered["agency_id"].iloc[0]),
        "n_trips": int(len(ordered)),
        "total_km": float(ordered["distance_km"].sum()),
        "span_h": float(ordered["end_h"].max() - ordered["start_h"].min()),
        "service_id": ", ".join(service_ids),
        "distance_source_breakdown": _distance_source_breakdown(ordered),
        "stop_continuity": _stop_continuity(ordered),
        "n_routes": int(ordered["route_id"].nunique()),
        "service_day_label": "a representative service day",
    }
    return pd.DataFrame([record])

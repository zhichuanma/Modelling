"""Data loading and quality summaries for the bus block table."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from mobility.core.spatial import (
    DEFAULT_ONSPD_PATH,
    load_lsoa_boundary_index,
    load_lsoa_centroids,
    nearest_lsoa_for_points,
    query_lsoa_polygons,
)


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


def attach_lsoa(
    blocks: pd.DataFrame,
    *,
    onspd_path: Path | None = None,
    centroids: pd.DataFrame | None = None,
    boundary_index: dict | None = None,
    boundary_paths: tuple | None = None,
    max_distance_km: float | None = 5.0,
) -> pd.DataFrame:
    """Spatially join bus trip endpoints to LSOA/Data Zone codes."""
    required_columns = ("start_lat", "start_lon", "end_lat", "end_lon")
    missing = [col for col in required_columns if col not in blocks.columns]
    if missing:
        raise ValueError(f"blocks is missing required coordinate columns: {missing}")

    source_path = DEFAULT_ONSPD_PATH if onspd_path is None else Path(onspd_path)
    out = blocks.copy()
    start_lat = out["start_lat"].to_numpy(dtype=float)
    start_lon = out["start_lon"].to_numpy(dtype=float)
    end_lat = out["end_lat"].to_numpy(dtype=float)
    end_lon = out["end_lon"].to_numpy(dtype=float)
    all_lat = np.concatenate([start_lat, end_lat])
    all_lon = np.concatenate([start_lon, end_lon])
    valid = np.isfinite(all_lat) & np.isfinite(all_lon)

    all_codes = np.full(all_lat.shape[0], "", dtype=object)
    all_sources = np.full(all_lat.shape[0], "no_match", dtype=object)
    all_methods = np.full(all_lat.shape[0], "no_match", dtype=object)
    all_distances = np.full(all_lat.shape[0], np.nan, dtype=float)

    if valid.any():
        coord_frame = (
            pd.DataFrame({"lat": all_lat[valid], "lon": all_lon[valid]})
            .drop_duplicates(ignore_index=True)
        )
        unique_lat = coord_frame["lat"].to_numpy(dtype=float)
        unique_lon = coord_frame["lon"].to_numpy(dtype=float)

        use_polygon = boundary_index is not None or boundary_paths is not None or centroids is None
        if use_polygon:
            polygon_index = (
                load_lsoa_boundary_index(boundary_paths)
                if boundary_index is None and boundary_paths is not None
                else boundary_index
            )
            if polygon_index is None:
                polygon_index = load_lsoa_boundary_index()
            unique_codes, unique_sources, unique_methods = query_lsoa_polygons(
                unique_lat,
                unique_lon,
                polygon_index,
            )
        else:
            unique_codes = np.full(unique_lat.shape[0], "", dtype=object)
            unique_sources = np.full(unique_lat.shape[0], "no_match", dtype=object)
            unique_methods = np.full(unique_lat.shape[0], "no_match", dtype=object)

        unique_distances = np.full(unique_lat.shape[0], np.nan, dtype=float)
        fallback_mask = unique_methods == "no_match"
        if fallback_mask.any():
            try:
                centroid_frame = load_lsoa_centroids(source_path) if centroids is None else centroids.copy()
                fallback_codes, fallback_distances = nearest_lsoa_for_points(
                    unique_lat[fallback_mask],
                    unique_lon[fallback_mask],
                    centroid_frame,
                    max_distance_km=max_distance_km,
                )
            except (FileNotFoundError, KeyError, ValueError, pd.errors.EmptyDataError):
                fallback_codes = np.full(int(fallback_mask.sum()), "", dtype=object)
                fallback_distances = np.full(int(fallback_mask.sum()), np.nan, dtype=float)
            target = np.flatnonzero(fallback_mask)
            if fallback_codes.size:
                unique_codes[target] = fallback_codes
                unique_distances[target] = fallback_distances
                matched = fallback_codes != ""
                unique_sources[target[matched]] = "centroid_fallback"
                unique_methods[target[matched]] = "centroid_fallback"
        unique_distances[unique_methods == "polygon"] = 0.0

        mapping = {
            (float(lat), float(lon)): (code, source, method, distance)
            for lat, lon, code, source, method, distance in zip(
                unique_lat,
                unique_lon,
                unique_codes,
                unique_sources,
                unique_methods,
                unique_distances,
            )
        }
        valid_positions = np.flatnonzero(valid)
        for pos in valid_positions:
            code, source, method, distance = mapping[(float(all_lat[pos]), float(all_lon[pos]))]
            all_codes[pos] = code
            all_sources[pos] = source
            all_methods[pos] = method
            all_distances[pos] = distance

    split = len(out)
    start_lsoa = all_codes[:split]
    end_lsoa = all_codes[split:]
    start_distance_km = all_distances[:split]
    end_distance_km = all_distances[split:]

    out["start_lsoa"] = start_lsoa
    out["end_lsoa"] = end_lsoa
    out["start_lsoa_source"] = all_sources[:split]
    out["start_lsoa_match_method"] = all_methods[:split]
    out["end_lsoa_source"] = all_sources[split:]
    out["end_lsoa_match_method"] = all_methods[split:]
    out["start_lsoa_distance_km"] = start_distance_km
    out["end_lsoa_distance_km"] = end_distance_km
    valid_methods = all_methods[valid]
    valid_sources = all_sources[valid]
    denominator = int(valid_methods.shape[0])

    def method_pct(method: str) -> float:
        return _pct(int((valid_methods == method).sum()), denominator)

    source_names = ("EW_LSOA21", "Scotland_DZ2022", "NI_DZ2021", "centroid_fallback", "no_match")
    source_breakdown = {
        name: _pct(int((valid_sources == name).sum()), denominator)
        for name in source_names
    }
    fallback_distances = all_distances[valid & (all_methods == "centroid_fallback")]
    out.attrs["lsoa_join"] = {
        "onspd_path": str(source_path) if centroids is None else str(onspd_path or ""),
        "max_distance_km": None if max_distance_km is None else float(max_distance_km),
        "n_unmatched": int(((out["start_lsoa"] == "") | (out["end_lsoa"] == "")).sum()),
        "method": "polygon_with_centroid_fallback",
        "valid_coordinate_assignments": denominator,
        "polygon_pct": method_pct("polygon"),
        "centroid_fallback_pct": method_pct("centroid_fallback"),
        "no_match_pct": method_pct("no_match"),
        "max_centroid_fallback_km": float(np.nanmax(fallback_distances)) if fallback_distances.size else np.nan,
        "source_breakdown": source_breakdown,
    }
    return out


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

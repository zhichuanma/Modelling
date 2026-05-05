"""Spatial helpers for LSOA centroids and OD distances.

Units:
- `_m` suffix means meter in British National Grid (EPSG:27700).
- `_km` suffix means kilometer derived from Euclidean BNG distances.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

DEFAULT_ONSPD_PATH = (
    Path(__file__).resolve().parents[3] / "Data" / "Units" / "ONSPD_MAY_2025_UK.csv"
)


def load_lsoa_centroids(onspd_path: Path | None = None) -> pd.DataFrame:
    """Load LSOA centroids from ONSPD postcode points."""
    source_path = DEFAULT_ONSPD_PATH if onspd_path is None else Path(onspd_path)
    raw = pd.read_csv(
        source_path,
        usecols=["lsoa21", "oseast1m", "osnrth1m", "lat", "long"],
        dtype={"lsoa21": "string"},
    )

    centroids = raw.rename(
        columns={
            "lsoa21": "lsoa_code",
            "oseast1m": "easting_m",
            "osnrth1m": "northing_m",
            "long": "lon",
        }
    ).copy()
    centroids["lsoa_code"] = centroids["lsoa_code"].str.strip()
    for col in ("easting_m", "northing_m", "lat", "lon"):
        centroids[col] = pd.to_numeric(centroids[col], errors="coerce")

    valid_rows = (
        centroids["lsoa_code"].notna()
        & centroids["lsoa_code"].ne("")
        & centroids["easting_m"].notna()
        & centroids["northing_m"].notna()
        & centroids["lat"].notna()
        & centroids["lon"].notna()
        & centroids["easting_m"].ne(0.0)
    )
    grouped = (
        centroids.loc[valid_rows, ["lsoa_code", "easting_m", "northing_m", "lat", "lon"]]
        .groupby("lsoa_code", as_index=False, sort=True)[["easting_m", "northing_m", "lat", "lon"]]
        .mean()
        .sort_values("lsoa_code", kind="stable")
        .reset_index(drop=True)
    )

    grouped["lsoa_code"] = grouped["lsoa_code"].astype(object)
    for col in ("easting_m", "northing_m", "lat", "lon"):
        grouped[col] = grouped[col].astype(float)
    return grouped


def nearest_lsoa_for_points(
    lat: np.ndarray,
    lon: np.ndarray,
    centroids: pd.DataFrame,
    *,
    max_distance_km: float | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Return nearest LSOA-21 centroid codes for WGS84 points."""
    lat_array = np.asarray(lat, dtype=float)
    lon_array = np.asarray(lon, dtype=float)
    if lat_array.ndim != 1 or lon_array.ndim != 1:
        raise ValueError("lat and lon must be 1D arrays.")
    if lat_array.shape[0] != lon_array.shape[0]:
        raise ValueError("lat and lon must have the same length.")

    n_points = lat_array.shape[0]
    codes = np.full(n_points, "", dtype=object)
    distances_km = np.full(n_points, np.nan, dtype=float)
    if n_points == 0:
        return codes, distances_km

    valid_points = np.isfinite(lat_array) & np.isfinite(lon_array)
    if not valid_points.any():
        return codes, distances_km

    centroid_frame = _centroids_for_nearest(centroids)
    centroid_codes = centroid_frame["lsoa_code"].to_numpy(dtype=object)
    query_lat = lat_array[valid_points]
    query_lon = lon_array[valid_points]

    projected = _nearest_with_projected_kdtree(query_lat, query_lon, centroid_frame)
    if projected is None:
        nearest_index, query_distances_km = _nearest_with_haversine(query_lat, query_lon, centroid_frame)
    else:
        nearest_index, query_distances_km = projected

    query_codes = centroid_codes[nearest_index].astype(object)
    if max_distance_km is not None:
        too_far = query_distances_km > float(max_distance_km)
        query_codes[too_far] = ""

    codes[valid_points] = query_codes
    distances_km[valid_points] = query_distances_km
    return codes, distances_km


def od_distance_km(
    a_lsoa: str,
    b_lsoa: str,
    centroids: pd.DataFrame,
    intra_km: float = 0.5,
) -> float:
    """Return the BNG Euclidean centroid distance in kilometers."""
    if a_lsoa == b_lsoa:
        return float(intra_km)

    indexed = _indexed_centroids(centroids)
    pair = indexed.loc[[a_lsoa, b_lsoa], ["easting_m", "northing_m"]].to_numpy(dtype=float)
    delta_m = pair[0] - pair[1]
    return float(np.sqrt(np.square(delta_m).sum()) / 1000.0)


def od_distance_matrix(
    codes: Sequence[str],
    centroids: pd.DataFrame,
    intra_km: float = 0.5,
) -> np.ndarray:
    """Return a vectorized `(N, N)` OD distance matrix in kilometers."""
    code_array = np.asarray(codes, dtype=object)
    indexed = _indexed_centroids(centroids)
    points = indexed.loc[list(code_array), ["easting_m", "northing_m"]].to_numpy(dtype=float)
    easting_m = points[:, 0]
    northing_m = points[:, 1]

    delta_e_m = np.subtract.outer(easting_m, easting_m)
    delta_n_m = np.subtract.outer(northing_m, northing_m)
    distance_km = np.sqrt(np.square(delta_e_m) + np.square(delta_n_m)) / 1000.0
    distance_km[np.equal.outer(code_array, code_array)] = float(intra_km)
    return distance_km


def _indexed_centroids(centroids: pd.DataFrame) -> pd.DataFrame:
    if "lsoa_code" in centroids.columns:
        indexed = centroids.set_index("lsoa_code", drop=True)
    else:
        indexed = centroids

    missing_columns = {"easting_m", "northing_m"} - set(indexed.columns)
    if missing_columns:
        raise KeyError(f"centroids is missing required columns: {sorted(missing_columns)}")

    if indexed.index.has_duplicates:
        raise ValueError("centroids must have unique lsoa_code values")

    return indexed.loc[:, ["easting_m", "northing_m"]]


def _centroids_for_nearest(centroids: pd.DataFrame) -> pd.DataFrame:
    if "lsoa_code" in centroids.columns:
        frame = centroids.copy()
    else:
        frame = centroids.reset_index().rename(
            columns={centroids.index.name or "index": "lsoa_code"}
        )

    missing_columns = {"lsoa_code", "easting_m", "northing_m"} - set(frame.columns)
    if missing_columns:
        raise KeyError(f"centroids is missing required columns: {sorted(missing_columns)}")

    for col in ("easting_m", "northing_m"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    valid_rows = frame["lsoa_code"].notna() & frame["easting_m"].notna() & frame["northing_m"].notna()

    if {"lat", "lon"}.issubset(frame.columns):
        for col in ("lat", "lon"):
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
        valid_rows &= frame["lat"].notna() & frame["lon"].notna()

    out = frame.loc[valid_rows].copy()
    if out.empty:
        raise ValueError("centroids has no valid rows for nearest-LSOA lookup.")
    if out["lsoa_code"].duplicated().any():
        raise ValueError("centroids must have unique lsoa_code values")
    out["lsoa_code"] = out["lsoa_code"].astype(object)
    return out.reset_index(drop=True)


def _nearest_with_projected_kdtree(
    lat: np.ndarray,
    lon: np.ndarray,
    centroids: pd.DataFrame,
) -> tuple[np.ndarray, np.ndarray] | None:
    try:
        from pyproj import Transformer
        from scipy.spatial import cKDTree
    except ImportError:
        return None

    transformer = Transformer.from_crs(4326, 27700, always_xy=True)
    point_easting_m, point_northing_m = transformer.transform(lon, lat)
    points = np.column_stack([point_easting_m, point_northing_m])
    finite_projected = np.isfinite(points).all(axis=1)
    if not finite_projected.all():
        return None

    centroid_xy = centroids[["easting_m", "northing_m"]].to_numpy(dtype=float)
    tree = cKDTree(centroid_xy)
    distances_m, nearest_index = tree.query(points, k=1)
    return nearest_index.astype(int), distances_m.astype(float) / 1000.0


def _nearest_with_haversine(
    lat: np.ndarray,
    lon: np.ndarray,
    centroids: pd.DataFrame,
) -> tuple[np.ndarray, np.ndarray]:
    missing_columns = {"lat", "lon"} - set(centroids.columns)
    if missing_columns:
        raise KeyError(
            "centroids must include lat/lon when pyproj is unavailable; "
            f"missing: {sorted(missing_columns)}"
        )

    try:
        from sklearn.neighbors import BallTree
    except ImportError:
        return _nearest_with_numpy_haversine(lat, lon, centroids)

    earth_radius_km = 6371.0088
    centroid_rad = np.radians(centroids[["lat", "lon"]].to_numpy(dtype=float))
    query_rad = np.radians(np.column_stack([lat, lon]))
    tree = BallTree(centroid_rad, metric="haversine")
    distances_rad, nearest_index = tree.query(query_rad, k=1)
    return nearest_index[:, 0].astype(int), distances_rad[:, 0].astype(float) * earth_radius_km


def _nearest_with_numpy_haversine(
    lat: np.ndarray,
    lon: np.ndarray,
    centroids: pd.DataFrame,
) -> tuple[np.ndarray, np.ndarray]:
    earth_radius_km = 6371.0088
    centroid_lat_rad = np.radians(centroids["lat"].to_numpy(dtype=float))
    centroid_lon_rad = np.radians(centroids["lon"].to_numpy(dtype=float))
    query_lat_rad = np.radians(lat)
    query_lon_rad = np.radians(lon)
    nearest_index = np.empty(lat.shape[0], dtype=int)
    nearest_distance_km = np.empty(lat.shape[0], dtype=float)

    for start in range(0, lat.shape[0], 1024):
        stop = min(start + 1024, lat.shape[0])
        delta_lat = query_lat_rad[start:stop, None] - centroid_lat_rad[None, :]
        delta_lon = query_lon_rad[start:stop, None] - centroid_lon_rad[None, :]
        a = (
            np.sin(delta_lat / 2.0) ** 2
            + np.cos(query_lat_rad[start:stop, None])
            * np.cos(centroid_lat_rad[None, :])
            * np.sin(delta_lon / 2.0) ** 2
        )
        distances = 2.0 * earth_radius_km * np.arcsin(np.minimum(1.0, np.sqrt(a)))
        nearest_index[start:stop] = np.argmin(distances, axis=1)
        nearest_distance_km[start:stop] = distances[
            np.arange(stop - start),
            nearest_index[start:stop],
        ]

    return nearest_index, nearest_distance_km

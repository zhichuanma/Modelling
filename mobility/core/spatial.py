"""Spatial helpers for LSOA centroids and OD distances.

Units:
- `_m` suffix means meter in British National Grid (EPSG:27700).
- `_km` suffix means kilometer derived from Euclidean BNG distances.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

MODELLING_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = MODELLING_ROOT.parent
DATA_LOADS = PROJECT_ROOT / "Data" / "Loads"
DEFAULT_ONSPD_PATH = (
    PROJECT_ROOT / "Data" / "Units" / "ONSPD_MAY_2025_UK.csv"
)
LSOA_BOUNDARY_PATHS = (
    (
        DATA_LOADS / "Lower_layer_Super_Output_Areas_December_2021_Boundaries_EW_BSC_V4_-4299016806856585929.geojson",
        "LSOA21CD",
        "EW_LSOA21",
    ),
    (
        DATA_LOADS / "SG_DataZone_Bdry_2022.geojson",
        "dzcode",
        "Scotland_DZ2022",
    ),
    (
        DATA_LOADS / "DZ2021.geojson",
        "DZ2021_cd",
        "NI_DZ2021",
    ),
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
    if {"lat", "lon"}.issubset(centroids.columns):
        centroid_lon = centroids["lon"].to_numpy(dtype=float)
        centroid_lat = centroids["lat"].to_numpy(dtype=float)
        centroid_easting_m, centroid_northing_m = transformer.transform(centroid_lon, centroid_lat)
        projected_centroids = np.column_stack([centroid_easting_m, centroid_northing_m])
        declared_centroids = centroids[["easting_m", "northing_m"]].to_numpy(dtype=float)
        if (
            not np.isfinite(projected_centroids).all()
            or not np.isfinite(declared_centroids).all()
            or np.nanmax(np.linalg.norm(projected_centroids - declared_centroids, axis=1)) > 1000.0
        ):
            # Small hand-built centroid fixtures can carry approximate or dummy
            # BNG coordinates; fall back to the lat/lon path instead of
            # returning distorted nearest-neighbour distances.
            return None

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


def load_lsoa_boundary_index(paths: tuple = LSOA_BOUNDARY_PATHS) -> dict:
    """Load UK LSOA/Data Zone GeoJSON boundaries with stdlib JSON only."""
    codes: list[str] = []
    sources: list[str] = []
    bboxes: list[tuple[float, float, float, float]] = []
    areas: list[float] = []
    polygons: list[list[list[np.ndarray]]] = []

    for path_value, code_column, source in paths:
        path = Path(path_value)
        if not path.exists():
            warnings.warn(f"Boundary file missing, skipping {source}: {path}", RuntimeWarning, stacklevel=2)
            continue
        data = json.loads(path.read_text())
        features = data.get("features", [])
        if not _geojson_looks_lonlat(features):
            warnings.warn(
                f"Boundary file does not look like EPSG:4326 lon/lat, skipping {source}: {path}",
                RuntimeWarning,
                stacklevel=2,
            )
            continue

        for feature in features:
            code = str(feature.get("properties", {}).get(code_column, "")).strip()
            geometry = feature.get("geometry") or {}
            feature_polygons = _geometry_to_polygons(geometry)
            if not code or not feature_polygons:
                continue
            bbox = _polygon_bbox(feature_polygons)
            if bbox is None:
                continue
            codes.append(code)
            sources.append(str(source))
            bboxes.append(bbox)
            areas.append(_multipolygon_area(feature_polygons))
            polygons.append(feature_polygons)

    bbox_array = np.asarray(bboxes, dtype=float).reshape((-1, 4))
    return {
        "codes": np.asarray(codes, dtype=object),
        "sources": np.asarray(sources, dtype=object),
        "bboxes": bbox_array,
        "areas": np.asarray(areas, dtype=float),
        "polygons": polygons,
        "bbox_grid": _build_bbox_grid(bbox_array),
    }


def query_lsoa_polygons(
    lats: np.ndarray,
    lons: np.ndarray,
    index: dict,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Point-in-polygon lookup without centroid fallback."""
    lat_array = np.asarray(lats, dtype=float)
    lon_array = np.asarray(lons, dtype=float)
    if lat_array.ndim != 1 or lon_array.ndim != 1:
        raise ValueError("lats and lons must be 1D arrays.")
    if lat_array.shape[0] != lon_array.shape[0]:
        raise ValueError("lats and lons must have the same length.")

    n_points = lat_array.shape[0]
    out_codes = np.full(n_points, "", dtype=object)
    out_sources = np.full(n_points, "no_match", dtype=object)
    out_methods = np.full(n_points, "no_match", dtype=object)
    bboxes = np.asarray(index.get("bboxes", np.empty((0, 4))), dtype=float).reshape((-1, 4))
    if n_points == 0 or bboxes.shape[0] == 0:
        return out_codes, out_sources, out_methods

    codes = np.asarray(index["codes"], dtype=object)
    sources = np.asarray(index["sources"], dtype=object)
    areas = np.asarray(index["areas"], dtype=float)
    polygons = index["polygons"]

    valid = np.isfinite(lat_array) & np.isfinite(lon_array)
    for point_index in np.flatnonzero(valid):
        x = float(lon_array[point_index])
        y = float(lat_array[point_index])
        bbox_hits = _grid_bbox_candidates(x, y, index.get("bbox_grid"))
        if bbox_hits is None:
            bbox_hits = np.flatnonzero(
                (bboxes[:, 0] <= x)
                & (x <= bboxes[:, 2])
                & (bboxes[:, 1] <= y)
                & (y <= bboxes[:, 3])
            )
        else:
            bbox_hits = np.asarray(
                [
                    idx
                    for idx in bbox_hits
                    if bboxes[idx, 0] <= x <= bboxes[idx, 2] and bboxes[idx, 1] <= y <= bboxes[idx, 3]
                ],
                dtype=int,
            )
        matches = [
            feature_index
            for feature_index in bbox_hits
            if _point_in_multipolygon(x, y, polygons[feature_index])
        ]
        if not matches:
            continue
        best = min(matches, key=lambda idx: (float(areas[idx]), str(codes[idx])))
        out_codes[point_index] = str(codes[best])
        out_sources[point_index] = str(sources[best])
        out_methods[point_index] = "polygon"

    return out_codes, out_sources, out_methods


def _build_bbox_grid(bboxes: np.ndarray, *, cell_size: float = 0.25) -> dict:
    if bboxes.size == 0:
        return {"cells": {}, "cell_size": float(cell_size), "origin_lon": 0.0, "origin_lat": 0.0}
    origin_lon = float(np.nanmin(bboxes[:, 0]))
    origin_lat = float(np.nanmin(bboxes[:, 1]))
    cells: dict[tuple[int, int], list[int]] = {}
    for idx, (min_lon, min_lat, max_lon, max_lat) in enumerate(bboxes):
        x0 = int(np.floor((float(min_lon) - origin_lon) / cell_size))
        x1 = int(np.floor((float(max_lon) - origin_lon) / cell_size))
        y0 = int(np.floor((float(min_lat) - origin_lat) / cell_size))
        y1 = int(np.floor((float(max_lat) - origin_lat) / cell_size))
        for gx in range(x0, x1 + 1):
            for gy in range(y0, y1 + 1):
                cells.setdefault((gx, gy), []).append(idx)
    return {
        "cells": cells,
        "cell_size": float(cell_size),
        "origin_lon": origin_lon,
        "origin_lat": origin_lat,
    }


def _grid_bbox_candidates(x: float, y: float, grid: dict | None) -> list[int] | None:
    if not grid:
        return None
    cell_size = float(grid["cell_size"])
    gx = int(np.floor((x - float(grid["origin_lon"])) / cell_size))
    gy = int(np.floor((y - float(grid["origin_lat"])) / cell_size))
    return grid["cells"].get((gx, gy), [])


def _geometry_to_polygons(geometry: dict) -> list[list[np.ndarray]]:
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "Polygon":
        return [_rings_to_arrays(coordinates)]
    if geometry_type == "MultiPolygon":
        return [_rings_to_arrays(polygon) for polygon in coordinates or []]
    return []


def _rings_to_arrays(rings) -> list[np.ndarray]:
    out: list[np.ndarray] = []
    for ring in rings or []:
        arr = np.asarray(ring, dtype=float)
        if arr.ndim == 2 and arr.shape[0] >= 3 and arr.shape[1] >= 2:
            out.append(arr[:, :2])
    return out


def _polygon_bbox(polygons: list[list[np.ndarray]]) -> tuple[float, float, float, float] | None:
    arrays = [ring for polygon in polygons for ring in polygon if ring.size]
    if not arrays:
        return None
    coords = np.vstack(arrays)
    return (
        float(np.nanmin(coords[:, 0])),
        float(np.nanmin(coords[:, 1])),
        float(np.nanmax(coords[:, 0])),
        float(np.nanmax(coords[:, 1])),
    )


def _multipolygon_area(polygons: list[list[np.ndarray]]) -> float:
    area = 0.0
    for polygon in polygons:
        if not polygon:
            continue
        area += abs(_ring_signed_area(polygon[0]))
        for hole in polygon[1:]:
            area -= abs(_ring_signed_area(hole))
    return float(max(area, 0.0))


def _ring_signed_area(ring: np.ndarray) -> float:
    x = ring[:, 0]
    y = ring[:, 1]
    return float(0.5 * np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def _geojson_looks_lonlat(features: list[dict]) -> bool:
    checked = 0
    for feature in features:
        geometry = feature.get("geometry") or {}
        for x, y in _iter_geojson_xy(geometry.get("coordinates")):
            if not (-12.0 <= float(x) <= 4.0 and 48.0 <= float(y) <= 62.5):
                return False
            checked += 1
            if checked >= 10:
                return True
    return checked > 0


def _iter_geojson_xy(coordinates):
    if not isinstance(coordinates, list) or not coordinates:
        return
    if len(coordinates) >= 2 and all(isinstance(value, (int, float)) for value in coordinates[:2]):
        yield float(coordinates[0]), float(coordinates[1])
        return
    for item in coordinates:
        yield from _iter_geojson_xy(item)


def _point_on_segment(x, y, x1, y1, x2, y2, *, eps=1e-12) -> bool:
    cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
    if abs(cross) > eps:
        return False
    return (
        min(x1, x2) - eps <= x <= max(x1, x2) + eps
        and min(y1, y2) - eps <= y <= max(y1, y2) + eps
    )


def _point_in_ring(x, y, ring: np.ndarray) -> bool:
    inside = False
    n = int(ring.shape[0])
    for i in range(n):
        x1, y1 = float(ring[i, 0]), float(ring[i, 1])
        x2, y2 = float(ring[(i + 1) % n, 0]), float(ring[(i + 1) % n, 1])
        if _point_on_segment(x, y, x1, y1, x2, y2):
            return True
        intersects = (y1 > y) != (y2 > y)
        if intersects:
            x_intersection = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x <= x_intersection:
                inside = not inside
    return inside


def _point_in_polygon_with_holes(x, y, rings: list[np.ndarray]) -> bool:
    if not rings or not _point_in_ring(x, y, rings[0]):
        return False
    return not any(_point_in_ring(x, y, hole) for hole in rings[1:])


def _point_in_multipolygon(x, y, polygons: list[list[np.ndarray]]) -> bool:
    return any(_point_in_polygon_with_holes(x, y, polygon) for polygon in polygons)

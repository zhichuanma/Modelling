"""Distance estimation for coach vehicle journeys."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

import numpy as np
import pandas as pd


EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return great-circle distance between two WGS84 points in kilometres."""
    phi1 = math.radians(float(lat1))
    phi2 = math.radians(float(lat2))
    d_phi = math.radians(float(lat2) - float(lat1))
    d_lambda = math.radians(float(lon2) - float(lon1))
    a = (
        math.sin(d_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
    )
    return float(2.0 * EARTH_RADIUS_KM * math.asin(math.sqrt(a)))


def _stop_refs(stop_seq: Any) -> list[str]:
    if isinstance(stop_seq, pd.DataFrame):
        if "stop_point_ref" not in stop_seq.columns:
            raise ValueError("stop_seq DataFrame must include stop_point_ref.")
        values = stop_seq.sort_values("stop_sequence")["stop_point_ref"] if "stop_sequence" in stop_seq.columns else stop_seq["stop_point_ref"]
        return [str(value).strip() for value in values if str(value).strip()]
    if isinstance(stop_seq, pd.Series):
        return [str(stop_seq["stop_point_ref"]).strip()]
    if isinstance(stop_seq, Iterable) and not isinstance(stop_seq, (str, bytes)):
        return [str(value).strip() for value in stop_seq if str(value).strip()]
    raise TypeError("stop_seq must be a DataFrame, Series, or iterable of stop refs.")


def build_coords_lookup(stops_geom: pd.DataFrame) -> dict[str, tuple[float, float]]:
    """Build a stop_point_ref -> (lat, lon) dict, dropping rows with non-finite coords.

    Hoist this once outside the per-journey loop to avoid repeated DataFrame
    indexing on the full NaPTAN+custom union.
    """
    required = {"stop_point_ref", "lat", "lon"}
    missing = required - set(stops_geom.columns)
    if missing:
        raise ValueError(f"stops_geom is missing required columns: {sorted(missing)}")
    if stops_geom.empty:
        return {}
    deduped = stops_geom.drop_duplicates("stop_point_ref", keep="last")
    refs = deduped["stop_point_ref"].astype(str).to_numpy()
    lats = pd.to_numeric(deduped["lat"], errors="coerce").to_numpy(dtype=float)
    lons = pd.to_numeric(deduped["lon"], errors="coerce").to_numpy(dtype=float)
    finite = np.isfinite(lats) & np.isfinite(lons)
    return {
        str(ref): (float(lat), float(lon))
        for ref, lat, lon, ok in zip(refs, lats, lons, finite)
        if ok
    }


def vehicle_journey_distance_km(
    stop_seq: Any,
    stops_geom: pd.DataFrame,
    road_detour_factor: float = 1.30,
    *,
    coords: dict[str, tuple[float, float]] | None = None,
) -> tuple[float | None, str]:
    """Estimate a vehicle journey distance from adjacent stop coordinates.

    Pass a precomputed ``coords`` dict (from :func:`build_coords_lookup`) when
    iterating over many journeys to avoid rebuilding the lookup each call.
    """
    if road_detour_factor <= 0.0:
        raise ValueError("road_detour_factor must be positive.")
    if coords is None:
        if stops_geom.empty:
            return None, "unknown"
        coords = build_coords_lookup(stops_geom)
    if not coords:
        return None, "unknown"

    refs = _stop_refs(stop_seq)
    if len(refs) < 2:
        return 0.0, "haversine_x_detour"

    total_haversine_km = 0.0
    for left, right in zip(refs[:-1], refs[1:]):
        left_coords = coords.get(left)
        right_coords = coords.get(right)
        if left_coords is None or right_coords is None:
            return None, "unknown"
        total_haversine_km += haversine_km(
            left_coords[0], left_coords[1], right_coords[0], right_coords[1]
        )

    return float(total_haversine_km * road_detour_factor), "haversine_x_detour"

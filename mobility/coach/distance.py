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


def vehicle_journey_distance_km(
    stop_seq: Any,
    stops_geom: pd.DataFrame,
    road_detour_factor: float = 1.30,
) -> tuple[float | None, str]:
    """Estimate a vehicle journey distance from adjacent stop coordinates."""
    if road_detour_factor <= 0.0:
        raise ValueError("road_detour_factor must be positive.")
    if stops_geom.empty:
        return None, "unknown"
    required = {"stop_point_ref", "lat", "lon"}
    missing = required - set(stops_geom.columns)
    if missing:
        raise ValueError(f"stops_geom is missing required columns: {sorted(missing)}")

    refs = _stop_refs(stop_seq)
    if len(refs) < 2:
        return 0.0, f"haversine_x{road_detour_factor:.2f}"

    coords = (
        stops_geom.drop_duplicates("stop_point_ref", keep="last")
        .set_index("stop_point_ref")[["lat", "lon"]]
        .apply(pd.to_numeric, errors="coerce")
    )

    total_haversine_km = 0.0
    for left, right in zip(refs[:-1], refs[1:]):
        if left not in coords.index or right not in coords.index:
            return None, "unknown"
        lat1, lon1 = coords.loc[left, ["lat", "lon"]]
        lat2, lon2 = coords.loc[right, ["lat", "lon"]]
        values = np.array([lat1, lon1, lat2, lon2], dtype=float)
        if not np.isfinite(values).all():
            return None, "unknown"
        total_haversine_km += haversine_km(lat1, lon1, lat2, lon2)

    return float(total_haversine_km * road_detour_factor), f"haversine_x{road_detour_factor:.2f}"

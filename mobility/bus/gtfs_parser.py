"""Reusable helpers for bus mobility modelling.

Kept small on purpose: the notebook embeds the algorithms inline for teaching,
this module just holds the stable pieces used across scripts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


# -------- time & distance --------
def parse_gtfs_time(s: str) -> float:
    """HH:MM:SS → decimal hours. GTFS allows >24h (e.g. 25:30:00 = next-day 01:30)."""
    if not isinstance(s, str):
        return np.nan
    h, m, sec = s.split(":")
    return int(h) + int(m) / 60.0 + int(sec) / 3600.0


def format_hhmm(h) -> str:
    """Decimal hours → 'HH:MM'. Preserves >24h for overnight (e.g. 25.5 → '25:30')."""
    if h is None or (isinstance(h, float) and np.isnan(h)):
        return ""
    total_min = int(round(float(h) * 60))
    return f"{total_min // 60:02d}:{total_min % 60:02d}"


def haversine_km(lat1, lon1, lat2, lon2):
    """Vectorised great-circle distance in km. Accepts scalars or numpy arrays."""
    R = 6371.0088
    lat1 = np.radians(lat1); lon1 = np.radians(lon1)
    lat2 = np.radians(lat2); lon2 = np.radians(lon2)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return 2 * R * np.arcsin(np.sqrt(a))


# -------- big-file streaming filters --------
def filter_stop_times(
    path: Path,
    trip_ids: set[str],
    chunk_rows: int = 2_000_000,
) -> pd.DataFrame:
    cols = ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"]
    parts = []
    for chunk in pd.read_csv(path, usecols=cols, chunksize=chunk_rows, low_memory=False):
        sub = chunk[chunk["trip_id"].isin(trip_ids)]
        if len(sub):
            parts.append(sub)
    st = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=cols)
    st["dep_h"] = st["departure_time"].map(parse_gtfs_time)
    st["arr_h"] = st["arrival_time"].map(parse_gtfs_time)
    return st


def filter_shapes(path: Path, shape_ids: set[str], chunk_rows: int = 2_000_000) -> pd.DataFrame:
    parts = []
    for chunk in pd.read_csv(path, chunksize=chunk_rows, low_memory=False):
        sub = chunk[chunk["shape_id"].isin(shape_ids)]
        if len(sub):
            parts.append(sub)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


# -------- distance per shape / per trip (fallback) --------
def shape_length_km(shapes: pd.DataFrame) -> pd.Series:
    s = shapes.sort_values(["shape_id", "shape_pt_sequence"]).copy()
    s["lat2"] = s.groupby("shape_id")["shape_pt_lat"].shift(-1)
    s["lon2"] = s.groupby("shape_id")["shape_pt_lon"].shift(-1)
    mask = s["lat2"].notna()
    s["seg_km"] = 0.0
    s.loc[mask, "seg_km"] = haversine_km(
        s.loc[mask, "shape_pt_lat"].values,
        s.loc[mask, "shape_pt_lon"].values,
        s.loc[mask, "lat2"].values,
        s.loc[mask, "lon2"].values,
    )
    return s.groupby("shape_id")["seg_km"].sum()


def stop_trip_length_km(st: pd.DataFrame, stops: pd.DataFrame) -> pd.Series:
    merged = st.merge(stops[["stop_id", "stop_lat", "stop_lon"]], on="stop_id", how="left")
    merged = merged.sort_values(["trip_id", "stop_sequence"])
    merged["lat2"] = merged.groupby("trip_id")["stop_lat"].shift(-1)
    merged["lon2"] = merged.groupby("trip_id")["stop_lon"].shift(-1)
    mask = merged["lat2"].notna()
    merged["seg_km"] = 0.0
    merged.loc[mask, "seg_km"] = haversine_km(
        merged.loc[mask, "stop_lat"].values,
        merged.loc[mask, "stop_lon"].values,
        merged.loc[mask, "lat2"].values,
        merged.loc[mask, "lon2"].values,
    )
    return merged.groupby("trip_id")["seg_km"].sum()


# -------- trip span (first/last stop) --------
def build_trip_span(st: pd.DataFrame) -> pd.DataFrame:
    ordered = st.sort_values(["trip_id", "stop_sequence"])
    first = ordered.groupby("trip_id").first()[["dep_h", "stop_id"]]
    first.columns = ["start_h", "start_stop"]
    last = ordered.groupby("trip_id").last()[["arr_h", "stop_id"]]
    last.columns = ["end_h", "end_stop"]
    return first.join(last)


# -------- block inference --------
def infer_blocks(
    sched: pd.DataFrame,
    max_layover_h: float = 1.0,
    max_deadhead_km: float = 1.0,
    same_stop_bonus_h: float = 0.5,
    route_continuity_bonus_h: float = 0.25,
    max_shift_h: float = 10.0,
) -> pd.Series:
    """Greedy vehicle assignment — reconstruct block_id for trips that lack it.

    For each new trip, we score every candidate bus by "effective wait time":

        effective_wait = actual_wait
                        - same_stop_bonus_h       if prev_end_stop == this_start_stop
                        - route_continuity_bonus_h if same route_id (return-trip pattern)

    and pick the bus with the smallest effective wait (subject to hard limits on
    layover time and deadhead distance). This prefers buses that end EXACTLY
    where the new trip starts and stay on their own route, instead of blindly
    hopping onto whatever bus was free earliest — which is what causes parallel
    services to collapse into fewer blocks than reality.

    Required columns in `sched`:
        trip_id, agency_id, service_id, start_h, end_h,
        start_lat, start_lon, end_lat, end_lon,
        start_stop, end_stop, route_id
    """
    sched = sched.sort_values(["agency_id", "service_id", "start_h"]).reset_index(drop=False)
    n = len(sched)
    inferred = np.empty(n, dtype=object)
    block_counter = 0

    start_h = sched["start_h"].values
    end_h = sched["end_h"].values
    s_lat = sched["start_lat"].values
    s_lon = sched["start_lon"].values
    e_lat = sched["end_lat"].values
    e_lon = sched["end_lon"].values
    s_stop = sched["start_stop"].values
    e_stop = sched["end_stop"].values
    route = sched["route_id"].values

    for (agency, svc), grp_idx in sched.groupby(["agency_id", "service_id"]).indices.items():
        pool_end_h: list[float] = []
        pool_start_h: list[float] = []   # first-trip start, for max_shift_h cap
        pool_lat: list[float] = []
        pool_lon: list[float] = []
        pool_stop: list = []
        pool_route: list = []
        pool_bid: list[str] = []

        for i in grp_idx:
            t_start = start_h[i]
            t_end = end_h[i]
            best_j = -1
            best_score = np.inf
            for j in range(len(pool_end_h)):
                wait = t_start - pool_end_h[j]
                if wait < 0 or wait > max_layover_h:
                    continue
                if t_end - pool_start_h[j] > max_shift_h:
                    continue
                same_stop = pool_stop[j] == s_stop[i]
                if not same_stop:
                    dk = haversine_km(pool_lat[j], pool_lon[j], s_lat[i], s_lon[i])
                    if dk > max_deadhead_km:
                        continue
                score = wait
                if same_stop:
                    score -= same_stop_bonus_h
                if pool_route[j] == route[i]:
                    score -= route_continuity_bonus_h
                if score < best_score:
                    best_score = score
                    best_j = j
            if best_j == -1:
                bid = f"INF_{agency}_{svc}_{block_counter:06d}"
                block_counter += 1
                pool_end_h.append(t_end)
                pool_start_h.append(t_start)
                pool_lat.append(e_lat[i])
                pool_lon.append(e_lon[i])
                pool_stop.append(e_stop[i])
                pool_route.append(route[i])
                pool_bid.append(bid)
                inferred[i] = bid
            else:
                pool_end_h[best_j] = t_end
                pool_lat[best_j] = e_lat[i]
                pool_lon[best_j] = e_lon[i]
                pool_stop[best_j] = e_stop[i]
                pool_route[best_j] = route[i]
                inferred[i] = pool_bid[best_j]

    out = pd.Series(inferred, index=sched["index"].values, name="inferred_block_id")
    return out.sort_index()

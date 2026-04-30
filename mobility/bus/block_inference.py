"""Bit-preserving bus block inference for trips without native block IDs."""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class BlockInferenceConfig:
    max_layover_h: float = 1.0
    max_deadhead_km: float = 1.0
    same_stop_bonus_h: float = 0.5
    route_continuity_bonus_h: float = 0.25
    max_shift_h: float = 10.0


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km. Accepts scalars or numpy arrays."""
    radius_km = 6371.0088
    lat1 = np.radians(lat1)
    lon1 = np.radians(lon1)
    lat2 = np.radians(lat2)
    lon2 = np.radians(lon2)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return 2 * radius_km * np.arcsin(np.sqrt(a))


def _resolve_config(
    config: BlockInferenceConfig | None,
    *,
    max_layover_h: float | None,
    max_deadhead_km: float | None,
    same_stop_bonus_h: float | None,
    route_continuity_bonus_h: float | None,
    max_shift_h: float | None,
) -> BlockInferenceConfig:
    cfg = config or BlockInferenceConfig()
    updates = {
        "max_layover_h": max_layover_h,
        "max_deadhead_km": max_deadhead_km,
        "same_stop_bonus_h": same_stop_bonus_h,
        "route_continuity_bonus_h": route_continuity_bonus_h,
        "max_shift_h": max_shift_h,
    }
    clean_updates = {key: value for key, value in updates.items() if value is not None}
    return replace(cfg, **clean_updates) if clean_updates else cfg


def _select_best_candidate(
    *,
    t_start: float,
    t_end: float,
    s_lat: float,
    s_lon: float,
    s_stop,
    route,
    pool_end_h: list[float],
    pool_start_h: list[float],
    pool_lat: list[float],
    pool_lon: list[float],
    pool_stop: list,
    pool_route: list,
    config: BlockInferenceConfig,
) -> int:
    best_j = -1
    best_score = np.inf
    for j in range(len(pool_end_h)):
        wait = t_start - pool_end_h[j]
        if wait < 0 or wait > config.max_layover_h:
            continue
        if t_end - pool_start_h[j] > config.max_shift_h:
            continue
        same_stop = pool_stop[j] == s_stop
        if not same_stop:
            deadhead_km = haversine_km(pool_lat[j], pool_lon[j], s_lat, s_lon)
            if deadhead_km > config.max_deadhead_km:
                continue
        score = wait
        if same_stop:
            score -= config.same_stop_bonus_h
        if pool_route[j] == route:
            score -= config.route_continuity_bonus_h
        if score < best_score:
            best_score = score
            best_j = j
    return best_j


def infer_blocks(
    sched: pd.DataFrame,
    config: BlockInferenceConfig | None = None,
    *,
    max_layover_h: float | None = None,
    max_deadhead_km: float | None = None,
    same_stop_bonus_h: float | None = None,
    route_continuity_bonus_h: float | None = None,
    max_shift_h: float | None = None,
) -> pd.Series:
    """Greedy vehicle assignment for trips that lack native ``block_id``."""
    cfg = _resolve_config(
        config,
        max_layover_h=max_layover_h,
        max_deadhead_km=max_deadhead_km,
        same_stop_bonus_h=same_stop_bonus_h,
        route_continuity_bonus_h=route_continuity_bonus_h,
        max_shift_h=max_shift_h,
    )
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

    for (agency, service_id), grp_idx in sched.groupby(["agency_id", "service_id"]).indices.items():
        pool_end_h: list[float] = []
        pool_start_h: list[float] = []
        pool_lat: list[float] = []
        pool_lon: list[float] = []
        pool_stop: list = []
        pool_route: list = []
        pool_bid: list[str] = []

        for i in grp_idx:
            t_start = start_h[i]
            t_end = end_h[i]
            best_j = _select_best_candidate(
                t_start=t_start,
                t_end=t_end,
                s_lat=s_lat[i],
                s_lon=s_lon[i],
                s_stop=s_stop[i],
                route=route[i],
                pool_end_h=pool_end_h,
                pool_start_h=pool_start_h,
                pool_lat=pool_lat,
                pool_lon=pool_lon,
                pool_stop=pool_stop,
                pool_route=pool_route,
                config=cfg,
            )
            if best_j == -1:
                bid = f"INF_{agency}_{service_id}_{block_counter:06d}"
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

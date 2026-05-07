"""Bit-preserving bus block inference for trips without native block IDs."""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from .distance import haversine_km


@dataclass(frozen=True)
class BlockInferenceConfig:
    same_stop_bonus_h: float = 1.0
    route_continuity_bonus_h: float = 0.5
    max_layover_h: float = 4.0
    max_shift_h: float = 16.0
    deadhead_speed_kmh: float = 30.0
    min_dwell_after_deadhead_h: float = 0.05
    max_inferred_deadhead_km: float = 5.0
    deadhead_penalty_h_per_km: float = 0.05


def _resolve_config(
    config: BlockInferenceConfig | None,
    *,
    max_layover_h: float | None,
    max_deadhead_km: float | None,
    max_inferred_deadhead_km: float | None,
    same_stop_bonus_h: float | None,
    route_continuity_bonus_h: float | None,
    max_shift_h: float | None,
) -> BlockInferenceConfig:
    cfg = config or BlockInferenceConfig()
    updates = {
        "max_layover_h": max_layover_h,
        "same_stop_bonus_h": same_stop_bonus_h,
        "route_continuity_bonus_h": route_continuity_bonus_h,
        "max_shift_h": max_shift_h,
    }
    if max_deadhead_km is not None and max_inferred_deadhead_km is not None:
        raise ValueError("Use only one of max_deadhead_km or max_inferred_deadhead_km.")
    updates["max_inferred_deadhead_km"] = (
        max_inferred_deadhead_km if max_inferred_deadhead_km is not None else max_deadhead_km
    )
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
    pool_bid: list[str],
    config: BlockInferenceConfig,
) -> int:
    best_j = -1
    best_key: tuple[float, float, float, str, str] | None = None
    for j in range(len(pool_end_h)):
        gap_h = float(t_start - pool_end_h[j])
        if not (0.0 < gap_h <= config.max_layover_h):
            continue
        if t_end - pool_start_h[j] > config.max_shift_h:
            continue
        same_stop = pool_stop[j] == s_stop
        if same_stop:
            deadhead_km = 0.0
        else:
            coords = (pool_lat[j], pool_lon[j], s_lat, s_lon)
            if not all(np.isfinite(value) for value in coords):
                # Missing coordinates cannot be treated as zero-km deadhead.
                continue
            deadhead_km = float(haversine_km(pool_lat[j], pool_lon[j], s_lat, s_lon))
        deadhead_h = deadhead_km / float(config.deadhead_speed_kmh)
        if deadhead_km > config.max_inferred_deadhead_km:
            continue
        if gap_h < deadhead_h + config.min_dwell_after_deadhead_h:
            continue

        score = gap_h - deadhead_h
        score += config.deadhead_penalty_h_per_km * deadhead_km
        score -= config.same_stop_bonus_h * float(same_stop)
        score -= config.route_continuity_bonus_h * float(pool_route[j] == route)
        # Candidates represent existing inferred pool blocks, not only individual trips;
        # using pool_bid gives a stable deterministic tie-break at the block level.
        key = (float(score), float(pool_start_h[j]), float(pool_end_h[j]), str(pool_route[j]), str(pool_bid[j]))
        if best_key is None or key < best_key:
            best_key = key
            best_j = j
    return best_j


def infer_blocks(
    sched: pd.DataFrame,
    config: BlockInferenceConfig | None = None,
    *,
    max_layover_h: float | None = None,
    max_deadhead_km: float | None = None,
    max_inferred_deadhead_km: float | None = None,
    same_stop_bonus_h: float | None = None,
    route_continuity_bonus_h: float | None = None,
    max_shift_h: float | None = None,
) -> pd.Series:
    """Greedy vehicle assignment for trips that lack native ``block_id``."""
    cfg = _resolve_config(
        config,
        max_layover_h=max_layover_h,
        max_deadhead_km=max_deadhead_km,
        max_inferred_deadhead_km=max_inferred_deadhead_km,
        same_stop_bonus_h=same_stop_bonus_h,
        route_continuity_bonus_h=route_continuity_bonus_h,
        max_shift_h=max_shift_h,
    )
    sched = sched.sort_values(
        ["agency_id", "service_id", "start_h", "end_h", "route_id", "trip_id"],
        kind="stable",
    ).reset_index(drop=False)
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
                pool_bid=pool_bid,
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

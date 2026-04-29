"""Match parking events to public charging stations or home charging."""

from __future__ import annotations

from typing import Mapping, Optional
import warnings
import zlib

import numpy as np
import pandas as pd

from mobility.core.constants import (
    HOME_CHARGER_KW,
    HUFF_LAYER2_BETA,
    HUFF_LAYER2_DMIN_M,
    HUFF_LAYER2_DSCALE_M,
)
from mobility.core.data_structures import DailySchedule, ParkingEvent
from mobility.core.spatial import load_lsoa_centroids, od_distance_km


def _build_lsoa_indices(stations_df: pd.DataFrame) -> dict:
    """Build reusable Layer-2 sampling indices from the full station table."""
    valid = stations_df.dropna(
        subset=["lsoa_code", "label", "station_attractiveness"]
    ).copy().reset_index(drop=True)

    by_lsoa_label = {
        (key[0], key[1]): np.asarray(row_idx, dtype=np.int64)
        for key, row_idx in valid.groupby(["lsoa_code", "label"], sort=False).indices.items()
    }
    by_lsoa = {
        key: np.asarray(row_idx, dtype=np.int64)
        for key, row_idx in valid.groupby("lsoa_code", sort=False).indices.items()
    }

    return {
        "by_lsoa_label": by_lsoa_label,
        "by_lsoa": by_lsoa,
        "sid": valid["StationID"].to_numpy(dtype=np.int64, copy=True),
        "cap": valid["TotalCapacity_kW"].to_numpy(dtype=np.float64, copy=True),
        "attr": valid["station_attractiveness"].to_numpy(dtype=np.float64, copy=True),
        "lsoa": valid["lsoa_code"].astype(str).to_numpy(dtype=object, copy=True),
    }


def _sub_rng(ev_id: str, date_iso: str, pe: ParkingEvent) -> np.random.Generator:
    # zlib.crc32 instead of built-in hash(): stable across processes/platforms
    # (Python's hash() is PYTHONHASHSEED-randomised → not reproducible across runs)
    key = f"{ev_id}|{date_iso}|{pe.location_purpose}|{int(pe.start_time * 60)}"
    seed = zlib.crc32(key.encode("utf-8"))
    return np.random.default_rng(seed)


def _distance_m(pe_lsoa: str, station_lsoa: str, centroids: pd.DataFrame) -> float:
    if pe_lsoa == station_lsoa:
        return 500.0
    return float(od_distance_km(pe_lsoa, station_lsoa, centroids, intra_km=0.5) * 1000.0)


def _huff_weights(attr: np.ndarray, d_m: np.ndarray) -> np.ndarray:
    d = np.maximum(d_m, HUFF_LAYER2_DMIN_M)
    return attr * np.exp(-np.square(d_m / HUFF_LAYER2_DSCALE_M)) / np.power(
        d, HUFF_LAYER2_BETA
    )


def _match_one(
    pe: ParkingEvent,
    ev_home_lsoa: str,
    ev_ac_power_kw: float,
    sub_rng: np.random.Generator,
    idx: dict,
    centroids: pd.DataFrame,
    neighbor_buffer_lsoas: Mapping[str, list[str]] | None,
) -> None:
    if pe.location_purpose == "home":
        pe.can_charge = True
        pe.matched_station_id = None
        pe.charge_power_kw = HOME_CHARGER_KW
        return

    pe_lsoa = pe.location_lsoa or ev_home_lsoa

    rows = idx["by_lsoa_label"].get((pe_lsoa, pe.location_purpose))
    if rows is None or len(rows) == 0:
        rows = idx["by_lsoa"].get(pe_lsoa)

    if (rows is None or len(rows) == 0) and neighbor_buffer_lsoas is not None:
        pool = []
        for neighbor_lsoa in neighbor_buffer_lsoas.get(pe_lsoa, []):
            neighbor_rows = idx["by_lsoa"].get(neighbor_lsoa)
            if neighbor_rows is not None and len(neighbor_rows):
                pool.append(neighbor_rows)
        rows = np.concatenate(pool) if pool else None

    if rows is None or len(rows) == 0:
        pe.can_charge = False
        pe.matched_station_id = None
        pe.charge_power_kw = 0.0
        return

    attr = idx["attr"][rows]
    d_m = np.fromiter(
        (_distance_m(pe_lsoa, idx["lsoa"][row_idx], centroids) for row_idx in rows),
        dtype=np.float64,
        count=len(rows),
    )
    weights = _huff_weights(attr, d_m)
    weight_sum = weights.sum()
    if weight_sum <= 0.0:
        pe.can_charge = False
        pe.matched_station_id = None
        pe.charge_power_kw = 0.0
        return

    probs = weights / weight_sum
    chosen_row = int(sub_rng.choice(rows, p=probs))

    pe.can_charge = True
    pe.matched_station_id = int(idx["sid"][chosen_row])
    pe.charge_power_kw = float(min(idx["cap"][chosen_row], ev_ac_power_kw))


def match_stations_for_schedule(
    schedule: DailySchedule,
    ev_home_lsoa: str,
    ev_ac_power_kw: float,
    stations_df: pd.DataFrame,
    rng: np.random.Generator,
    *,
    centroids: pd.DataFrame,
    neighbor_buffer_lsoas: Mapping[str, list[str]] | None = None,
    _indices: dict | None = None,
    date_iso: str = "",
) -> None:
    """Annotate each ParkingEvent in the schedule with charging info in-place."""
    _ = rng
    idx = _indices if _indices is not None else _build_lsoa_indices(stations_df)
    centroids_indexed = _ensure_centroid_index(centroids)

    for pe in schedule.parking_events:
        sub_rng = _sub_rng(schedule.ev_id, date_iso, pe)
        _match_one(
            pe=pe,
            ev_home_lsoa=ev_home_lsoa,
            ev_ac_power_kw=ev_ac_power_kw,
            sub_rng=sub_rng,
            idx=idx,
            centroids=centroids_indexed,
            neighbor_buffer_lsoas=neighbor_buffer_lsoas,
        )


def match_stations_for_fleet(
    fleet_schedules: dict[str, list],
    ev_fleet: pd.DataFrame,
    stations_df: pd.DataFrame,
    rng: np.random.Generator,
    *,
    centroids: pd.DataFrame | None = None,
    neighbor_buffer_lsoas: Mapping[str, list[str]] | None = None,
) -> None:
    """Match charging stations for all EVs across all days."""
    centroids_frame = (
        load_lsoa_centroids().set_index("lsoa_code")[["easting_m", "northing_m"]]
        if centroids is None
        else _ensure_centroid_index(centroids)
    )
    indices = _build_lsoa_indices(stations_df)

    ev_home_map = _build_home_lsoa_map(ev_fleet)
    ev_ac_map = dict(zip(ev_fleet["EV_ID"], ev_fleet["ac_power_kw"]))

    for ev_id, daily_schedules in fleet_schedules.items():
        ev_home_lsoa = ev_home_map.get(ev_id, "")
        ev_ac = ev_ac_map.get(ev_id, 7.0)
        if ev_ac is None or (isinstance(ev_ac, float) and np.isnan(ev_ac)):
            ev_ac = 7.0

        for schedule in daily_schedules:
            match_stations_for_schedule(
                schedule=schedule,
                ev_home_lsoa=ev_home_lsoa,
                ev_ac_power_kw=float(ev_ac),
                stations_df=stations_df,
                rng=rng,
                centroids=centroids_frame,
                neighbor_buffer_lsoas=neighbor_buffer_lsoas,
                _indices=indices,
                date_iso=f"day{schedule.day:03d}",
            )


def _build_home_lsoa_map(ev_fleet: pd.DataFrame) -> dict[str, str]:
    if "home_lsoa" in ev_fleet.columns:
        home_values = ev_fleet["home_lsoa"].fillna("").astype(str)
        if "LSOA_code" in ev_fleet.columns:
            fallback_values = ev_fleet["LSOA_code"].fillna("").astype(str)
            home_values = home_values.where(home_values != "", fallback_values)
        return dict(zip(ev_fleet["EV_ID"], home_values))

    if "LSOA_code" in ev_fleet.columns:
        warnings.warn(
            "ev_fleet has no home_lsoa column; falling back to LSOA_code",
            RuntimeWarning,
            stacklevel=2,
        )
        return dict(zip(ev_fleet["EV_ID"], ev_fleet["LSOA_code"].fillna("").astype(str)))

    return dict(zip(ev_fleet["EV_ID"], np.full(len(ev_fleet), "", dtype=object)))


def _ensure_centroid_index(centroids: pd.DataFrame) -> pd.DataFrame:
    result = centroids.copy()
    if "lsoa_code" in result.columns:
        result = result.set_index("lsoa_code", drop=True)
    return result.loc[:, ["easting_m", "northing_m"]]

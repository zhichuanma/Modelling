"""Canonical vehicle-day event ledger for the M1 bus simulator."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .distance import haversine_km


EVENT_COLUMNS = [
    "vehicle_id",
    "chain_id",
    "service_date",
    "event_seq",
    "event_type",
    "block_instance_id",
    "start_time",
    "end_time",
    "duration_min",
    "start_lat",
    "start_lon",
    "end_lat",
    "end_lon",
    "distance_km",
    "distance_method",
    "energy_kwh_proxy",
]

EXTRA_COLUMNS = [
    "depot_id",
    "vehicle_provenance",
    "battery_kwh",
    "consumption_kwh_per_km",
    "ac_charge_kw_max",
    "dc_charge_kw_max",
    "usable_soc_min",
    "usable_soc_max",
]

MOVEMENT_EVENTS = {
    "depot_deadhead",
    "passenger_block",
    "inter_block_deadhead",
    "return_deadhead",
    "midday_return_deadhead",
    "midday_out_deadhead",
}


def _distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    if not all(np.isfinite([lat1, lon1, lat2, lon2])):
        return 0.0
    return float(haversine_km(lat1, lon1, lat2, lon2))


def _event(
    *,
    base: dict,
    event_type: str,
    block_instance_id: str,
    start_time: float,
    end_time: float,
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
    distance_km: float,
    consumption_kwh_per_km: float,
) -> dict:
    duration = max(0.0, float(end_time) - float(start_time))
    energy = float(distance_km) * float(consumption_kwh_per_km) if event_type in MOVEMENT_EVENTS else 0.0
    return {
        **base,
        "event_seq": 0,
        "event_type": event_type,
        "block_instance_id": block_instance_id,
        "start_time": float(start_time),
        "end_time": float(end_time),
        "duration_min": duration,
        "start_lat": float(start_lat),
        "start_lon": float(start_lon),
        "end_lat": float(end_lat),
        "end_lon": float(end_lon),
        "distance_km": float(distance_km),
        "distance_method": "haversine_x_1.0",
        "energy_kwh_proxy": float(energy),
    }


def _depot_lookup(depot_registry: pd.DataFrame) -> dict[str, pd.Series]:
    if depot_registry is None or depot_registry.empty:
        return {}
    return {
        str(row.depot_id): pd.Series(row._asdict())
        for row in depot_registry.itertuples(index=False)
    }


def _assignment_blocks(
    vehicle_assignments: pd.DataFrame,
    block_instances: pd.DataFrame,
) -> pd.DataFrame:
    merged = vehicle_assignments.merge(
        block_instances,
        on=["service_date", "block_id", "block_instance_id"],
        how="left",
        suffixes=("", "_block"),
    )
    if merged[["start_time", "end_time"]].isna().any().any():
        missing = merged.loc[merged["start_time"].isna(), "block_instance_id"].head().tolist()
        raise ValueError(f"Missing block instance rows for assignments: {missing}")
    return merged


def build_event_ledger(
    vehicle_assignments: pd.DataFrame,
    block_instances: pd.DataFrame,
    depot_registry: pd.DataFrame,
    stops_df: pd.DataFrame,
) -> pd.DataFrame:
    """Reconstruct the canonical per-vehicle-per-day event sequence."""
    del stops_df
    if vehicle_assignments is None or vehicle_assignments.empty:
        return pd.DataFrame(columns=EVENT_COLUMNS + EXTRA_COLUMNS)
    merged = _assignment_blocks(vehicle_assignments, block_instances)
    depots = _depot_lookup(depot_registry)
    rows: list[dict] = []
    deadhead_speed_kmh = 30.0

    for chain_id, group in merged.groupby("chain_id", sort=True):
        ordered = group.sort_values(["start_time", "block_instance_id"], kind="stable").reset_index(drop=True)
        first_assignment = ordered.iloc[0]
        depot_id = str(first_assignment["depot_id"])
        depot = depots.get(depot_id)
        if depot is None:
            raise ValueError(f"Depot {depot_id!r} missing for chain {chain_id!r}.")
        depot_lat = float(depot["lat"])
        depot_lon = float(depot["lon"])
        consumption = float(first_assignment.get("consumption_kwh_per_km", 1.2))
        base = {
            "vehicle_id": str(first_assignment["vehicle_id"]),
            "chain_id": str(chain_id),
            "service_date": str(first_assignment["service_date"]),
            "depot_id": depot_id,
            "vehicle_provenance": str(first_assignment.get("vehicle_provenance", "")),
            "battery_kwh": float(first_assignment.get("battery_kwh", 300.0)),
            "consumption_kwh_per_km": consumption,
            "ac_charge_kw_max": float(first_assignment.get("ac_charge_kw_max", 100.0)),
            "dc_charge_kw_max": float(first_assignment.get("dc_charge_kw_max", 150.0)),
            "usable_soc_min": float(first_assignment.get("usable_soc_min", 0.10)),
            "usable_soc_max": float(first_assignment.get("usable_soc_max", 0.95)),
        }

        first_deadhead_km = _distance(depot_lat, depot_lon, float(first_assignment.start_lat), float(first_assignment.start_lon))
        first_deadhead_min = first_deadhead_km / deadhead_speed_kmh * 60.0
        first_deadhead_start = max(0.0, float(first_assignment.start_time) - first_deadhead_min)
        rows.append(
            _event(
                base=base,
                event_type="depot_parking",
                block_instance_id="",
                start_time=0.0,
                end_time=first_deadhead_start,
                start_lat=depot_lat,
                start_lon=depot_lon,
                end_lat=depot_lat,
                end_lon=depot_lon,
                distance_km=0.0,
                consumption_kwh_per_km=consumption,
            )
        )
        rows.append(
            _event(
                base=base,
                event_type="depot_deadhead",
                block_instance_id=str(first_assignment.block_instance_id),
                start_time=first_deadhead_start,
                end_time=float(first_assignment.start_time),
                start_lat=depot_lat,
                start_lon=depot_lon,
                end_lat=float(first_assignment.start_lat),
                end_lon=float(first_assignment.start_lon),
                distance_km=first_deadhead_km,
                consumption_kwh_per_km=consumption,
            )
        )

        for idx, block in ordered.iterrows():
            rows.append(
                _event(
                    base=base,
                    event_type="passenger_block",
                    block_instance_id=str(block.block_instance_id),
                    start_time=float(block.start_time),
                    end_time=float(block.end_time),
                    start_lat=float(block.start_lat),
                    start_lon=float(block.start_lon),
                    end_lat=float(block.end_lat),
                    end_lon=float(block.end_lon),
                    distance_km=float(block.passenger_distance_km),
                    consumption_kwh_per_km=consumption,
                )
            )
            if idx >= len(ordered) - 1:
                continue
            nxt = ordered.iloc[idx + 1]
            gap_start = float(block.end_time)
            gap_end = float(nxt.start_time)
            gap = max(0.0, gap_end - gap_start)
            inter_km = _distance(float(block.end_lat), float(block.end_lon), float(nxt.start_lat), float(nxt.start_lon))
            if inter_km > 1e-6 and gap > 0.0:
                deadhead_min = min(inter_km / deadhead_speed_kmh * 60.0, gap)
                rows.append(
                    _event(
                        base=base,
                        event_type="inter_block_deadhead",
                        block_instance_id=str(nxt.block_instance_id),
                        start_time=gap_start,
                        end_time=gap_start + deadhead_min,
                        start_lat=float(block.end_lat),
                        start_lon=float(block.end_lon),
                        end_lat=float(nxt.start_lat),
                        end_lon=float(nxt.start_lon),
                        distance_km=inter_km,
                        consumption_kwh_per_km=consumption,
                    )
                )
                layover_start = gap_start + deadhead_min
                layover_lat = float(nxt.start_lat)
                layover_lon = float(nxt.start_lon)
            else:
                layover_start = gap_start
                layover_lat = float(block.end_lat)
                layover_lon = float(block.end_lon)
            if gap_end > layover_start:
                rows.append(
                    _event(
                        base=base,
                        event_type="terminal_layover",
                        block_instance_id="",
                        start_time=layover_start,
                        end_time=gap_end,
                        start_lat=layover_lat,
                        start_lon=layover_lon,
                        end_lat=layover_lat,
                        end_lon=layover_lon,
                        distance_km=0.0,
                        consumption_kwh_per_km=consumption,
                    )
                )

        last = ordered.iloc[-1]
        return_km = _distance(float(last.end_lat), float(last.end_lon), depot_lat, depot_lon)
        return_min = return_km / deadhead_speed_kmh * 60.0
        return_start = float(last.end_time)
        return_end = return_start + return_min
        rows.append(
            _event(
                base=base,
                event_type="return_deadhead",
                block_instance_id=str(last.block_instance_id),
                start_time=return_start,
                end_time=return_end,
                start_lat=float(last.end_lat),
                start_lon=float(last.end_lon),
                end_lat=depot_lat,
                end_lon=depot_lon,
                distance_km=return_km,
                consumption_kwh_per_km=consumption,
            )
        )
        if return_end < 1439.0:
            rows.append(
                _event(
                    base=base,
                    event_type="depot_parking",
                    block_instance_id="",
                    start_time=return_end,
                    end_time=1439.0,
                    start_lat=depot_lat,
                    start_lon=depot_lon,
                    end_lat=depot_lat,
                    end_lon=depot_lon,
                    distance_km=0.0,
                    consumption_kwh_per_km=consumption,
                )
            )

    out = pd.DataFrame(rows)
    out = out.sort_values(["chain_id", "start_time", "end_time", "event_type"], kind="stable").reset_index(drop=True)
    out["event_seq"] = out.groupby("chain_id", sort=False).cumcount() + 1
    return out.loc[:, EVENT_COLUMNS + EXTRA_COLUMNS].reset_index(drop=True)

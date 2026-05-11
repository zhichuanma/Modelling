"""Time-space-only greedy vehicle assignment for M1 bus block instances."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib

import numpy as np
import pandas as pd

from .distance import haversine_km


ASSIGNMENT_COLUMNS = [
    "service_date",
    "block_id",
    "block_instance_id",
    "vehicle_id",
    "chain_id",
    "prev_block_instance_id",
    "next_block_instance_id",
    "connection_deadhead_km",
    "connection_deadhead_min",
    "assignment_status",
    "assignment_method",
    "tiebreak_reason",
    "vehicle_provenance",
]

SPEC_COLUMNS = [
    "depot_id",
    "battery_kwh",
    "consumption_kwh_per_km",
    "ac_charge_kw_max",
    "dc_charge_kw_max",
    "usable_soc_min",
    "usable_soc_max",
]


@dataclass
class _VehicleState:
    vehicle_id: str
    depot_id: str
    current_lat: float
    current_lon: float
    free_after_time: float
    prev_block_instance_id: str
    vehicle_provenance: str
    battery_kwh: float
    consumption_kwh_per_km: float
    ac_charge_kw_max: float
    dc_charge_kw_max: float
    usable_soc_min: float
    usable_soc_max: float


def _confidence_rank(value: object) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(str(value), 9)


def _agency_depot_map(depot_registry: pd.DataFrame) -> dict[str, pd.DataFrame]:
    if depot_registry is None or depot_registry.empty:
        return {}
    depots = depot_registry.dropna(subset=["lat", "lon"]).copy()
    depots["_rank"] = depots["depot_confidence"].map(_confidence_rank)
    depots = depots.sort_values(["agency_id", "_rank", "depot_id"], kind="stable")
    return {
        str(agency_id): group.copy().reset_index(drop=True)
        for agency_id, group in depots.groupby("agency_id", sort=False)
    }


def _nearest_depot_id(
    block: pd.Series,
    agency_depots: dict[str, pd.DataFrame],
    all_depots: pd.DataFrame,
) -> str:
    agency_id = str(block["agency_id"])
    candidates = agency_depots.get(agency_id, all_depots)
    if candidates.empty:
        candidates = all_depots
    if candidates.empty:
        return ""
    lat = float(block["start_lat"])
    lon = float(block["start_lon"])
    if not (np.isfinite(lat) and np.isfinite(lon)):
        winner = candidates.sort_values(["_rank", "depot_id"], kind="stable").iloc[0]
        return str(winner["depot_id"])
    scored = candidates.copy()
    scored["_distance_km"] = haversine_km(
        lat,
        lon,
        scored["lat"].to_numpy(dtype=float),
        scored["lon"].to_numpy(dtype=float),
    )
    winner = scored.sort_values(["_distance_km", "_rank", "depot_id"], kind="stable").iloc[0]
    return str(winner["depot_id"])


def _assign_block_depots(
    blocks: pd.DataFrame,
    depot_registry: pd.DataFrame,
    agency_depots: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    out = blocks.copy()
    if depot_registry is None or depot_registry.empty:
        if "depot_id" not in out.columns:
            out["depot_id"] = np.nan
        return out
    all_depots = depot_registry.dropna(subset=["lat", "lon"]).copy()
    if all_depots.empty:
        if "depot_id" not in out.columns:
            out["depot_id"] = np.nan
        return out
    all_depots["_rank"] = all_depots["depot_confidence"].map(_confidence_rank)
    all_depots = all_depots.sort_values(["agency_id", "_rank", "depot_id"], kind="stable").reset_index(drop=True)
    out["depot_id"] = [
        _nearest_depot_id(row, agency_depots, all_depots)
        for _, row in out.iterrows()
    ]
    return out


def _normalise_vehicle_pool(vehicles: pd.DataFrame) -> pd.DataFrame:
    if vehicles is None or vehicles.empty:
        return pd.DataFrame()
    out = vehicles.copy()
    out = out.dropna(subset=["depot_id"]).copy()
    for col, default in (
        ("battery_kwh", 300.0),
        ("consumption_kwh_per_km", 1.2),
        ("ac_charge_kw_max", 100.0),
        ("dc_charge_kw_max", 150.0),
        ("usable_soc_min", 0.10),
        ("usable_soc_max", 0.95),
    ):
        if col not in out.columns:
            out[col] = default
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(default)
    if "vehicle_provenance" not in out.columns:
        out["vehicle_provenance"] = "ev_uk_lsoa_real"
    out["vehicle_id"] = out["vehicle_id"].astype(str)
    out["depot_id"] = out["depot_id"].astype(str)
    return out.sort_values(["depot_id", "vehicle_id"], kind="stable").reset_index(drop=True)


def _stable_seed(*parts: object, rng_seed: int) -> int:
    text = "|".join(str(part) for part in (rng_seed, *parts))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return int(digest[:16], 16) % (2**32)


def _synthetic_spec(
    depot_id: str,
    service_date: str,
    spawn_index: int,
    depot_vehicles: pd.DataFrame,
    rng_seed: int,
) -> dict:
    if depot_vehicles.empty:
        base = {
            "battery_kwh": 300.0,
            "consumption_kwh_per_km": 1.2,
            "ac_charge_kw_max": 100.0,
            "dc_charge_kw_max": 150.0,
            "usable_soc_min": 0.10,
            "usable_soc_max": 0.95,
        }
    else:
        frame = depot_vehicles.sort_values("vehicle_id", kind="stable").reset_index(drop=True)
        rng = np.random.default_rng(_stable_seed(depot_id, service_date, spawn_index, rng_seed=rng_seed))
        row = frame.iloc[int(rng.integers(0, len(frame)))]
        base = {col: float(row[col]) for col in SPEC_COLUMNS if col != "depot_id"}
    base["vehicle_id"] = f"synthetic_{depot_id}_{service_date}_{spawn_index:03d}"
    base["vehicle_provenance"] = "synthetic_overflow"
    return base


def _state_from_vehicle(row: pd.Series, depot: pd.Series) -> _VehicleState:
    return _VehicleState(
        vehicle_id=str(row["vehicle_id"]),
        depot_id=str(row["depot_id"]),
        current_lat=float(depot["lat"]),
        current_lon=float(depot["lon"]),
        free_after_time=0.0,
        prev_block_instance_id="",
        vehicle_provenance=str(row.get("vehicle_provenance", "ev_uk_lsoa_real")),
        battery_kwh=float(row.get("battery_kwh", 300.0)),
        consumption_kwh_per_km=float(row.get("consumption_kwh_per_km", 1.2)),
        ac_charge_kw_max=float(row.get("ac_charge_kw_max", 100.0)),
        dc_charge_kw_max=float(row.get("dc_charge_kw_max", 150.0)),
        usable_soc_min=float(row.get("usable_soc_min", 0.10)),
        usable_soc_max=float(row.get("usable_soc_max", 0.95)),
    )


def _connection_from_state(
    state: _VehicleState,
    block: pd.Series,
    deadhead_speed_kmh: float,
) -> tuple[float, float]:
    km = float(haversine_km(state.current_lat, state.current_lon, float(block.start_lat), float(block.start_lon)))
    minutes = km / float(deadhead_speed_kmh) * 60.0 if deadhead_speed_kmh > 0.0 else float("inf")
    return km, minutes


def _eligible_states(
    states: list[_VehicleState],
    block: pd.Series,
    deadhead_speed_kmh: float,
    min_turnaround_min: float,
) -> list[tuple[float, str, _VehicleState, float]]:
    latest_free_without_travel = float(block.start_time) - float(min_turnaround_min)
    candidates = [
        state
        for state in states
        if state.free_after_time <= latest_free_without_travel + 1e-9
    ]
    if not candidates:
        return []
    if len(candidates) < 4:
        eligible: list[tuple[float, str, _VehicleState, float]] = []
        for state in candidates:
            km, minutes = _connection_from_state(state, block, deadhead_speed_kmh)
            if state.free_after_time + minutes + float(min_turnaround_min) <= float(block.start_time) + 1e-9:
                eligible.append((km, state.vehicle_id, state, minutes))
        return sorted(eligible, key=lambda item: (item[0], item[1]))

    lat = np.fromiter((state.current_lat for state in candidates), dtype=float, count=len(candidates))
    lon = np.fromiter((state.current_lon for state in candidates), dtype=float, count=len(candidates))
    free_after = np.fromiter((state.free_after_time for state in candidates), dtype=float, count=len(candidates))
    distances_km = haversine_km(float(block.start_lat), float(block.start_lon), lat, lon)
    minutes = distances_km / float(deadhead_speed_kmh) * 60.0 if deadhead_speed_kmh > 0.0 else np.full(len(candidates), float("inf"))
    feasible = free_after + minutes + float(min_turnaround_min) <= float(block.start_time) + 1e-9
    if not feasible.any():
        return []
    valid = np.flatnonzero(feasible)
    order = sorted(valid, key=lambda idx: (float(distances_km[idx]), candidates[int(idx)].vehicle_id))
    return [
        (
            float(distances_km[idx]),
            candidates[int(idx)].vehicle_id,
            candidates[int(idx)],
            float(minutes[idx]),
        )
        for idx in order
    ]


def _build_states(
    depot_id: str,
    depot: pd.Series,
    vehicles: pd.DataFrame,
) -> list[_VehicleState]:
    pool = vehicles[vehicles["depot_id"].astype(str).eq(str(depot_id))]
    return [_state_from_vehicle(row, depot) for _, row in pool.iterrows()]


def assign_vehicles_greedy(
    block_instances: pd.DataFrame,
    vehicles: pd.DataFrame,
    depot_registry: pd.DataFrame,
    deadhead_speed_kmh: float = 30.0,
    min_turnaround_min: float = 10.0,
    rng_seed: int = 20260508,
) -> pd.DataFrame:
    """Assign every block instance using time-space feasibility only."""
    if block_instances is None or block_instances.empty:
        return pd.DataFrame(columns=ASSIGNMENT_COLUMNS + SPEC_COLUMNS)

    blocks = block_instances.copy()
    vehicles_norm = _normalise_vehicle_pool(vehicles)
    agency_depots = _agency_depot_map(depot_registry)
    depot_by_id = (
        depot_registry.dropna(subset=["lat", "lon"])
        .drop_duplicates("depot_id", keep="first")
        .set_index("depot_id", drop=False)
        if depot_registry is not None and not depot_registry.empty
        else pd.DataFrame()
    )
    if "depot_id" not in blocks.columns:
        blocks["depot_id"] = np.nan
    blocks = _assign_block_depots(blocks, depot_registry, agency_depots)
    blocks = blocks.dropna(subset=["depot_id"]).copy()
    blocks = blocks[blocks["depot_id"].astype(str).str.strip().ne("")].copy()
    if blocks.empty:
        raise ValueError("No block instances could be mapped to a depot.")

    records: list[dict] = []
    spawn_counts: dict[tuple[str, str], int] = {}
    for (service_date, depot_id), group in blocks.groupby(["service_date", "depot_id"], sort=True):
        if depot_id not in depot_by_id.index:
            raise ValueError(f"Depot {depot_id!r} is not present in depot_registry.")
        depot = depot_by_id.loc[depot_id]
        states = _build_states(str(depot_id), depot, vehicles_norm)
        depot_vehicle_specs = vehicles_norm[vehicles_norm["depot_id"].astype(str).eq(str(depot_id))]
        ordered = group.sort_values(["start_time", "block_instance_id"], kind="stable")
        for _, block in ordered.iterrows():
            eligible = _eligible_states(states, block, deadhead_speed_kmh, min_turnaround_min)
            if eligible:
                connection_km, _, state, connection_min = eligible[0]
                tiebreak_reason = "min_deadhead_then_vehicle_id"
            else:
                key = (str(service_date), str(depot_id))
                spawn_counts[key] = spawn_counts.get(key, 0) + 1
                spec = _synthetic_spec(str(depot_id), str(service_date), spawn_counts[key], depot_vehicle_specs, rng_seed)
                state = _VehicleState(
                    vehicle_id=spec["vehicle_id"],
                    depot_id=str(depot_id),
                    current_lat=float(depot["lat"]),
                    current_lon=float(depot["lon"]),
                    free_after_time=0.0,
                    prev_block_instance_id="",
                    vehicle_provenance="synthetic_overflow",
                    battery_kwh=float(spec["battery_kwh"]),
                    consumption_kwh_per_km=float(spec["consumption_kwh_per_km"]),
                    ac_charge_kw_max=float(spec["ac_charge_kw_max"]),
                    dc_charge_kw_max=float(spec["dc_charge_kw_max"]),
                    usable_soc_min=float(spec["usable_soc_min"]),
                    usable_soc_max=float(spec["usable_soc_max"]),
                )
                states.append(state)
                connection_km, connection_min = _connection_from_state(state, block, deadhead_speed_kmh)
                tiebreak_reason = "synthetic_overflow_spawn"

            chain_id = f"{state.vehicle_id}_{service_date}_00"
            records.append(
                {
                    "service_date": str(service_date),
                    "block_id": str(block["block_id"]),
                    "block_instance_id": str(block["block_instance_id"]),
                    "vehicle_id": state.vehicle_id,
                    "chain_id": chain_id,
                    "prev_block_instance_id": state.prev_block_instance_id,
                    "next_block_instance_id": "",
                    "connection_deadhead_km": float(connection_km),
                    "connection_deadhead_min": float(connection_min),
                    "assignment_status": "assigned",
                    "assignment_method": "greedy",
                    "tiebreak_reason": tiebreak_reason,
                    "vehicle_provenance": state.vehicle_provenance,
                    "depot_id": str(depot_id),
                    "battery_kwh": state.battery_kwh,
                    "consumption_kwh_per_km": state.consumption_kwh_per_km,
                    "ac_charge_kw_max": state.ac_charge_kw_max,
                    "dc_charge_kw_max": state.dc_charge_kw_max,
                    "usable_soc_min": state.usable_soc_min,
                    "usable_soc_max": state.usable_soc_max,
                }
            )
            state.current_lat = float(block["end_lat"])
            state.current_lon = float(block["end_lon"])
            state.free_after_time = float(block["end_time"])
            state.prev_block_instance_id = str(block["block_instance_id"])

    out = pd.DataFrame(records)
    if out.empty:
        return pd.DataFrame(columns=ASSIGNMENT_COLUMNS + SPEC_COLUMNS)
    out = out.sort_values(["chain_id", "service_date", "block_instance_id"], kind="stable").reset_index(drop=True)
    out["next_block_instance_id"] = out.groupby("chain_id", sort=False)["block_instance_id"].shift(-1).fillna("")
    return out.loc[:, ASSIGNMENT_COLUMNS + SPEC_COLUMNS].reset_index(drop=True)

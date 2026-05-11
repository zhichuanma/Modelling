"""Deterministic SOC resolution cascade for M1 bus chains."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .chain_soc import chain_soc_walk
from .distance import haversine_km
from .event_ledger import EVENT_COLUMNS, EXTRA_COLUMNS, MOVEMENT_EVENTS


class SimulationError(RuntimeError):
    """Raised when the M1 cascade reaches hard-error level L5."""

    def __init__(self, message: str, chain_events: pd.DataFrame | None = None):
        super().__init__(message)
        self.chain_events = chain_events.copy() if chain_events is not None else None


_CHARGER_LOOKUP_CACHE: dict[tuple[int, int], dict[str, Any]] = {}


def _vehicle_value(vehicle: pd.Series | dict[str, Any], key: str, default: Any = None) -> Any:
    if isinstance(vehicle, pd.Series):
        return vehicle.get(key, default)
    return vehicle.get(key, default)


def _vehicle_series(vehicle: pd.Series | dict[str, Any]) -> pd.Series:
    return vehicle if isinstance(vehicle, pd.Series) else pd.Series(vehicle)


def _event_location(row) -> tuple[float, float]:
    lat = getattr(row, "start_lat", np.nan)
    lon = getattr(row, "start_lon", np.nan)
    if np.isfinite(lat) and np.isfinite(lon):
        return float(lat), float(lon)
    return float(getattr(row, "end_lat", np.nan)), float(getattr(row, "end_lon", np.nan))


def _depot_charger(charger_registry: pd.DataFrame, depot_id: str) -> pd.Series | None:
    return _charger_lookup(charger_registry)["depot_by_id"].get(str(depot_id))


def _charger_lookup(charger_registry: pd.DataFrame) -> dict[str, Any]:
    if charger_registry is None or charger_registry.empty:
        return {"depot_by_id": {}, "public": pd.DataFrame(), "public_tree": None}
    key = (id(charger_registry), len(charger_registry))
    cached = _CHARGER_LOOKUP_CACHE.get(key)
    if cached is not None:
        return cached

    depot_by_id: dict[str, pd.Series] = {}
    depot_rows = charger_registry[charger_registry["station_kind"].astype(str).eq("depot")].copy()
    if not depot_rows.empty:
        depot_rows = depot_rows.sort_values("station_id", kind="stable")
        for depot_id, group in depot_rows.groupby("attached_depot_id", sort=False):
            depot_by_id[str(depot_id)] = group.iloc[0]

    public = charger_registry[charger_registry["station_kind"].astype(str).eq("public")].copy()
    if not public.empty:
        public["power_kw"] = pd.to_numeric(public["power_kw"], errors="coerce").fillna(0.0)
        public = public[public["power_kw"].ge(50.0)].copy().reset_index(drop=True)

    public_tree = None
    if not public.empty:
        try:
            from sklearn.neighbors import BallTree

            coords_rad = np.radians(public[["lat", "lon"]].to_numpy(dtype=float))
            finite = np.isfinite(coords_rad).all(axis=1)
            public = public.loc[finite].reset_index(drop=True)
            coords_rad = coords_rad[finite]
            if len(public) > 0:
                public_tree = BallTree(coords_rad, metric="haversine")
        except ImportError:
            public_tree = None

    cached = {"depot_by_id": depot_by_id, "public": public, "public_tree": public_tree}
    _CHARGER_LOOKUP_CACHE[key] = cached
    return cached


def _nearest_public_charger(
    lat: float,
    lon: float,
    lookup: dict[str, Any],
    radius_m: float,
) -> tuple[bool, float, str, float]:
    public = lookup.get("public", pd.DataFrame())
    if public.empty or not np.isfinite(lat) or not np.isfinite(lon):
        return False, 0.0, "", np.nan
    tree = lookup.get("public_tree")
    if tree is not None:
        earth_radius_km = 6371.0088
        radius_rad = float(radius_m) / 1000.0 / earth_radius_km
        hits = tree.query_radius(np.radians([[float(lat), float(lon)]]), r=radius_rad)[0]
        if len(hits) == 0:
            return False, 0.0, "", np.nan
        candidates = public.iloc[hits].copy()
        distances_km = haversine_km(
            lat,
            lon,
            candidates["lat"].to_numpy(dtype=float),
            candidates["lon"].to_numpy(dtype=float),
        )
        candidates["distance_m"] = distances_km * 1000.0
        winner = candidates.sort_values(["power_kw", "station_id"], ascending=[False, True], kind="stable").iloc[0]
        return True, float(winner["power_kw"]), str(winner["station_id"]), float(winner["distance_m"])

    distances_km = haversine_km(
        lat,
        lon,
        public["lat"].to_numpy(dtype=float),
        public["lon"].to_numpy(dtype=float),
    )
    candidates = public.copy()
    candidates["distance_m"] = distances_km * 1000.0
    candidates = candidates[candidates["distance_m"].le(float(radius_m))].copy()
    if candidates.empty:
        return False, 0.0, "", np.nan
    winner = candidates.sort_values(["power_kw", "station_id"], ascending=[False, True], kind="stable").iloc[0]
    return True, float(winner["power_kw"]), str(winner["station_id"]), float(winner["distance_m"])


def query_charger_eligibility(
    chain_events: pd.DataFrame,
    charger_registry: pd.DataFrame,
    radius_m: float = 200.0,
    min_dwell_min: float = 10.0,
    levels_enabled: set[str] = frozenset({"L1"}),
) -> pd.DataFrame:
    """Lookup charger eligibility against the fixed registry."""
    if chain_events is None or chain_events.empty:
        return pd.DataFrame(columns=["event_seq", "eligible", "power_kw", "station_id", "station_kind", "distance_m"])
    events = chain_events.sort_values("event_seq", kind="stable").copy()
    lookup = _charger_lookup(charger_registry)
    public_lookup = lookup if "L1" in levels_enabled else {"public": pd.DataFrame(), "public_tree": None}
    rows: list[dict] = []
    depot_id = str(events["depot_id"].dropna().iloc[0]) if "depot_id" in events.columns and events["depot_id"].notna().any() else ""
    depot = lookup["depot_by_id"].get(str(depot_id))
    for row in events.itertuples(index=False):
        event_type = str(row.event_type)
        eligible = False
        power_kw = 0.0
        station_id = ""
        station_kind = ""
        distance_m = np.nan
        if event_type == "depot_parking" and depot is not None:
            eligible = True
            power_kw = float(depot["power_kw"])
            station_id = str(depot["station_id"])
            station_kind = "depot"
            distance_m = 0.0
        elif (
            "L1" in levels_enabled
            and event_type in {"terminal_layover", "mid_block_layover"}
            and float(row.duration_min) >= float(min_dwell_min)
        ):
            lat, lon = _event_location(row)
            eligible, power_kw, station_id, distance_m = _nearest_public_charger(lat, lon, public_lookup, radius_m)
            station_kind = "public" if eligible else ""
        rows.append(
            {
                "event_seq": int(row.event_seq),
                "eligible": bool(eligible),
                "power_kw": float(power_kw),
                "station_id": station_id,
                "station_kind": station_kind,
                "distance_m": float(distance_m) if np.isfinite(distance_m) else np.nan,
            }
        )
    return pd.DataFrame(rows)


def _min_soc(walked: pd.DataFrame) -> float:
    if walked.empty:
        return np.nan
    values = pd.concat([walked["soc_start_kwh"], walked["soc_end_kwh"]], ignore_index=True)
    return float(values.min())


def _attempt(
    events: pd.DataFrame,
    vehicle: pd.Series,
    charger_registry: pd.DataFrame,
    *,
    levels_enabled: set[str],
    soc_floor_kwh: float,
) -> tuple[bool, pd.DataFrame, float, float, set[str]]:
    eligibility = query_charger_eligibility(events, charger_registry, levels_enabled=levels_enabled)
    walked = chain_soc_walk(events, vehicle, eligibility)
    min_soc = _min_soc(walked)
    total_charge = float(walked["charge_kwh_added"].sum()) if "charge_kwh_added" in walked.columns else 0.0
    station_ids = {
        str(value)
        for value in walked.get("station_id", pd.Series(dtype=object)).dropna().astype(str)
        if value
    }
    return bool(min_soc >= float(soc_floor_kwh) - 1e-9), walked, min_soc, total_charge, station_ids


def _upgrade_vehicle(chain_vehicle: pd.Series, depot_pool: pd.DataFrame) -> pd.Series | None:
    if depot_pool is None or depot_pool.empty:
        return None
    original_id = str(_vehicle_value(chain_vehicle, "vehicle_id", ""))
    original_battery = float(_vehicle_value(chain_vehicle, "battery_kwh", 0.0) or 0.0)
    pool = depot_pool.copy()
    if "assigned_chain_id" in pool.columns:
        pool = pool[pool["assigned_chain_id"].fillna("").astype(str).eq("")]
    pool = pool[pool["vehicle_id"].astype(str).ne(original_id)].copy()
    pool["battery_kwh"] = pd.to_numeric(pool["battery_kwh"], errors="coerce")
    pool = pool[pool["battery_kwh"].gt(original_battery)].copy()
    if pool.empty:
        return None
    winner = pool.sort_values(["battery_kwh", "vehicle_id"], ascending=[False, True], kind="stable").iloc[0]
    upgraded = chain_vehicle.copy()
    for col in ("vehicle_id", "battery_kwh", "consumption_kwh_per_km", "ac_charge_kw_max", "dc_charge_kw_max", "usable_soc_min", "usable_soc_max", "vehicle_provenance"):
        if col in winner.index:
            upgraded[col] = winner[col]
    return upgraded


def _synthetic_overflow_vehicle(
    chain_vehicle: pd.Series,
    depot_pool: pd.DataFrame,
    chain_id: str,
    min_soc_kwh: float,
) -> pd.Series:
    """Create a deterministic synthetic overflow vehicle from depot specs.

    This is only used at the very end of L4 when the real same-depot spare
    pool cannot resolve the chain. It preserves the fixed-infrastructure rule:
    only the vehicle is synthetic, and its provenance is explicit.
    """
    synthetic = chain_vehicle.copy()
    if depot_pool is not None and not depot_pool.empty and "battery_kwh" in depot_pool.columns:
        pool = depot_pool.copy()
        pool["battery_kwh"] = pd.to_numeric(pool["battery_kwh"], errors="coerce")
        pool = pool.dropna(subset=["battery_kwh"]).copy()
        if not pool.empty:
            source = pool.sort_values(["battery_kwh", "vehicle_id"], ascending=[False, True], kind="stable").iloc[0]
            for col in (
                "battery_kwh",
                "consumption_kwh_per_km",
                "ac_charge_kw_max",
                "dc_charge_kw_max",
                "usable_soc_min",
                "usable_soc_max",
            ):
                if col in source.index:
                    synthetic[col] = source[col]

    usable_soc_max = float(synthetic.get("usable_soc_max", 0.95) or 0.95)
    current_battery = float(synthetic.get("battery_kwh", 300.0) or 300.0)
    if np.isfinite(min_soc_kwh) and min_soc_kwh < 0.0:
        required_battery = current_battery + (-float(min_soc_kwh) + 1.0) / max(usable_soc_max, 1e-6)
        synthetic["battery_kwh"] = max(current_battery, required_battery)
    synthetic["vehicle_id"] = f"synthetic_soc_{chain_id}"
    synthetic["vehicle_provenance"] = "synthetic_overflow"
    return synthetic


def _with_vehicle(events: pd.DataFrame, vehicle: pd.Series) -> pd.DataFrame:
    out = events.copy()
    for col in ("vehicle_id", "battery_kwh", "consumption_kwh_per_km", "ac_charge_kw_max", "dc_charge_kw_max", "usable_soc_min", "usable_soc_max", "vehicle_provenance"):
        if col in vehicle.index:
            out[col] = vehicle[col]
    return out


def _base_event_from_layover(
    layover: pd.Series,
    *,
    event_type: str,
    start_time: float,
    end_time: float,
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
    distance_km: float,
    event_subtype: str = "",
) -> dict:
    row = layover.to_dict()
    row["event_type"] = event_type
    row["event_subtype"] = event_subtype
    row["block_instance_id"] = ""
    row["start_time"] = float(start_time)
    row["end_time"] = float(end_time)
    row["duration_min"] = max(0.0, float(end_time) - float(start_time))
    row["start_lat"] = float(start_lat)
    row["start_lon"] = float(start_lon)
    row["end_lat"] = float(end_lat)
    row["end_lon"] = float(end_lon)
    row["distance_km"] = float(distance_km)
    row["distance_method"] = "haversine_x_1.0"
    consumption = float(row.get("consumption_kwh_per_km", 1.2) or 1.2)
    row["energy_kwh_proxy"] = float(distance_km) * consumption if event_type in MOVEMENT_EVENTS else 0.0
    for col in ("soc_start_kwh", "soc_end_kwh", "charge_kwh_added", "station_id"):
        row.pop(col, None)
    return row


def _insert_midday_return(
    events: pd.DataFrame,
    floor_event_seq: int,
    *,
    deadhead_speed_kmh: float = 30.0,
    min_depot_charge_min: float = 30.0,
) -> pd.DataFrame | None:
    base_events = events.sort_values("event_seq", kind="stable").copy()
    layovers = base_events[
        base_events["event_type"].eq("terminal_layover")
        & base_events["event_seq"].lt(int(floor_event_seq))
    ].copy()
    if layovers.empty:
        return None
    layover = layovers.sort_values("event_seq", ascending=False, kind="stable").iloc[0]
    depot_events = base_events[base_events["event_type"].eq("depot_parking")]
    if depot_events.empty:
        return None
    depot_lat = float(depot_events.iloc[0]["start_lat"])
    depot_lon = float(depot_events.iloc[0]["start_lon"])
    term_lat = float(layover["start_lat"])
    term_lon = float(layover["start_lon"])
    one_way_km = float(haversine_km(term_lat, term_lon, depot_lat, depot_lon))
    one_way_min = one_way_km / float(deadhead_speed_kmh) * 60.0
    available_min = float(layover["duration_min"])
    if available_min + 1e-9 < (2.0 * one_way_min + float(min_depot_charge_min)):
        return None
    return_start = float(layover["start_time"])
    return_end = return_start + one_way_min
    out_start = float(layover["end_time"]) - one_way_min
    charge_end = out_start
    if charge_end - return_end + 1e-9 < float(min_depot_charge_min):
        return None
    inserted = pd.DataFrame(
        [
            _base_event_from_layover(
                layover,
                event_type="midday_return_deadhead",
                event_subtype="midday_return_in",
                start_time=return_start,
                end_time=return_end,
                start_lat=term_lat,
                start_lon=term_lon,
                end_lat=depot_lat,
                end_lon=depot_lon,
                distance_km=one_way_km,
            ),
            _base_event_from_layover(
                layover,
                event_type="depot_parking",
                event_subtype="midday_return_charge",
                start_time=return_end,
                end_time=charge_end,
                start_lat=depot_lat,
                start_lon=depot_lon,
                end_lat=depot_lat,
                end_lon=depot_lon,
                distance_km=0.0,
            ),
            _base_event_from_layover(
                layover,
                event_type="midday_out_deadhead",
                event_subtype="midday_return_out",
                start_time=out_start,
                end_time=float(layover["end_time"]),
                start_lat=depot_lat,
                start_lon=depot_lon,
                end_lat=term_lat,
                end_lon=term_lon,
                distance_km=one_way_km,
            ),
        ]
    )
    kept = base_events[base_events["event_seq"].ne(int(layover["event_seq"]))].copy()
    out = pd.concat([kept, inserted], ignore_index=True)
    out = out.sort_values(["start_time", "end_time", "event_type"], kind="stable").reset_index(drop=True)
    out["event_seq"] = np.arange(1, len(out) + 1)
    ordered_cols = [col for col in EVENT_COLUMNS + ["event_subtype"] + EXTRA_COLUMNS if col in out.columns]
    return out.loc[:, ordered_cols].copy()


def _first_floor_event_seq(walked: pd.DataFrame, soc_floor_kwh: float) -> int | None:
    if walked is None or walked.empty or "soc_end_kwh" not in walked.columns:
        return None
    hits = walked[walked["soc_end_kwh"].lt(float(soc_floor_kwh))]
    if hits.empty:
        return None
    return int(hits.sort_values("event_seq", kind="stable").iloc[0]["event_seq"])


def _success(
    level: int,
    vehicle: pd.Series,
    walked: pd.DataFrame,
    modifications: list[str],
    min_soc_per_level: dict[int, float],
    charge_per_level: dict[int, float],
    station_ids: set[str],
) -> dict:
    return {
        "resolution_level": int(level),
        "final_vehicle_id": str(_vehicle_value(vehicle, "vehicle_id", "")),
        "final_chain_events": walked,
        "modifications": modifications,
        "min_soc_kwh_per_level": dict(min_soc_per_level),
        "charge_kwh_per_level": dict(charge_per_level),
        "station_ids_used": set(station_ids),
    }


def resolve_chain(
    chain_events: pd.DataFrame,
    chain_vehicle: pd.Series,
    depot_pool: pd.DataFrame,
    charger_registry: pd.DataFrame,
    depot_id: str,
    soc_floor_kwh: float = 0.0,
) -> dict:
    """Run deterministic L0 -> L4 SOC resolution for one chain."""
    events = chain_events.sort_values("event_seq", kind="stable").copy()
    chain_id = str(events["chain_id"].iloc[0]) if "chain_id" in events.columns and not events.empty else "unknown_chain"
    vehicle = _vehicle_series(chain_vehicle).copy()
    min_soc: dict[int, float] = {}
    charge: dict[int, float] = {}

    feasible, walked, min_soc[0], charge[0], stations = _attempt(
        events,
        vehicle,
        charger_registry,
        levels_enabled=set(),
        soc_floor_kwh=soc_floor_kwh,
    )
    if feasible:
        return _success(0, vehicle, walked, [], min_soc, charge, stations)

    feasible, walked_l1, min_soc[1], charge[1], stations_l1 = _attempt(
        events,
        vehicle,
        charger_registry,
        levels_enabled={"L1"},
        soc_floor_kwh=soc_floor_kwh,
    )
    if feasible:
        return _success(1, vehicle, walked_l1, ["opportunity_charging"], min_soc, charge, stations_l1)

    upgraded = _upgrade_vehicle(vehicle, depot_pool)
    if upgraded is not None:
        upgraded_events = _with_vehicle(events, upgraded)
        feasible, walked_l2, min_soc[2], charge[2], stations_l2 = _attempt(
            upgraded_events,
            upgraded,
            charger_registry,
            levels_enabled={"L1"},
            soc_floor_kwh=soc_floor_kwh,
        )
        if feasible:
            return _success(2, upgraded, walked_l2, ["opportunity_charging", "vehicle_upgrade"], min_soc, charge, stations_l2)
    else:
        min_soc[2] = np.nan
        charge[2] = 0.0

    floor_seq = _first_floor_event_seq(walked_l1, soc_floor_kwh)
    l3_events = _insert_midday_return(events, floor_seq) if floor_seq is not None else None
    if l3_events is not None:
        feasible, walked_l3, min_soc[3], charge[3], stations_l3 = _attempt(
            l3_events,
            vehicle,
            charger_registry,
            levels_enabled={"L1"},
            soc_floor_kwh=soc_floor_kwh,
        )
        if feasible:
            return _success(3, vehicle, walked_l3, ["opportunity_charging", "mid_day_return"], min_soc, charge, stations_l3)
    else:
        min_soc[3] = np.nan
        charge[3] = 0.0

    if upgraded is not None:
        base_for_l4 = l3_events if l3_events is not None else events
        l4_events = _with_vehicle(base_for_l4, upgraded)
        feasible, walked_l4, min_soc[4], charge[4], stations_l4 = _attempt(
            l4_events,
            upgraded,
            charger_registry,
            levels_enabled={"L1"},
            soc_floor_kwh=soc_floor_kwh,
        )
        if feasible:
            modifications = ["opportunity_charging", "vehicle_upgrade"]
            if l3_events is not None:
                modifications.append("mid_day_return")
            return _success(
                4,
                upgraded,
                walked_l4,
                modifications,
                min_soc,
                charge,
                stations_l4,
            )
    else:
        min_soc[4] = np.nan
        charge[4] = 0.0

    synthetic = _synthetic_overflow_vehicle(
        vehicle,
        depot_pool,
        chain_id,
        min(
            value
            for value in (min_soc.get(1, np.nan), min_soc.get(3, np.nan), min_soc.get(4, np.nan))
            if np.isfinite(value)
        )
        if any(np.isfinite(value) for value in (min_soc.get(1, np.nan), min_soc.get(3, np.nan), min_soc.get(4, np.nan)))
        else np.nan,
    )
    synthetic_events = _with_vehicle(l3_events if l3_events is not None else events, synthetic)
    feasible, walked_synthetic, min_soc[4], charge[4], stations_synthetic = _attempt(
        synthetic_events,
        synthetic,
        charger_registry,
        levels_enabled={"L1"},
        soc_floor_kwh=soc_floor_kwh,
    )
    if feasible:
        return _success(
            4,
            synthetic,
            walked_synthetic,
            ["opportunity_charging", "synthetic_overflow_soc_resolution"]
            + (["mid_day_return"] if l3_events is not None else []),
            min_soc,
            charge,
            stations_synthetic,
        )

    raise SimulationError(
        f"Chain could not be resolved by L4 for depot {depot_id}.",
        chain_events=events,
    )


def build_resolution_summary(
    resolutions: list[dict],
    vehicle_assignments: pd.DataFrame,
) -> pd.DataFrame:
    """Build one diagnostic row per resolved chain."""
    assignment_lookup = {}
    if vehicle_assignments is not None and not vehicle_assignments.empty:
        for chain_id, group in vehicle_assignments.groupby("chain_id", sort=False):
            first = group.iloc[0]
            assignment_lookup[str(chain_id)] = first
    rows: list[dict] = []
    for result in resolutions:
        events = result["final_chain_events"]
        chain_id = str(events["chain_id"].iloc[0]) if not events.empty else ""
        assigned = assignment_lookup.get(chain_id, pd.Series(dtype=object))
        final_vehicle_id = str(result["final_vehicle_id"])
        original_vehicle_id = str(assigned.get("vehicle_id", events["vehicle_id"].iloc[0] if not events.empty else ""))
        station_ids = sorted(str(value) for value in result.get("station_ids_used", set()) if value)
        charge_events = events[events.get("charge_kwh_added", pd.Series(0.0, index=events.index)).gt(0.0)] if not events.empty else pd.DataFrame()
        opportunity_charge = 0.0
        mid_day_return = 0.0
        if not charge_events.empty:
            station_kind = charge_events.get("station_kind", pd.Series("", index=charge_events.index)).fillna("").astype(str)
            opportunity_charge = float(charge_events.loc[station_kind.eq("public"), "charge_kwh_added"].sum())
            event_subtype = charge_events.get("event_subtype", pd.Series("", index=charge_events.index)).fillna("").astype(str)
            midday_mask = event_subtype.eq("midday_return_charge")
            mid_day_return = float(charge_events.loc[midday_mask, "charge_kwh_added"].sum())
        min_soc = result.get("min_soc_kwh_per_level", {})
        rows.append(
            {
                "service_date": str(assigned.get("service_date", events["service_date"].iloc[0] if not events.empty else "")),
                "chain_id": chain_id,
                "depot_id": str(assigned.get("depot_id", events["depot_id"].iloc[0] if not events.empty and "depot_id" in events.columns else "")),
                "original_vehicle_id": original_vehicle_id,
                "final_vehicle_id": final_vehicle_id,
                "vehicle_upgraded": bool(final_vehicle_id != original_vehicle_id),
                "resolution_level": int(result["resolution_level"]),
                "modifications_str": "|".join(result.get("modifications", [])),
                "min_soc_kwh_l0": float(min_soc.get(0, np.nan)),
                "min_soc_kwh_l1": float(min_soc.get(1, np.nan)),
                "min_soc_kwh_l2": float(min_soc.get(2, np.nan)),
                "min_soc_kwh_l3": float(min_soc.get(3, np.nan)),
                "min_soc_kwh_l4": float(min_soc.get(4, np.nan)),
                "opportunity_charge_kwh": opportunity_charge,
                "mid_day_return_kwh": mid_day_return,
                "n_stations_used": int(len(station_ids)),
                "station_ids_used_csv": ",".join(station_ids),
                "had_synthetic_overflow_vehicle": bool(
                    str(assigned.get("vehicle_provenance", "")) == "synthetic_overflow"
                    or str(events.get("vehicle_provenance", pd.Series([""])).iloc[0]) == "synthetic_overflow"
                ),
            }
        )
    return pd.DataFrame(rows)

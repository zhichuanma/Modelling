"""Fixed vehicle home-depot assignment for PR 1.5 (plan v2 §14.2 / §15).

Each EV bus is deterministically pinned to the depot nearest its inventory
``source_lsoa`` centroid (``source_lsoa_nearest``). No simulation bootstrap is
involved, so the home depot is stable across runs and usable as a matching
constraint (``first_assignment`` was rejected as circular in plan v2 §14.2).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from mobility.bus.annual_vehicle_day_assignment import _haversine_km_vec


HOME_DEPOT_METHOD_SOURCE_LSOA_NEAREST = "source_lsoa_nearest"
HOME_DEPOT_METHODS = (HOME_DEPOT_METHOD_SOURCE_LSOA_NEAREST,)

HOME_DEPOT_STATUS_ASSIGNED = "assigned"
HOME_DEPOT_STATUS_MISSING_SOURCE_LSOA = "missing_source_lsoa"
HOME_DEPOT_STATUS_MISSING_CENTROID = "missing_centroid"

HOME_DEPOT_SPEC_COLUMNS = [
    "home_depot_id",
    "home_depot_lsoa",
    "home_depot_lat",
    "home_depot_lon",
    "home_depot_distance_km",
    "home_depot_method",
    "home_depot_status",
]


def assign_home_depots(
    ev_bus_specs: pd.DataFrame,
    depot_registry: pd.DataFrame,
    centroids: pd.DataFrame,
    *,
    method: str = HOME_DEPOT_METHOD_SOURCE_LSOA_NEAREST,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Attach a fixed home depot to every EV spec.

    Returns ``(specs_with_home_depot, per_depot_fleet_counts)``. Specs whose
    ``source_lsoa`` is empty or has no centroid keep ``home_depot_id == ""`` and a
    non-``assigned`` ``home_depot_status``; constrained matching excludes them.
    """
    if method not in HOME_DEPOT_METHODS:
        raise NotImplementedError(f"home depot method {method!r} is not implemented; choose from {HOME_DEPOT_METHODS}.")
    required_registry = {"depot_id", "depot_lat", "depot_lon"}
    missing_registry = sorted(required_registry - set(depot_registry.columns))
    if missing_registry:
        raise ValueError(f"depot_registry is missing required columns: {missing_registry}")
    if "source_lsoa" not in ev_bus_specs.columns:
        raise ValueError("ev_bus_specs is missing required column: source_lsoa")
    required_centroids = {"lsoa_code", "lat", "lon"}
    missing_centroids = sorted(required_centroids - set(centroids.columns))
    if missing_centroids:
        raise ValueError(f"centroids is missing required columns: {missing_centroids}")

    depots = depot_registry.drop_duplicates("depot_id", keep="first").reset_index(drop=True)
    depot_lat = pd.to_numeric(depots["depot_lat"], errors="coerce").to_numpy(dtype=float)
    depot_lon = pd.to_numeric(depots["depot_lon"], errors="coerce").to_numpy(dtype=float)
    valid_depot = np.isfinite(depot_lat) & np.isfinite(depot_lon)
    if not valid_depot.any():
        raise ValueError("depot_registry has no depot with finite coordinates.")
    depots = depots.loc[valid_depot].reset_index(drop=True)
    depot_lat = depot_lat[valid_depot]
    depot_lon = depot_lon[valid_depot]
    depot_ids = depots["depot_id"].astype(str).to_numpy()
    depot_lsoas = depots["depot_lsoa"].astype(str).to_numpy() if "depot_lsoa" in depots.columns else np.full(len(depots), "", dtype=object)

    centroid_lookup = (
        centroids.assign(lsoa_code=centroids["lsoa_code"].astype(str).str.strip())
        .drop_duplicates("lsoa_code", keep="first")
        .set_index("lsoa_code")[["lat", "lon"]]
    )

    specs = ev_bus_specs.copy()
    source_lsoa = specs["source_lsoa"].fillna("").astype(str).str.strip()
    distinct_lsoas = sorted(set(source_lsoa) - {""})

    # Nearest depot per distinct source LSOA: distinct-LSOA x depot haversine.
    nearest_by_lsoa: dict[str, tuple[int, float]] = {}
    resolvable = [lsoa for lsoa in distinct_lsoas if lsoa in centroid_lookup.index]
    if resolvable:
        lsoa_lat = pd.to_numeric(centroid_lookup.loc[resolvable, "lat"], errors="coerce").to_numpy(dtype=float)
        lsoa_lon = pd.to_numeric(centroid_lookup.loc[resolvable, "lon"], errors="coerce").to_numpy(dtype=float)
        distances = _haversine_km_vec(
            lsoa_lat[:, None],
            lsoa_lon[:, None],
            depot_lat[None, :],
            depot_lon[None, :],
        )
        nearest_pos = np.argmin(distances, axis=1)
        for row, lsoa in enumerate(resolvable):
            position = int(nearest_pos[row])
            nearest_by_lsoa[lsoa] = (position, float(distances[row, position]))

    home_depot_id = np.full(len(specs), "", dtype=object)
    home_depot_lsoa = np.full(len(specs), "", dtype=object)
    home_lat = np.full(len(specs), np.nan, dtype=float)
    home_lon = np.full(len(specs), np.nan, dtype=float)
    home_distance = np.full(len(specs), np.nan, dtype=float)
    status = np.full(len(specs), HOME_DEPOT_STATUS_MISSING_SOURCE_LSOA, dtype=object)
    for row, lsoa in enumerate(source_lsoa.to_numpy()):
        if not lsoa:
            continue
        hit = nearest_by_lsoa.get(lsoa)
        if hit is None:
            status[row] = HOME_DEPOT_STATUS_MISSING_CENTROID
            continue
        position, distance_km = hit
        home_depot_id[row] = depot_ids[position]
        home_depot_lsoa[row] = depot_lsoas[position]
        home_lat[row] = depot_lat[position]
        home_lon[row] = depot_lon[position]
        home_distance[row] = distance_km
        status[row] = HOME_DEPOT_STATUS_ASSIGNED

    specs["home_depot_id"] = home_depot_id
    specs["home_depot_lsoa"] = home_depot_lsoa
    specs["home_depot_lat"] = home_lat
    specs["home_depot_lon"] = home_lon
    specs["home_depot_distance_km"] = home_distance
    specs["home_depot_method"] = method
    specs["home_depot_status"] = status

    assigned = specs.loc[specs["home_depot_status"].eq(HOME_DEPOT_STATUS_ASSIGNED)]
    per_depot = (
        assigned.groupby("home_depot_id", as_index=False, sort=True)
        .agg(
            n_home_vehicles=("vehicle_spec_id", "size"),
            n_source_lsoas=("source_lsoa", "nunique"),
            mean_home_depot_distance_km=("home_depot_distance_km", "mean"),
            max_home_depot_distance_km=("home_depot_distance_km", "max"),
        )
        .rename(columns={"home_depot_id": "depot_id"})
    )
    per_depot["share_of_fleet"] = per_depot["n_home_vehicles"] / float(len(specs)) if len(specs) else np.nan
    per_depot["home_depot_method"] = method
    per_depot["n_specs_total"] = int(len(specs))
    per_depot["n_specs_unassigned_home_depot"] = int((~specs["home_depot_status"].eq(HOME_DEPOT_STATUS_ASSIGNED)).sum())
    return specs, per_depot.reset_index(drop=True)


def build_depot_supply_demand(
    ev_bus_specs_with_home: pd.DataFrame,
    block_instances: pd.DataFrame,
    depot_registry: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Per-depot home-fleet supply vs block demand (plan v2 §15 diagnostics)."""
    if "home_depot_id" not in ev_bus_specs_with_home.columns:
        raise ValueError("ev_bus_specs_with_home is missing required column: home_depot_id")
    supply = (
        ev_bus_specs_with_home.loc[ev_bus_specs_with_home["home_depot_id"].astype(str).ne("")]
        .groupby(ev_bus_specs_with_home["home_depot_id"].astype(str), sort=True)
        .size()
        .rename("n_home_vehicles")
        .rename_axis("depot_id")
        .reset_index()
    )
    if block_instances.empty or not {"depot_id", "service_date"}.issubset(block_instances.columns):
        demand = pd.DataFrame(columns=["depot_id", "n_block_instances", "n_service_dates", "mean_daily_blocks"])
    else:
        demand = (
            block_instances.assign(depot_id=block_instances["depot_id"].astype(str))
            .groupby("depot_id", as_index=False, sort=True)
            .agg(
                n_block_instances=("block_instance_id", "size"),
                n_service_dates=("service_date", "nunique"),
            )
        )
        demand["mean_daily_blocks"] = demand["n_block_instances"] / demand["n_service_dates"].replace(0, np.nan)
    table = demand.merge(supply, on="depot_id", how="outer")
    table["n_home_vehicles"] = pd.to_numeric(table["n_home_vehicles"], errors="coerce").fillna(0).astype(int)
    for column in ("n_block_instances", "n_service_dates"):
        table[column] = pd.to_numeric(table[column], errors="coerce").fillna(0).astype(int)
    table["mean_daily_blocks"] = pd.to_numeric(table["mean_daily_blocks"], errors="coerce")
    with np.errstate(divide="ignore", invalid="ignore"):
        table["supply_demand_ratio"] = table["n_home_vehicles"] / table["mean_daily_blocks"]
    if depot_registry is not None and not depot_registry.empty:
        registry_cols = [col for col in ("depot_id", "depot_lsoa", "depot_lat", "depot_lon") if col in depot_registry.columns]
        table = table.merge(depot_registry.loc[:, registry_cols].drop_duplicates("depot_id", keep="first"), on="depot_id", how="left")
    return table.sort_values("depot_id", kind="stable").reset_index(drop=True)


__all__ = [
    "HOME_DEPOT_METHODS",
    "HOME_DEPOT_METHOD_SOURCE_LSOA_NEAREST",
    "HOME_DEPOT_SPEC_COLUMNS",
    "HOME_DEPOT_STATUS_ASSIGNED",
    "HOME_DEPOT_STATUS_MISSING_CENTROID",
    "HOME_DEPOT_STATUS_MISSING_SOURCE_LSOA",
    "assign_home_depots",
    "build_depot_supply_demand",
]

"""Bridge the EV-by-LSOA inventory into fixed bus fleet records."""

from __future__ import annotations

from pathlib import Path
import hashlib

import numpy as np
import pandas as pd

from mobility.core.spatial import load_lsoa_centroids

from .distance import haversine_km


MODELLING_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = MODELLING_ROOT.parent
DEFAULT_EV_LSOA_PATH = MODELLING_ROOT / "data" / "EV_UK_LSOA_2025_with_energy.csv"
if not DEFAULT_EV_LSOA_PATH.exists():
    DEFAULT_EV_LSOA_PATH = PROJECT_ROOT / "Data" / "EV" / "outputs" / "EV_UK_LSOA_2025_with_energy.csv"

VEHICLE_COLUMNS = [
    "vehicle_id",
    "depot_id",
    "source_lsoa",
    "battery_kwh",
    "consumption_kwh_per_km",
    "ac_charge_kw_max",
    "dc_charge_kw_max",
    "usable_soc_min",
    "usable_soc_max",
    "depot_match_distance_km",
    "depot_match_method",
    "operator_match",
    "vehicle_provenance",
    "source_row_id",
    "source_csv_md5",
]


def file_md5(path: Path) -> str:
    """Return an MD5 checksum for source-data provenance."""
    digest = hashlib.md5()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_ev_lsoa_inventory(path: Path = DEFAULT_EV_LSOA_PATH) -> pd.DataFrame:
    """Read the EV-LSOA inventory and attach source provenance metadata."""
    source_path = Path(path)
    df = pd.read_csv(source_path)
    df.attrs["source_path"] = str(source_path)
    df.attrs["source_csv_md5"] = file_md5(source_path)
    return df


def _numeric(raw: pd.DataFrame, candidates: tuple[str, ...], default: float = np.nan) -> pd.Series:
    for column in candidates:
        if column in raw.columns:
            return pd.to_numeric(raw[column], errors="coerce")
    return pd.Series(default, index=raw.index, dtype=float)


def _lsoa_centroid_lookup() -> pd.DataFrame:
    try:
        centroids = load_lsoa_centroids()
    except (FileNotFoundError, KeyError, ValueError, pd.errors.EmptyDataError):
        return pd.DataFrame(columns=["lsoa_code", "lat", "lon"])
    return centroids.loc[:, ["lsoa_code", "lat", "lon"]].copy()


def _operator_hint(row: pd.Series) -> str:
    for column in ("agency_id", "operator_noc", "operator", "Operator", "NOC"):
        if column in row.index and pd.notna(row[column]):
            value = str(row[column]).strip()
            if value:
                return value.upper()
    return ""


def _assign_depot(
    row: pd.Series,
    depots: pd.DataFrame,
    nearest_depot_max_km: float,
) -> tuple[str, float, str, bool]:
    if depots.empty or not np.isfinite(row.get("lat", np.nan)) or not np.isfinite(row.get("lon", np.nan)):
        return "", np.nan, "unmatched", False
    candidates = depots.copy()
    distances = haversine_km(
        float(row["lat"]),
        float(row["lon"]),
        candidates["lat"].to_numpy(dtype=float),
        candidates["lon"].to_numpy(dtype=float),
    )
    candidates["distance_km"] = distances
    candidates = candidates[candidates["distance_km"].le(float(nearest_depot_max_km))].copy()
    if candidates.empty:
        return "", float(np.nanmin(distances)) if len(distances) else np.nan, "unmatched", False

    hint = _operator_hint(row)
    operator_match = False
    if hint:
        same_operator = candidates[
            candidates.get("operator_noc", pd.Series("", index=candidates.index))
            .fillna("")
            .astype(str)
            .str.upper()
            .eq(hint)
            | candidates.get("agency_id", pd.Series("", index=candidates.index))
            .fillna("")
            .astype(str)
            .str.upper()
            .eq(hint)
        ]
        if not same_operator.empty:
            candidates = same_operator.copy()
            operator_match = True

    winner = candidates.sort_values(["distance_km", "depot_id"], kind="stable").iloc[0]
    method = "nearest_same_operator_depot" if operator_match else "nearest_depot"
    return str(winner["depot_id"]), float(winner["distance_km"]), method, operator_match


def bridge_ev_lsoa_to_fleet(
    ev_lsoa_df: pd.DataFrame,
    depot_registry: pd.DataFrame,
    nearest_depot_max_km: float = 30.0,
) -> pd.DataFrame:
    """Bridge EV_UK_LSOA bus rows to depot-anchored fleet records."""
    if ev_lsoa_df is None or ev_lsoa_df.empty:
        return pd.DataFrame(columns=VEHICLE_COLUMNS)

    raw = ev_lsoa_df.copy()
    subtype = raw.get("vehicle_subtype", pd.Series("", index=raw.index)).fillna("").astype(str).str.lower()
    raw = raw.loc[subtype.isin({"bus", "minibus"})].copy()
    if raw.empty:
        return pd.DataFrame(columns=VEHICLE_COLUMNS)

    raw["_source_row_id"] = raw.index.astype(int)
    raw["source_lsoa"] = raw.get("LSOA_code", raw.get("lsoa_code", pd.Series("", index=raw.index))).astype(str)
    centroids = _lsoa_centroid_lookup()
    if not centroids.empty:
        raw = raw.merge(centroids, left_on="source_lsoa", right_on="lsoa_code", how="left")
    else:
        raw["lat"] = np.nan
        raw["lon"] = np.nan

    depots = depot_registry.copy()
    if not depots.empty:
        depots = depots.dropna(subset=["lat", "lon"]).copy()
        depots["lat"] = pd.to_numeric(depots["lat"], errors="coerce")
        depots["lon"] = pd.to_numeric(depots["lon"], errors="coerce")
        depots = depots.dropna(subset=["lat", "lon"])

    assignments = [
        _assign_depot(row, depots, nearest_depot_max_km)
        for _, row in raw.iterrows()
    ]
    depot_ids, distances, methods, operator_matches = zip(*assignments) if assignments else ([], [], [], [])

    battery = _numeric(raw, ("Energy_kWh", "battery_kwh", "energy_capacity_kWh"))
    consumption = _numeric(raw, ("efficiency_wh_per_km",)) / 1000.0
    if consumption.isna().all():
        consumption = _numeric(raw, ("consumption_kwh_per_km", "energy_kWh_per_km_ukbc"))
    ac_power = _numeric(raw, ("AC_Power_kW", "ac_charge_kw_max", "power_capacity_kW"), default=100.0)
    dc_power = _numeric(raw, ("DC_Power_kW", "dc_charge_kw_max"), default=np.nan)
    dc_power = dc_power.where(dc_power.gt(0.0), ac_power)

    vehicle_id = raw.get("EV_ID", pd.Series(index=raw.index, dtype=object)).fillna("").astype(str)
    missing_ids = vehicle_id.str.strip().eq("")
    vehicle_id.loc[missing_ids] = [f"ev_lsoa_bus_{idx}" for idx in raw.loc[missing_ids, "_source_row_id"]]

    source_md5 = str(ev_lsoa_df.attrs.get("source_csv_md5", ""))
    out = pd.DataFrame(
        {
            "vehicle_id": vehicle_id.to_numpy(dtype=object),
            "depot_id": list(depot_ids),
            "source_lsoa": raw["source_lsoa"].to_numpy(dtype=object),
            "battery_kwh": battery.astype(float).to_numpy(),
            "consumption_kwh_per_km": consumption.astype(float).to_numpy(),
            "ac_charge_kw_max": ac_power.astype(float).to_numpy(),
            "dc_charge_kw_max": dc_power.astype(float).to_numpy(),
            "usable_soc_min": 0.10,
            "usable_soc_max": 0.95,
            "depot_match_distance_km": list(distances),
            "depot_match_method": list(methods),
            "operator_match": list(operator_matches),
            "vehicle_provenance": "ev_uk_lsoa_real",
            "source_row_id": raw["_source_row_id"].astype(int).to_numpy(),
            "source_csv_md5": source_md5,
        }
    )
    out["depot_id"] = out["depot_id"].replace("", np.nan)
    valid_specs = out["battery_kwh"].gt(0.0) & out["consumption_kwh_per_km"].gt(0.0)
    out = out.loc[valid_specs, VEHICLE_COLUMNS].reset_index(drop=True)
    return out

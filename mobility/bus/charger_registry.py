"""Fixed charger registry for M1 bus chain-mode simulation."""

from __future__ import annotations

from pathlib import Path
import warnings

import numpy as np
import pandas as pd


MODELLING_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = MODELLING_ROOT.parent
DEFAULT_OCM_PATH = MODELLING_ROOT / "data" / "UK_OCM_stations_labeled.csv"
if not DEFAULT_OCM_PATH.exists():
    DEFAULT_OCM_PATH = PROJECT_ROOT / "Data" / "Charging_stations" / "UK_OCM_stations_labeled.csv"
DEFAULT_VEHICLES_PATH = MODELLING_ROOT / "outputs" / "vehicles.parquet"

CHARGER_COLUMNS = [
    "station_id",
    "station_kind",
    "lat",
    "lon",
    "lsoa_code",
    "power_kw",
    "attached_depot_id",
    "source",
]


def _load_vehicle_power_by_depot(vehicles: pd.DataFrame | None) -> dict[str, float]:
    if vehicles is None:
        if DEFAULT_VEHICLES_PATH.exists():
            try:
                vehicles = pd.read_parquet(DEFAULT_VEHICLES_PATH)
            except (ImportError, OSError, ValueError):
                vehicles = None
    if vehicles is None or vehicles.empty or "depot_id" not in vehicles.columns:
        return {}
    power_col = "ac_charge_kw_max" if "ac_charge_kw_max" in vehicles.columns else "depot_charge_kw"
    if power_col not in vehicles.columns:
        return {}
    data = vehicles.dropna(subset=["depot_id"]).copy()
    data[power_col] = pd.to_numeric(data[power_col], errors="coerce")
    grouped = data.dropna(subset=[power_col]).groupby("depot_id", sort=False)[power_col].median()
    return {str(key): float(value) for key, value in grouped.items() if np.isfinite(value) and value > 0.0}


def _allowed_station_mask(
    raw: pd.DataFrame,
    allowed_bands: tuple[str, ...],
) -> pd.Series:
    allowed = {str(value).lower().replace(" site", "").strip() for value in allowed_bands}
    station_type = raw.get("StationType", pd.Series("", index=raw.index)).fillna("").astype(str).str.lower()
    station_type = station_type.str.replace(" site", "", regex=False).str.strip()
    bands = raw.get("Bands", pd.Series("", index=raw.index)).fillna("").astype(str).str.lower()
    mask = station_type.isin(allowed)
    for label in allowed:
        if label:
            mask |= bands.str.contains(label, regex=False)
    return mask


def _depot_chargers(
    depot_registry: pd.DataFrame,
    depot_power_kw: dict[str, float],
) -> pd.DataFrame:
    if depot_registry is None or depot_registry.empty:
        return pd.DataFrame(columns=CHARGER_COLUMNS)
    rows: list[dict] = []
    for row in depot_registry.itertuples(index=False):
        depot_id = str(row.depot_id)
        rows.append(
            {
                "station_id": f"depot_{depot_id}",
                "station_kind": "depot",
                "lat": float(row.lat),
                "lon": float(row.lon),
                "lsoa_code": getattr(row, "lsoa_code", np.nan),
                "power_kw": float(depot_power_kw.get(depot_id, 100.0)),
                "attached_depot_id": depot_id,
                "source": "synthetic_depot_charger",
            }
        )
    return pd.DataFrame(rows, columns=CHARGER_COLUMNS)


def _public_chargers(
    ocm_csv_path: Path,
    min_power_kw: float,
    allowed_bands: tuple[str, ...],
) -> pd.DataFrame:
    source_path = Path(ocm_csv_path)
    if not source_path.exists():
        warnings.warn(f"OCM charger CSV missing: {source_path}", RuntimeWarning, stacklevel=2)
        return pd.DataFrame(columns=CHARGER_COLUMNS)
    raw = pd.read_csv(source_path)
    raw["TotalCapacity_kW"] = pd.to_numeric(raw.get("TotalCapacity_kW"), errors="coerce")
    raw["Latitude"] = pd.to_numeric(raw.get("Latitude"), errors="coerce")
    raw["Longitude"] = pd.to_numeric(raw.get("Longitude"), errors="coerce")
    keep = (
        raw["TotalCapacity_kW"].ge(float(min_power_kw))
        & raw["Latitude"].notna()
        & raw["Longitude"].notna()
        & _allowed_station_mask(raw, allowed_bands)
    )
    dropped = int((~keep).sum())
    public = raw.loc[keep].copy()
    out = pd.DataFrame(
        {
            "station_id": public.get("StationID", pd.Series(index=public.index, dtype=object)).astype(str),
            "station_kind": "public",
            "lat": public["Latitude"].astype(float),
            "lon": public["Longitude"].astype(float),
            "lsoa_code": public.get("lsoa_code", pd.Series(np.nan, index=public.index)),
            "power_kw": public["TotalCapacity_kW"].astype(float),
            "attached_depot_id": "",
            "source": "UK_OCM_stations_labeled",
        }
    )
    out.attrs["dropped_public_chargers"] = dropped
    return out.loc[:, CHARGER_COLUMNS].reset_index(drop=True)


def build_charger_registry(
    depot_registry: pd.DataFrame,
    ocm_csv_path: Path = DEFAULT_OCM_PATH,
    min_power_kw: float = 50.0,
    allowed_bands: tuple[str, ...] = ("Fast site", "Rapid", "Ultra-rapid"),
    vehicles: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Combine one synthetic depot charger per depot with filtered OCM sites."""
    depot_power_kw = _load_vehicle_power_by_depot(vehicles)
    depot_rows = _depot_chargers(depot_registry, depot_power_kw)
    public_rows = _public_chargers(ocm_csv_path, min_power_kw, allowed_bands)
    out = pd.concat([depot_rows, public_rows], ignore_index=True)
    if out.empty:
        return pd.DataFrame(columns=CHARGER_COLUMNS)
    out["station_id"] = out["station_id"].astype(str)
    out["power_kw"] = pd.to_numeric(out["power_kw"], errors="coerce")
    out = out.dropna(subset=["lat", "lon", "power_kw"]).copy()
    out = out[out["power_kw"].gt(0.0)]
    return out.loc[:, CHARGER_COLUMNS].reset_index(drop=True)

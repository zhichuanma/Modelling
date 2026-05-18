"""Coach-eligible public charging supply helpers."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
COACH_ELIGIBLE_OCM_BANDS = ("Rapid (50-149 kW)", "Ultra-Rapid (150+ kW)")
DEFAULT_OCM_PATH = ROOT / "data" / "UK_OCM_stations_labeled.csv"
REQUIRED_COLUMNS = {"StationID", "lsoa_code", "TotalCapacity_kW", "Bands"}


def _normalise_band(value: str) -> str:
    return str(value).replace("\u2013", "-").replace(" ", "").strip().lower()


def load_coach_eligible_stations(
    path: str | Path = DEFAULT_OCM_PATH,
    *,
    bands: tuple[str, ...] = COACH_ELIGIBLE_OCM_BANDS,
    min_capacity_kw: float = 50.0,
) -> pd.DataFrame:
    """Load OCM stations that meet the coach-eligible power-band filter."""
    path = Path(path)
    stations = pd.read_csv(path)
    missing = REQUIRED_COLUMNS - set(stations.columns)
    if missing:
        raise ValueError(f"OCM station table at {path} is missing columns: {sorted(missing)}")

    eligible_bands = {_normalise_band(band) for band in bands}
    capacity = pd.to_numeric(stations["TotalCapacity_kW"], errors="coerce")
    band = stations["Bands"].fillna("").astype(str)
    band_mask = band.map(
        lambda value: any(_normalise_band(token) in eligible_bands for token in str(value).split(";"))
    )
    keep = band_mask & capacity.ge(float(min_capacity_kw)) & stations["lsoa_code"].notna()
    out = stations.loc[keep, ["StationID", "lsoa_code", "TotalCapacity_kW"]].copy()
    out["TotalCapacity_kW"] = pd.to_numeric(out["TotalCapacity_kW"], errors="coerce")
    out = out.dropna(subset=["TotalCapacity_kW"])
    out["lsoa_code"] = out["lsoa_code"].astype(str)
    return out.reset_index(drop=True)


def eligible_lsoa_kw(stations: pd.DataFrame) -> pd.Series:
    """Aggregate coach-eligible OCM capacity by LSOA."""
    required = {"lsoa_code", "TotalCapacity_kW"}
    missing = required - set(stations.columns)
    if missing:
        raise ValueError(f"stations is missing columns: {sorted(missing)}")
    if stations.empty:
        return pd.Series(dtype=float, name="TotalCapacity_kW")
    capacity = pd.to_numeric(stations["TotalCapacity_kW"], errors="coerce")
    frame = stations.assign(TotalCapacity_kW=capacity).dropna(subset=["lsoa_code", "TotalCapacity_kW"])
    grouped = frame.groupby(frame["lsoa_code"].astype(str))["TotalCapacity_kW"].sum()
    grouped.name = "TotalCapacity_kW"
    return grouped


__all__ = [
    "COACH_ELIGIBLE_OCM_BANDS",
    "DEFAULT_OCM_PATH",
    "eligible_lsoa_kw",
    "load_coach_eligible_stations",
]

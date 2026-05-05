"""Coach EV specification loading and weighted sampling."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


COACH_FLEET_PATH = Path(__file__).resolve().parents[2] / "data" / "EV_UK_LSOA_2025_with_energy.csv"
COACH_FLEET_COLUMNS = [
    "EV_ID",
    "Model",
    "Energy_kWh",
    "DC_Power_kW",
    "AC_Power_kW",
    "efficiency_wh_per_km",
    "consumption_kwh_per_km",
    "LSOA_code",
    "count",
]


def _require_columns(df: pd.DataFrame, required: set[str], path: Path) -> None:
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"coach fleet table at {path} is missing required columns: {sorted(missing)}")


def load_coach_fleet(path: str | Path = COACH_FLEET_PATH) -> pd.DataFrame:
    """Load coach-only EV specs from the Modelling EV fleet table.

    The upstream EV preparation notebook owns model classification and parameter
    enrichment. The simulator only filters to usable coach rows and derives
    kWh/km from the prepared Wh/km column.
    """
    path = Path(path)
    raw = pd.read_csv(path)
    required = {
        "EV_ID",
        "Model",
        "Energy_kWh",
        "DC_Power_kW",
        "AC_Power_kW",
        "efficiency_wh_per_km",
        "LSOA_code",
        "count",
        "vehicle_subtype",
    }
    _require_columns(raw, required, path)

    subtype = raw["vehicle_subtype"].fillna("").astype(str).str.strip().str.lower()
    fleet = raw.loc[subtype.eq("coach")].copy()
    if fleet.empty:
        raise ValueError(f"No coach rows found in EV fleet table {path}.")

    numeric_cols = ["Energy_kWh", "DC_Power_kW", "AC_Power_kW", "efficiency_wh_per_km", "count"]
    for column in numeric_cols:
        fleet[column] = pd.to_numeric(fleet[column], errors="coerce")

    fleet = fleet.dropna(subset=["Energy_kWh", "efficiency_wh_per_km"])
    fleet = fleet.loc[fleet["Energy_kWh"].gt(0.0) & fleet["efficiency_wh_per_km"].gt(0.0)].copy()
    if fleet.empty:
        raise ValueError(f"No coach rows with positive Energy_kWh and efficiency_wh_per_km in {path}.")

    fleet["consumption_kwh_per_km"] = fleet["efficiency_wh_per_km"] / 1000.0
    fleet.attrs["source_path"] = str(path)
    return fleet[COACH_FLEET_COLUMNS].reset_index(drop=True)


def sample_coach_ev(
    fleet_df: pd.DataFrame,
    rng: np.random.Generator,
    *,
    weight_by_count: bool = True,
) -> pd.Series:
    """Sample one coach EV row, optionally weighted by per-row fleet count."""
    if not isinstance(rng, np.random.Generator):
        raise TypeError("rng must be a numpy.random.Generator.")
    required = {"Energy_kWh", "consumption_kwh_per_km", "count", "Model"}
    missing = required - set(fleet_df.columns)
    if missing:
        raise ValueError(f"fleet_df is missing required columns: {sorted(missing)}")

    candidates = fleet_df.copy()
    candidates["Energy_kWh"] = pd.to_numeric(candidates["Energy_kWh"], errors="coerce")
    candidates["consumption_kwh_per_km"] = pd.to_numeric(
        candidates["consumption_kwh_per_km"],
        errors="coerce",
    )
    candidates = candidates.loc[
        candidates["Energy_kWh"].gt(0.0)
        & candidates["consumption_kwh_per_km"].gt(0.0)
    ].copy()
    if candidates.empty:
        raise ValueError("No coach EV rows with positive battery and consumption are available.")

    if weight_by_count:
        weights = pd.to_numeric(candidates["count"], errors="coerce").fillna(0.0).to_numpy(float)
        if weights.sum() <= 0.0:
            weights = np.ones(len(candidates), dtype=float)
    else:
        weights = np.ones(len(candidates), dtype=float)

    probabilities = weights / weights.sum()
    chosen_pos = int(rng.choice(np.arange(len(candidates)), p=probabilities))
    return candidates.iloc[chosen_pos].copy()

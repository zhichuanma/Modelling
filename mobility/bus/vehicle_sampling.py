"""Weighted bus vehicle-parameter sampling from the prepared model table."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


DEFAULT_VEHICLE_PARAMS_PATH = (
    Path(__file__).resolve().parents[3]
    / "Data"
    / "EV"
    / "EV_prepared"
    / "BEV_Bus_Coach_unique_with_params_with_AC.csv"
)
DEFAULT_FALLBACK_DEPOT_CHARGE_KW = 100.0


def _numeric_column(df: pd.DataFrame, candidates: tuple[str, ...]) -> pd.Series:
    for column in candidates:
        if column in df.columns:
            return pd.to_numeric(df[column], errors="coerce")
    return pd.Series(np.nan, index=df.index, dtype=float)


def load_bus_vehicle_params(
    path: Path = DEFAULT_VEHICLE_PARAMS_PATH,
    *,
    fallback_depot_charge_kw: float = DEFAULT_FALLBACK_DEPOT_CHARGE_KW,
) -> pd.DataFrame:
    """Load the prepared bus/coach model table as a weighted sampling frame."""
    raw = pd.read_csv(path)
    stock = _numeric_column(raw, ("2025 Q2",)).fillna(0.0)
    battery_kwh = _numeric_column(raw, ("energy_capacity_kWh", "battery_capacity_kWh"))

    if "efficiency_wh_per_km" in raw.columns:
        consumption_kwh_per_km = _numeric_column(raw, ("efficiency_wh_per_km",)) / 1000.0
    else:
        consumption_kwh_per_km = _numeric_column(
            raw,
            ("energy_kWh_per_km_ukbc", "overall_energy_kWh_per_km"),
        )

    depot_charge_kw = _numeric_column(raw, ("power_capacity_kW", "ac_charge_power_kW"))
    depot_charge_kw = depot_charge_kw.where(depot_charge_kw > 0.0, fallback_depot_charge_kw)

    params = pd.DataFrame(
        {
            "make": raw.get("Make", pd.Series("", index=raw.index)).astype(str),
            "gen_model": raw.get("GenModel", pd.Series("", index=raw.index)).astype(str),
            "stock_2025_q2": stock.astype(float),
            "battery_kwh": battery_kwh.astype(float),
            "consumption_kwh_per_km": consumption_kwh_per_km.astype(float),
            "depot_charge_kw": depot_charge_kw.astype(float),
            "source_url": raw.get("source_url", pd.Series("", index=raw.index)).fillna("").astype(str),
        }
    )
    valid = (
        params["stock_2025_q2"].gt(0.0)
        & params["battery_kwh"].gt(0.0)
        & params["consumption_kwh_per_km"].gt(0.0)
        & params["depot_charge_kw"].gt(0.0)
    )
    sampling_frame = params.loc[valid].reset_index(drop=True)
    if sampling_frame.empty:
        raise ValueError("No bus vehicle rows have positive stock, battery, consumption, and charge power.")

    total_stock = float(params["stock_2025_q2"].sum())
    valid_stock = float(sampling_frame["stock_2025_q2"].sum())
    sampling_frame.attrs["input_rows"] = int(len(raw))
    sampling_frame.attrs["sampling_rows"] = int(len(sampling_frame))
    sampling_frame.attrs["stock_total"] = total_stock
    sampling_frame.attrs["stock_with_sim_params"] = valid_stock
    sampling_frame.attrs["stock_coverage_pct"] = valid_stock / total_stock * 100.0 if total_stock else np.nan
    sampling_frame.attrs["source_path"] = str(path)
    return sampling_frame


def sample_bus_vehicle_specs(
    vehicle_params: pd.DataFrame,
    rng: np.random.Generator,
    *,
    n: int = 1,
) -> pd.DataFrame:
    """Sample bus vehicle specs with replacement using ``stock_2025_q2`` weights."""
    if n <= 0:
        raise ValueError("n must be positive.")
    required = {
        "make",
        "gen_model",
        "stock_2025_q2",
        "battery_kwh",
        "consumption_kwh_per_km",
        "depot_charge_kw",
    }
    missing = required - set(vehicle_params.columns)
    if missing:
        raise ValueError(f"vehicle_params is missing required columns: {sorted(missing)}")

    weights = pd.to_numeric(vehicle_params["stock_2025_q2"], errors="coerce").fillna(0.0).to_numpy(float)
    if weights.sum() <= 0.0:
        raise ValueError("vehicle_params stock_2025_q2 weights must sum to a positive value.")
    probabilities = weights / weights.sum()
    sampled_index = rng.choice(vehicle_params.index.to_numpy(), size=n, replace=True, p=probabilities)
    sampled = vehicle_params.loc[sampled_index].reset_index(drop=True).copy()
    sampled.insert(0, "sample_id", np.arange(n, dtype=int))
    return sampled

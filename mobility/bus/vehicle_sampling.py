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
DEFAULT_SUBTYPE_LOOKUP_PATH = (
    Path(__file__).resolve().parents[3]
    / "Data"
    / "EV"
    / "manual"
    / "genmodel_subtype_lookup.csv"
)
DEFAULT_INCLUDE_SUBTYPES: tuple[str, ...] = ("bus", "minibus", "unknown")
DEFAULT_FALLBACK_DEPOT_CHARGE_KW = 100.0


def _numeric_column(df: pd.DataFrame, candidates: tuple[str, ...]) -> pd.Series:
    for column in candidates:
        if column in df.columns:
            return pd.to_numeric(df[column], errors="coerce")
    return pd.Series(np.nan, index=df.index, dtype=float)


def _load_subtype_lookup(path: Path) -> dict[str, str]:
    """Read the GenModel->subtype lookup as a normalised dict.

    Keys and values are stripped + uppercased on the key side, lowercased on
    the value side, so callers can rely on case-insensitive matches.
    """
    raw = pd.read_csv(path)
    if not {"GenModel", "subtype"}.issubset(raw.columns):
        raise ValueError(
            f"subtype lookup at {path} must have GenModel and subtype columns; got {list(raw.columns)}"
        )
    keys = raw["GenModel"].astype(str).str.strip().str.upper()
    values = raw["subtype"].astype(str).str.strip().str.lower()
    return dict(zip(keys, values))


def load_bus_vehicle_params(
    path: Path = DEFAULT_VEHICLE_PARAMS_PATH,
    *,
    fallback_depot_charge_kw: float = DEFAULT_FALLBACK_DEPOT_CHARGE_KW,
    subtype_lookup_path: Path | None = DEFAULT_SUBTYPE_LOOKUP_PATH,
    include_subtypes: tuple[str, ...] = DEFAULT_INCLUDE_SUBTYPES,
) -> pd.DataFrame:
    """Load the prepared bus/coach model table as a weighted sampling frame.

    Pass ``subtype_lookup_path=None`` to skip subtype filtering for backwards-
    compatible tests. ``include_subtypes`` controls which lookup subtypes are
    retained, using case-insensitive values normalised to lowercase.
    """
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
    if subtype_lookup_path is not None:
        lookup = _load_subtype_lookup(subtype_lookup_path)
        lookup_keys = params["gen_model"].astype(str).str.strip().str.upper()
        params["subtype"] = lookup_keys.map(lookup).fillna("unknown")
    else:
        params["subtype"] = "unknown"

    allowed = tuple(subtype.strip().lower() for subtype in include_subtypes)
    valid = (
        params["stock_2025_q2"].gt(0.0)
        & params["battery_kwh"].gt(0.0)
        & params["consumption_kwh_per_km"].gt(0.0)
        & params["depot_charge_kw"].gt(0.0)
        & params["subtype"].isin(allowed)
    )
    sampling_frame = params.loc[valid].reset_index(drop=True)
    sampling_frame.attrs["subtype_lookup_path"] = str(subtype_lookup_path) if subtype_lookup_path is not None else None
    sampling_frame.attrs["include_subtypes"] = tuple(allowed)
    dropped = params.loc[~params["subtype"].isin(allowed), "subtype"].value_counts()
    dropped_by_subtype = {str(subtype): int(count) for subtype, count in dropped.items()}
    sampling_frame.attrs["dropped_by_subtype"] = dropped_by_subtype
    if sampling_frame.empty:
        raise ValueError(
            f"No bus vehicle rows survive subtype filter "
            f"(lookup={subtype_lookup_path}, include={allowed}); "
            f"dropped counts: {dropped_by_subtype}"
        )

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

"""Coach EV specification loading and weighted sampling."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_VEHICLE_PARAMS_PATH = (
    PROJECT_ROOT
    / "Data"
    / "EV"
    / "EV_prepared"
    / "BEV_Bus_Coach_unique_with_params_with_AC.csv"
)
DEFAULT_VARIANTS_PATH = (
    PROJECT_ROOT
    / "Data"
    / "EV"
    / "EV_prepared"
    / "BEV_Bus_Coach_unique_with_params.csv"
)
DEFAULT_SUBTYPE_LOOKUP_PATH = PROJECT_ROOT / "Data" / "EV" / "manual" / "genmodel_subtype_lookup.csv"
DEFAULT_MANUAL_WORKLIST_PATH = PROJECT_ROOT / "Data" / "EV" / "manual" / "manual_lookup_worklist.csv"


def _numeric_column(df: pd.DataFrame, candidates: tuple[str, ...]) -> pd.Series:
    for column in candidates:
        if column in df.columns:
            return pd.to_numeric(df[column], errors="coerce")
    return pd.Series(np.nan, index=df.index, dtype=float)


def _string_column(df: pd.DataFrame, column: str) -> pd.Series:
    if column in df.columns:
        return df[column].fillna("").astype(str)
    return pd.Series("", index=df.index, dtype=str)


def _load_subtype_lookup(path: Path | None) -> dict[str, str]:
    if path is None or not Path(path).exists():
        return {}
    raw = pd.read_csv(path)
    if not {"GenModel", "subtype"}.issubset(raw.columns):
        raise ValueError(f"subtype lookup at {path} must have GenModel and subtype columns.")
    keys = raw["GenModel"].astype(str).str.strip().str.upper()
    values = raw["subtype"].astype(str).str.strip().str.lower()
    return dict(zip(keys, values))


def _first_variant(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, list) or not parsed:
        return {}
    first = parsed[0]
    return first if isinstance(first, dict) else {}


def _load_variant_fallbacks(path: Path | None) -> pd.DataFrame:
    if path is None or not Path(path).exists():
        return pd.DataFrame(columns=["gen_model", "variant_battery_kwh", "variant_consumption_kwh_per_km", "variant_source_url"])
    raw = pd.read_csv(path, encoding="utf-8-sig")
    if "GenModel" not in raw.columns or "variants_json" not in raw.columns:
        return pd.DataFrame(columns=["gen_model", "variant_battery_kwh", "variant_consumption_kwh_per_km", "variant_source_url"])
    rows: list[dict[str, Any]] = []
    for _, row in raw.iterrows():
        variant = _first_variant(row.get("variants_json"))
        rows.append(
            {
                "gen_model": str(row["GenModel"]).strip().upper(),
                "variant_battery_kwh": pd.to_numeric(variant.get("battery_kWh"), errors="coerce"),
                "variant_consumption_kwh_per_km": pd.to_numeric(
                    variant.get("energy_kWh_per_km_ukbc"),
                    errors="coerce",
                ),
                "variant_source_url": str(variant.get("source_url", "") or ""),
            }
        )
    return pd.DataFrame(rows)


def _load_manual_consumption(path: Path | None) -> pd.DataFrame:
    if path is None or not Path(path).exists():
        return pd.DataFrame(columns=["gen_model", "manual_consumption_kwh_per_km", "manual_source"])
    raw = pd.read_csv(path)
    if "GenModel" not in raw.columns or "manual_wh_per_km" not in raw.columns:
        return pd.DataFrame(columns=["gen_model", "manual_consumption_kwh_per_km", "manual_source"])
    return pd.DataFrame(
        {
            "gen_model": raw["GenModel"].astype(str).str.strip().str.upper(),
            "manual_consumption_kwh_per_km": pd.to_numeric(raw["manual_wh_per_km"], errors="coerce") / 1000.0,
            "manual_source": raw.get("manual_source", pd.Series("", index=raw.index)).fillna("").astype(str),
        }
    )


def _source_label(primary: pd.Series, fallback: pd.Series, primary_label: str, fallback_label: str) -> pd.Series:
    return np.where(primary.notna() & primary.gt(0.0), primary_label, np.where(fallback.notna() & fallback.gt(0.0), fallback_label, "missing"))


def load_coach_fleet(
    path: str | Path = DEFAULT_VEHICLE_PARAMS_PATH,
    *,
    subtype_lookup_path: str | Path | None = DEFAULT_SUBTYPE_LOOKUP_PATH,
    variants_path: str | Path | None = DEFAULT_VARIANTS_PATH,
    manual_worklist_path: str | Path | None = DEFAULT_MANUAL_WORKLIST_PATH,
) -> pd.DataFrame:
    """Load coach-only EV specs, preserving source labels for imputed fields."""
    raw = pd.read_csv(path)
    lookup = _load_subtype_lookup(Path(subtype_lookup_path) if subtype_lookup_path is not None else None)

    gen_model_key = _string_column(raw, "GenModel").str.strip().str.upper()
    subtype = gen_model_key.map(lookup).fillna("unknown") if lookup else pd.Series("unknown", index=raw.index)
    coach_raw = raw.loc[subtype.eq("coach")].copy()
    if coach_raw.empty:
        raise ValueError(f"No coach rows found in vehicle table {path}.")

    coach_raw["_gen_model_key"] = coach_raw["GenModel"].astype(str).str.strip().str.upper()
    variants = _load_variant_fallbacks(Path(variants_path) if variants_path is not None else None)
    manual = _load_manual_consumption(Path(manual_worklist_path) if manual_worklist_path is not None else None)
    merged = coach_raw.merge(variants, left_on="_gen_model_key", right_on="gen_model", how="left")
    merged = merged.merge(manual, left_on="_gen_model_key", right_on="gen_model", how="left", suffixes=("", "_manual"))

    prepared_battery = _numeric_column(merged, ("energy_capacity_kWh", "battery_capacity_kWh"))
    variant_battery = pd.to_numeric(merged.get("variant_battery_kwh"), errors="coerce")
    battery_kwh = prepared_battery.where(prepared_battery.gt(0.0), variant_battery)

    if "efficiency_wh_per_km" in merged.columns:
        prepared_consumption = _numeric_column(merged, ("efficiency_wh_per_km",)) / 1000.0
    else:
        prepared_consumption = _numeric_column(merged, ("energy_kWh_per_km_ukbc", "overall_energy_kWh_per_km"))
    manual_consumption = pd.to_numeric(merged.get("manual_consumption_kwh_per_km"), errors="coerce")
    variant_consumption = pd.to_numeric(merged.get("variant_consumption_kwh_per_km"), errors="coerce")
    consumption = prepared_consumption.where(prepared_consumption.gt(0.0), manual_consumption)
    consumption = consumption.where(consumption.gt(0.0), variant_consumption)

    charge_kw = _numeric_column(merged, ("power_capacity_kW", "ac_charge_power_kW"))
    stock = _numeric_column(merged, ("2025 Q2",)).fillna(0.0)

    battery_source = _source_label(prepared_battery, variant_battery, "prepared", "variant_json")
    consumption_source = np.where(
        prepared_consumption.notna() & prepared_consumption.gt(0.0),
        "prepared",
        np.where(
            manual_consumption.notna() & manual_consumption.gt(0.0),
            "manual_worklist",
            np.where(variant_consumption.notna() & variant_consumption.gt(0.0), "variant_json", "missing"),
        ),
    )
    range_km = battery_kwh / consumption
    range_km = range_km.where(np.isfinite(range_km), np.nan)

    fleet = pd.DataFrame(
        {
            "make": _string_column(merged, "Make"),
            "gen_model": _string_column(merged, "GenModel"),
            "model": _string_column(merged, "GenModel"),
            "vehicle_subtype": "coach",
            "count": stock.astype(float),
            "battery_kwh": battery_kwh.astype(float),
            "consumption_kwh_per_km": consumption.astype(float),
            "range_km": range_km.astype(float),
            "charge_power_kw": charge_kw.astype(float),
            "battery_source": battery_source,
            "consumption_source": consumption_source,
            "source_url": _string_column(merged, "source_url").where(
                _string_column(merged, "source_url").str.strip().ne(""),
                _string_column(merged, "variant_source_url"),
            ),
        }
    )
    fleet["is_simulatable"] = (
        fleet["count"].gt(0.0)
        & fleet["battery_kwh"].gt(0.0)
        & fleet["consumption_kwh_per_km"].gt(0.0)
    )
    fleet.attrs["source_path"] = str(path)
    fleet.attrs["subtype_lookup_path"] = str(subtype_lookup_path) if subtype_lookup_path is not None else None
    fleet.attrs["variants_path"] = str(variants_path) if variants_path is not None else None
    fleet.attrs["manual_worklist_path"] = str(manual_worklist_path) if manual_worklist_path is not None else None
    fleet.attrs["simulatable_rows"] = int(fleet["is_simulatable"].sum())
    return fleet.reset_index(drop=True)


def sample_coach_ev(
    rng: np.random.Generator,
    weight_by_count: bool = True,
    fleet: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Sample one simulatable coach EV spec."""
    if fleet is None:
        fleet = load_coach_fleet()
    required = {"model", "battery_kwh", "consumption_kwh_per_km", "range_km", "count", "is_simulatable"}
    missing = required - set(fleet.columns)
    if missing:
        raise ValueError(f"fleet is missing required columns: {sorted(missing)}")
    candidates = fleet.loc[fleet["is_simulatable"]].copy()
    if candidates.empty:
        raise ValueError("No simulatable coach EV rows are available.")

    if weight_by_count:
        weights = pd.to_numeric(candidates["count"], errors="coerce").fillna(0.0).to_numpy(float)
        if weights.sum() <= 0.0:
            weights = np.ones(len(candidates), dtype=float)
    else:
        weights = np.ones(len(candidates), dtype=float)
    probabilities = weights / weights.sum()
    chosen_pos = int(rng.choice(np.arange(len(candidates)), p=probabilities))
    row = candidates.iloc[chosen_pos]
    return {
        "model": str(row["model"]),
        "make": str(row.get("make", "")),
        "gen_model": str(row.get("gen_model", row["model"])),
        "battery_kwh": float(row["battery_kwh"]),
        "consumption_kwh_per_km": float(row["consumption_kwh_per_km"]),
        "range_km": float(row["range_km"]),
        "count": float(row["count"]),
        "weight": float(probabilities[chosen_pos]),
        "charge_power_kw": float(row.get("charge_power_kw", np.nan)),
        "battery_source": str(row.get("battery_source", "")),
        "consumption_source": str(row.get("consumption_source", "")),
    }

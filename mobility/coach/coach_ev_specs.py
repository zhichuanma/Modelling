"""EV coach specification pool for the coach depot-load pipeline.

Mirrors :func:`mobility.bus.annual_ev_specs.build_ev_bus_specs` with the coach
decisions of 2026-06-05:

- Row-as-vehicle: the prepared inventory already enumerates one row per EV_ID
  (verified: within every (LSOA, Model) group the row count equals the
  broadcast ``count`` value). ``count`` stays audit-only — never expanded or
  summed (summing it double-counts: 947 vs the true 201 coaches).
- TC9 imputation: the 28 YUTONG TC9 rows lack Energy_kWh and DC_Power_kW; they
  are imputed from the YUTONG TC12 sibling (same 810 Wh/km efficiency class):
  battery 281 kWh, DC 150 kW — flagged ``battery_source="imputed_from_tc12"``
  so the run summary can disclose the 13.9% imputed share.
- Charge channel: the downstream uses the ``ac_charge_kw_max`` column as the
  vehicle-side charging cap. ``charge_side="dc"`` (default) loads the DC
  capability into that column (coach depots assumed DC; effective power =
  min(DC 150, --depot-power-kw 100) = 100 kW); ``charge_side="ac"`` is the
  conservative 22 kW sensitivity (note: a 563 kWh GTE14 cannot refill
  overnight at 22 kW).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from mobility.bus.annual_ev_specs import DEFAULT_USABLE_SOC_MAX, DEFAULT_USABLE_SOC_MIN, _column, _normalised_subtype


COACH_SUBTYPES = ("coach",)
COACH_CONSUMPTION_BOUNDS = (0.6, 1.5)
TC9_IMPUTE_DONOR_MODEL = "YUTONG TC12"
TC9_MODEL = "YUTONG TC9"


def build_ev_coach_specs(
    inventory: pd.DataFrame | str | Path,
    *,
    usable_soc_min: float = DEFAULT_USABLE_SOC_MIN,
    usable_soc_max: float = DEFAULT_USABLE_SOC_MAX,
    charge_side: str = "dc",
    impute_tc9: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build one EV-coach spec row per inventory row (count audit-only)."""
    if charge_side not in ("ac", "dc"):
        raise ValueError(f"charge_side must be 'ac' or 'dc', got {charge_side!r}.")
    raw = pd.read_csv(inventory) if isinstance(inventory, (str, Path)) else inventory.copy()
    subtype = _normalised_subtype(_column(raw, ("vehicle_subtype", "subtype"), default=""))
    coach_mask = subtype.isin(COACH_SUBTYPES)
    coach = raw.loc[coach_mask].copy().reset_index(drop=False).rename(columns={"index": "source_row_id"})
    coach_subtype = subtype.loc[coach_mask].reset_index(drop=True)

    ev_id = _column(coach, ("EV_ID", "ev_id", "vehicle_id"), default="")
    model = _column(coach, ("Model", "model", "vehicle_model", "GenModel"), default="").fillna("").astype(str)
    source_lsoa = _column(coach, ("LSOA_code", "source_lsoa", "lsoa_code"), default="")
    count_col = pd.to_numeric(_column(coach, ("count",), default=np.nan), errors="coerce")
    battery_kwh = pd.to_numeric(_column(coach, ("Energy_kWh", "energy_kwh", "battery_kwh"), default=np.nan), errors="coerce")
    ac_kw = pd.to_numeric(_column(coach, ("AC_Power_kW", "ac_charge_kw_max"), default=np.nan), errors="coerce")
    dc_kw = pd.to_numeric(_column(coach, ("DC_Power_kW", "dc_charge_kw_max"), default=np.nan), errors="coerce")
    efficiency_wh_per_km = pd.to_numeric(_column(coach, ("efficiency_wh_per_km",), default=np.nan), errors="coerce")
    consumption = efficiency_wh_per_km / 1000.0

    battery_source = pd.Series("inventory", index=coach.index, dtype=object)
    if impute_tc9:
        donor = coach.loc[model.str.upper().eq(TC9_IMPUTE_DONOR_MODEL)]
        donor_battery = pd.to_numeric(_column(donor, ("Energy_kWh",), default=np.nan), errors="coerce").dropna()
        donor_dc = pd.to_numeric(_column(donor, ("DC_Power_kW",), default=np.nan), errors="coerce").dropna()
        if not donor_battery.empty:
            tc9_mask = model.str.upper().eq(TC9_MODEL) & battery_kwh.isna()
            battery_kwh = battery_kwh.where(~tc9_mask, float(donor_battery.iloc[0]))
            if not donor_dc.empty:
                dc_kw = dc_kw.where(~(model.str.upper().eq(TC9_MODEL) & dc_kw.isna()), float(donor_dc.iloc[0]))
            battery_source = battery_source.where(~tc9_mask, "imputed_from_tc12")

    vehicle_side_kw = dc_kw if charge_side == "dc" else ac_kw

    specs = pd.DataFrame(
        {
            "vehicle_spec_id": [
                f"evcoach_{str(value).strip()}" if str(value).strip() else f"evcoach_row_{row_id}"
                for value, row_id in zip(ev_id, coach["source_row_id"])
            ],
            "source_ev_id": ev_id.fillna("").astype(str),
            "vehicle_model": model,
            "vehicle_subtype": coach_subtype.astype(str),
            "source_lsoa": source_lsoa.fillna("").astype(str),
            "battery_kwh": battery_kwh.astype(float),
            "battery_source": battery_source.astype(str),
            "consumption_kwh_per_km": consumption.astype(float),
            # Downstream vehicle-side charging cap column (see module docstring).
            "ac_charge_kw_max": vehicle_side_kw.astype(float),
            "dc_charge_kw_max": dc_kw.astype(float),
            "ac_charge_kw_max_inventory": ac_kw.astype(float),
            "charge_side": str(charge_side),
            "usable_soc_min": float(usable_soc_min),
            "usable_soc_max": float(usable_soc_max),
            "source_row_id": coach["source_row_id"].astype(int),
            "source_count": count_col.astype(float),
            "spec_weight": 1.0,
        }
    )
    duplicate_ids = specs["vehicle_spec_id"].duplicated(keep=False)
    if duplicate_ids.any():
        specs.loc[duplicate_ids, "vehicle_spec_id"] = [
            f"{spec_id}_{row_id}" for spec_id, row_id in specs.loc[duplicate_ids, ["vehicle_spec_id", "source_row_id"]].itertuples(index=False)
        ]

    low, high = COACH_CONSUMPTION_BOUNDS
    valid = (
        specs["battery_kwh"].gt(0.0)
        & specs["consumption_kwh_per_km"].between(low, high, inclusive="both")
        & specs["ac_charge_kw_max"].gt(0.0)
        & (float(usable_soc_min) < float(usable_soc_max))
    )
    diagnostics = specs.copy()
    diagnostics["sanity_valid"] = valid
    diagnostics["drop_reason"] = np.where(
        diagnostics["battery_kwh"].le(0.0) | diagnostics["battery_kwh"].isna(),
        "invalid_battery_kwh",
        np.where(
            ~diagnostics["consumption_kwh_per_km"].between(low, high, inclusive="both"),
            "invalid_consumption_kwh_per_km",
            np.where(
                diagnostics["ac_charge_kw_max"].le(0.0) | diagnostics["ac_charge_kw_max"].isna(),
                "invalid_charge_kw_max",
                np.where(float(usable_soc_min) >= float(usable_soc_max), "invalid_usable_soc_window", ""),
            ),
        ),
    )
    out = specs.loc[valid].reset_index(drop=True)
    return out, diagnostics


def coach_ev_specs_summary(raw_inventory: pd.DataFrame, specs: pd.DataFrame, diagnostics: pd.DataFrame) -> dict[str, Any]:
    subtype = _normalised_subtype(_column(raw_inventory, ("vehicle_subtype", "subtype"), default=""))
    coach_mask = subtype.isin(COACH_SUBTYPES)
    consumption = diagnostics["consumption_kwh_per_km"] if "consumption_kwh_per_km" in diagnostics else pd.Series(dtype=float)
    n_imputed = int(specs["battery_source"].eq("imputed_from_tc12").sum()) if "battery_source" in specs.columns else 0
    return {
        "n_ev_rows_raw": int(len(raw_inventory)),
        "n_ev_rows_coach": int(coach_mask.sum()),
        "n_coach_specs_valid_after_sanity": int(len(specs)),
        "n_coach_specs_dropped_by_sanity": int((~diagnostics["sanity_valid"]).sum()) if not diagnostics.empty else 0,
        "n_battery_imputed_from_tc12": n_imputed,
        "battery_imputed_share": float(n_imputed / len(specs)) if len(specs) else np.nan,
        "charge_side": str(specs["charge_side"].iloc[0]) if not specs.empty else "",
        "min_consumption_kwh_per_km": float(consumption.min()) if not consumption.empty else np.nan,
        "max_consumption_kwh_per_km": float(consumption.max()) if not consumption.empty else np.nan,
        "count_column_interpretation": "audit_only_not_expanded",
        "count_sum_double_counts": True,
        "ev_id_is_unique": bool(_column(raw_inventory, ("EV_ID", "ev_id"), default="").dropna().astype(str).is_unique),
    }


__all__ = [
    "COACH_CONSUMPTION_BOUNDS",
    "COACH_SUBTYPES",
    "build_ev_coach_specs",
    "coach_ev_specs_summary",
]

"""EV bus specification pool for the annual depot-load pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


DEFAULT_USABLE_SOC_MIN = 0.10
DEFAULT_USABLE_SOC_MAX = 0.95
VALID_SUBTYPES = ("bus", "minibus")


def _column(raw: pd.DataFrame, candidates: tuple[str, ...], *, default: Any = np.nan) -> pd.Series:
    for column in candidates:
        if column in raw.columns:
            return raw[column]
    return pd.Series(default, index=raw.index)


def _normalised_subtype(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.strip().str.lower()


def build_ev_bus_specs(
    inventory: pd.DataFrame | str | Path,
    *,
    usable_soc_min: float = DEFAULT_USABLE_SOC_MIN,
    usable_soc_max: float = DEFAULT_USABLE_SOC_MAX,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build one EV-bus spec row per inventory row.

    ``count`` is intentionally not expanded or summed. It is carried only as an
    audit field because the prepared inventory already stores one row per EV_ID
    in the modelling workflow.
    """
    raw = pd.read_csv(inventory) if isinstance(inventory, (str, Path)) else inventory.copy()
    subtype = _normalised_subtype(_column(raw, ("vehicle_subtype", "subtype"), default=""))
    bus_mask = subtype.isin(VALID_SUBTYPES)
    bus = raw.loc[bus_mask].copy().reset_index(drop=False).rename(columns={"index": "source_row_id"})
    bus_subtype = subtype.loc[bus_mask].reset_index(drop=True)

    ev_id = _column(bus, ("EV_ID", "ev_id", "vehicle_id"), default="")
    model = _column(bus, ("Model", "model", "vehicle_model", "GenModel"), default="")
    source_lsoa = _column(bus, ("LSOA_code", "source_lsoa", "lsoa_code"), default="")
    count_col = pd.to_numeric(_column(bus, ("count",), default=np.nan), errors="coerce")
    battery_kwh = pd.to_numeric(_column(bus, ("Energy_kWh", "energy_kwh", "battery_kwh", "energy_capacity_kWh"), default=np.nan), errors="coerce")
    ac_kw = pd.to_numeric(_column(bus, ("AC_Power_kW", "ac_charge_kw_max", "ac_charge_power_kW", "power_capacity_kW"), default=np.nan), errors="coerce")
    dc_kw = pd.to_numeric(_column(bus, ("DC_Power_kW", "dc_charge_kw_max"), default=np.nan), errors="coerce")
    efficiency_wh_per_km = pd.to_numeric(_column(bus, ("efficiency_wh_per_km",), default=np.nan), errors="coerce")
    if efficiency_wh_per_km.notna().any():
        consumption = efficiency_wh_per_km / 1000.0
    else:
        consumption = pd.to_numeric(
            _column(bus, ("energy_kWh_per_km_ukbc", "overall_energy_kWh_per_km", "consumption_kwh_per_km"), default=np.nan),
            errors="coerce",
        )

    specs = pd.DataFrame(
        {
            "vehicle_spec_id": [
                f"evspec_{str(value).strip()}" if str(value).strip() else f"evspec_row_{row_id}"
                for value, row_id in zip(ev_id, bus["source_row_id"])
            ],
            "source_ev_id": ev_id.fillna("").astype(str),
            "vehicle_model": model.fillna("").astype(str),
            "vehicle_subtype": bus_subtype.astype(str),
            "source_lsoa": source_lsoa.fillna("").astype(str),
            "battery_kwh": battery_kwh.astype(float),
            "consumption_kwh_per_km": consumption.astype(float),
            "ac_charge_kw_max": ac_kw.astype(float),
            "dc_charge_kw_max": dc_kw.astype(float),
            "usable_soc_min": float(usable_soc_min),
            "usable_soc_max": float(usable_soc_max),
            "source_row_id": bus["source_row_id"].astype(int),
            "source_count": count_col.astype(float),
            "spec_weight": 1.0,
        }
    )
    duplicate_ids = specs["vehicle_spec_id"].duplicated(keep=False)
    if duplicate_ids.any():
        specs.loc[duplicate_ids, "vehicle_spec_id"] = [
            f"{spec_id}_{row_id}" for spec_id, row_id in specs.loc[duplicate_ids, ["vehicle_spec_id", "source_row_id"]].itertuples(index=False)
        ]

    valid = (
        specs["battery_kwh"].gt(0.0)
        & specs["consumption_kwh_per_km"].between(0.7, 3.0, inclusive="both")
        & specs["ac_charge_kw_max"].gt(0.0)
        & (float(usable_soc_min) < float(usable_soc_max))
    )
    diagnostics = specs.copy()
    diagnostics["sanity_valid"] = valid
    diagnostics["drop_reason"] = np.where(
        diagnostics["battery_kwh"].le(0.0) | diagnostics["battery_kwh"].isna(),
        "invalid_battery_kwh",
        np.where(
            ~diagnostics["consumption_kwh_per_km"].between(0.7, 3.0, inclusive="both"),
            "invalid_consumption_kwh_per_km",
            np.where(
                diagnostics["ac_charge_kw_max"].le(0.0) | diagnostics["ac_charge_kw_max"].isna(),
                "invalid_ac_charge_kw_max",
                np.where(float(usable_soc_min) >= float(usable_soc_max), "invalid_usable_soc_window", ""),
            ),
        ),
    )
    out = specs.loc[valid].reset_index(drop=True)
    return out, diagnostics


def ev_specs_summary(raw_inventory: pd.DataFrame, specs: pd.DataFrame, diagnostics: pd.DataFrame) -> dict[str, Any]:
    subtype = _normalised_subtype(_column(raw_inventory, ("vehicle_subtype", "subtype"), default=""))
    bus_mask = subtype.isin(VALID_SUBTYPES)
    consumption = diagnostics["consumption_kwh_per_km"] if "consumption_kwh_per_km" in diagnostics else pd.Series(dtype=float)
    return {
        "n_ev_rows_raw": int(len(raw_inventory)),
        "n_ev_rows_bus_minibus": int(bus_mask.sum()),
        "n_ev_specs_valid_after_sanity": int(len(specs)),
        "n_ev_specs_dropped_by_sanity": int((~diagnostics["sanity_valid"]).sum()) if not diagnostics.empty else 0,
        "min_consumption_kwh_per_km": float(consumption.min()) if not consumption.empty else np.nan,
        "max_consumption_kwh_per_km": float(consumption.max()) if not consumption.empty else np.nan,
        "count_column_interpretation": "audit_only_not_expanded",
        "minibus_row_count": int((subtype == "minibus").sum()),
        "ev_id_is_unique": bool(_column(raw_inventory, ("EV_ID", "ev_id"), default="").dropna().astype(str).is_unique),
        "count_column_present": bool("count" in raw_inventory.columns),
    }


__all__ = [
    "DEFAULT_USABLE_SOC_MAX",
    "DEFAULT_USABLE_SOC_MIN",
    "VALID_SUBTYPES",
    "build_ev_bus_specs",
    "ev_specs_summary",
]

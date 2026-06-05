"""EV bus/minibus instance preparation for depot-only stock scenarios."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


VALID_BUS_SUBTYPES = ("bus", "minibus")
DEFAULT_USABLE_SOC_MIN = 0.10
DEFAULT_USABLE_SOC_MAX = 0.95


def _column(frame: pd.DataFrame, candidates: tuple[str, ...], default: Any = np.nan) -> pd.Series:
    for column in candidates:
        if column in frame.columns:
            return frame[column]
    return pd.Series(default, index=frame.index)


def _clean_subtype(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.strip().str.lower()


def build_ev_bus_instances(
    inventory: pd.DataFrame | str | Path,
    *,
    usable_soc_min: float = DEFAULT_USABLE_SOC_MIN,
    usable_soc_max: float = DEFAULT_USABLE_SOC_MAX,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Treat every valid bus/minibus inventory row as one vehicle instance.

    The prepared inventory is already one row per ``EV_ID``. The ``count`` field
    is retained only as an audit field and is never expanded or summed.
    """
    raw = pd.read_csv(inventory) if isinstance(inventory, (str, Path)) else inventory.copy()
    raw = raw.reset_index(drop=False).rename(columns={"index": "source_row_id"})
    subtype_all = _clean_subtype(_column(raw, ("vehicle_subtype", "subtype"), default=""))
    bus_mask = subtype_all.isin(VALID_BUS_SUBTYPES)
    bus = raw.loc[bus_mask].copy().reset_index(drop=True)
    subtype = subtype_all.loc[bus_mask].reset_index(drop=True)

    ev_id = _column(bus, ("EV_ID", "ev_id", "vehicle_id"), default="")
    model = _column(bus, ("Model", "model", "vehicle_model", "GenModel"), default="")
    source_lsoa = _column(bus, ("LSOA_code", "source_lsoa", "lsoa_code"), default="")
    count = pd.to_numeric(_column(bus, ("count",), default=np.nan), errors="coerce")
    battery = pd.to_numeric(
        _column(bus, ("Energy_kWh", "energy_kwh", "battery_kwh", "energy_capacity_kWh"), default=np.nan),
        errors="coerce",
    )
    ac_kw = pd.to_numeric(
        _column(bus, ("AC_Power_kW", "ac_charge_kw_max", "ac_charge_power_kW", "power_capacity_kW"), default=np.nan),
        errors="coerce",
    )
    dc_kw = pd.to_numeric(_column(bus, ("DC_Power_kW", "dc_charge_kw_max"), default=np.nan), errors="coerce")
    efficiency_wh_per_km = pd.to_numeric(_column(bus, ("efficiency_wh_per_km",), default=np.nan), errors="coerce")
    fallback_per_100 = pd.to_numeric(_column(bus, ("energy_kWh_per_100km",), default=np.nan), errors="coerce") / 100.0
    fallback_per_km = pd.to_numeric(
        _column(bus, ("energy_kWh_per_km_ukbc", "overall_energy_kWh_per_km", "consumption_kwh_per_km"), default=np.nan),
        errors="coerce",
    )
    consumption = np.where(efficiency_wh_per_km.notna(), efficiency_wh_per_km / 1000.0, np.where(fallback_per_100.notna(), fallback_per_100, fallback_per_km))
    consumption = pd.Series(consumption, index=bus.index, dtype="float64")

    instances = pd.DataFrame(
        {
            "vehicle_id": ev_id.fillna("").astype(str),
            "source_row_id": bus["source_row_id"].astype(int),
            "vehicle_model": model.fillna("").astype(str),
            "vehicle_subtype": subtype.astype(str),
            "source_lsoa": source_lsoa.fillna("").astype(str),
            "battery_kwh": battery.astype(float),
            "consumption_kwh_per_km": consumption.astype(float),
            "ac_charge_kw_max": ac_kw.astype(float),
            "dc_charge_kw_max": dc_kw.astype(float),
            "usable_soc_min": float(usable_soc_min),
            "usable_soc_max": float(usable_soc_max),
            "count": count.astype(float),
            "vehicle_instance_weight": 1.0,
        }
    )
    blank_ids = instances["vehicle_id"].str.strip().eq("")
    instances.loc[blank_ids, "vehicle_id"] = [f"ev_row_{row_id}" for row_id in instances.loc[blank_ids, "source_row_id"]]
    duplicate_ids = instances["vehicle_id"].duplicated(keep=False)
    if duplicate_ids.any():
        instances.loc[duplicate_ids, "vehicle_id"] = [
            f"{vehicle_id}_{row_id}"
            for vehicle_id, row_id in instances.loc[duplicate_ids, ["vehicle_id", "source_row_id"]].itertuples(index=False)
        ]

    invalid_battery = instances["battery_kwh"].le(0.0) | instances["battery_kwh"].isna()
    low_consumption = instances["consumption_kwh_per_km"].lt(0.7)
    invalid_consumption = ~instances["consumption_kwh_per_km"].between(0.7, 3.0, inclusive="both")
    invalid_ac = instances["ac_charge_kw_max"].le(0.0) | instances["ac_charge_kw_max"].isna()
    invalid_soc = float(usable_soc_min) >= float(usable_soc_max)
    valid = ~(invalid_battery | invalid_consumption | invalid_ac | invalid_soc)

    diagnostics = instances.copy()
    diagnostics["sanity_valid"] = valid
    diagnostics["drop_reason"] = np.select(
        [
            invalid_battery,
            low_consumption,
            instances["consumption_kwh_per_km"].gt(3.0) | instances["consumption_kwh_per_km"].isna(),
            invalid_ac,
            pd.Series(invalid_soc, index=instances.index),
        ],
        [
            "invalid_battery_kwh",
            "low_consumption_kwh_per_km",
            "invalid_consumption_kwh_per_km",
            "invalid_ac_charge_kw_max",
            "invalid_usable_soc_window",
        ],
        default="",
    )
    valid_instances = instances.loc[valid].reset_index(drop=True)
    invalid_rows = diagnostics.loc[~valid].reset_index(drop=True)

    summary = _summary(raw, subtype_all, bus, valid_instances, diagnostics, low_consumption, invalid_battery)
    return valid_instances, invalid_rows, summary


def _summary(
    raw: pd.DataFrame,
    subtype_all: pd.Series,
    bus: pd.DataFrame,
    valid_instances: pd.DataFrame,
    diagnostics: pd.DataFrame,
    low_consumption: pd.Series,
    invalid_battery: pd.Series,
) -> dict[str, Any]:
    count_matches = None
    if not bus.empty and {"LSOA_code", "Model", "count"}.issubset(bus.columns):
        sizes = bus.groupby(["LSOA_code", "Model"], dropna=False).size().sort_index().astype(float)
        counts = pd.to_numeric(bus.groupby(["LSOA_code", "Model"], dropna=False)["count"].first(), errors="coerce").sort_index().astype(float)
        count_matches = bool(counts.index.equals(sizes.index) and np.allclose(counts.to_numpy(), sizes.to_numpy(), equal_nan=False))
    ev_ids = _column(raw, ("EV_ID", "ev_id", "vehicle_id"), default="")
    minibus_total = int((subtype_all == "minibus").sum())
    return {
        "n_ev_rows_raw": int(len(raw)),
        "n_ev_rows_bus_minibus": int(len(bus)),
        "n_valid_ev_bus_instances": int(len(valid_instances)),
        "n_invalid_vehicle_rows": int(len(diagnostics) - len(valid_instances)),
        "low_consumption_filtered_count": int(low_consumption.fillna(False).sum()),
        "invalid_battery_vehicle_count": int(invalid_battery.fillna(False).sum()),
        "minibus_valid_count": int((valid_instances["vehicle_subtype"] == "minibus").sum()) if not valid_instances.empty else 0,
        "minibus_row_count": minibus_total,
        "minibus_count_note": "minibus count is 0" if minibus_total == 0 else f"minibus rows present: {minibus_total}",
        "ev_id_is_unique": bool(ev_ids.dropna().astype(str).is_unique),
        "count_column_present": bool("count" in raw.columns),
        "count_matches_lsoa_model_group_size": count_matches,
        "count_column_interpretation": "audit_only_not_expanded",
        "vehicle_rows_are_instances": True,
    }


__all__ = [
    "DEFAULT_USABLE_SOC_MAX",
    "DEFAULT_USABLE_SOC_MIN",
    "VALID_BUS_SUBTYPES",
    "build_ev_bus_instances",
]

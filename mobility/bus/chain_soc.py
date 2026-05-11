"""Standalone chain-level SOC walk for M1 diagnostics and resolution."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


MOVEMENT_EVENTS = {
    "passenger_block",
    "depot_deadhead",
    "inter_block_deadhead",
    "return_deadhead",
    "midday_return_deadhead",
    "midday_out_deadhead",
}


def _value(vehicle: pd.Series | dict[str, Any], key: str, default: float) -> float:
    if isinstance(vehicle, pd.Series):
        raw = vehicle.get(key, default)
    else:
        raw = vehicle.get(key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = float(default)
    return value if np.isfinite(value) else float(default)


def chain_soc_walk(
    chain_events: pd.DataFrame,
    vehicle: pd.Series,
    charger_eligibility: pd.DataFrame,
    initial_soc_kwh: float | None = None,
) -> pd.DataFrame:
    """Walk SOC through one chain without clamping negative values."""
    if chain_events is None or chain_events.empty:
        return pd.DataFrame()
    events = chain_events.sort_values("event_seq", kind="stable").copy()
    battery_kwh = _value(vehicle, "battery_kwh", 300.0)
    usable_soc_max = _value(vehicle, "usable_soc_max", 0.95)
    ac_kw = _value(vehicle, "ac_charge_kw_max", 100.0)
    dc_kw = _value(vehicle, "dc_charge_kw_max", 150.0)
    max_soc_kwh = battery_kwh * usable_soc_max
    soc = max_soc_kwh if initial_soc_kwh is None else float(initial_soc_kwh)

    eligibility = pd.DataFrame({"event_seq": events["event_seq"]})
    if charger_eligibility is not None and not charger_eligibility.empty:
        eligibility = eligibility.merge(charger_eligibility, on="event_seq", how="left")
    for column, default in (
        ("eligible", False),
        ("power_kw", 0.0),
        ("station_id", ""),
        ("station_kind", ""),
    ):
        if column not in eligibility.columns:
            eligibility[column] = default
    eligibility["eligible"] = eligibility["eligible"].fillna(False).astype(bool)
    eligibility["power_kw"] = pd.to_numeric(eligibility["power_kw"], errors="coerce").fillna(0.0)
    eligibility["station_id"] = eligibility["station_id"].fillna("").astype(str)
    eligibility["station_kind"] = eligibility["station_kind"].fillna("").astype(str)
    eligibility = eligibility.set_index("event_seq")

    soc_start: list[float] = []
    soc_end: list[float] = []
    charge_added: list[float] = []
    station_ids: list[str] = []
    station_kinds: list[str] = []

    for row in events.itertuples(index=False):
        start_soc = float(soc)
        charge_kwh = 0.0
        station_id = ""
        station_kind = str(getattr(row, "station_kind", "") or "")
        event_type = str(row.event_type)
        if event_type in MOVEMENT_EVENTS:
            soc = soc - float(getattr(row, "energy_kwh_proxy", 0.0) or 0.0)
        else:
            elig = eligibility.loc[row.event_seq] if row.event_seq in eligibility.index else None
            if elig is not None and bool(elig["eligible"]):
                power_kw = float(elig["power_kw"])
                vehicle_power_kw = ac_kw if event_type == "depot_parking" else dc_kw
                effective_kw = min(power_kw, vehicle_power_kw)
                requested_kwh = max(0.0, effective_kw) * float(row.duration_min) / 60.0
                charge_kwh = min(requested_kwh, max(0.0, max_soc_kwh - soc))
                soc = min(soc + charge_kwh, max_soc_kwh)
                if charge_kwh > 0.0:
                    station_id = str(elig["station_id"])
                    eligibility_station_kind = str(elig["station_kind"])
                    if eligibility_station_kind:
                        station_kind = eligibility_station_kind
        soc_start.append(start_soc)
        soc_end.append(float(soc))
        charge_added.append(float(charge_kwh))
        station_ids.append(station_id)
        station_kinds.append(station_kind)

    out = events.copy()
    out["soc_start_kwh"] = soc_start
    out["soc_end_kwh"] = soc_end
    out["charge_kwh_added"] = charge_added
    out["station_id"] = station_ids
    out["station_kind"] = station_kinds
    return out

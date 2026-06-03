"""Aggregate depot-only charging events to 15-minute depot load curves."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .annual_depot_events import DEPOT_CHARGING_EVENT_TYPES


SLOT_MINUTES = 15


def aggregate_depot_load_15min(
    events: pd.DataFrame,
    depot_registry: pd.DataFrame,
    soc_summary: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Aggregate depot charging to true datetime 15-minute slots."""
    grouped_frames: list[pd.DataFrame] = []
    daily_count_frames: list[pd.DataFrame] = []
    if not events.empty:
        charge_events = events[
            events["event_type"].isin(DEPOT_CHARGING_EVENT_TYPES)
            & pd.to_numeric(events["charge_kwh_added"], errors="coerce").fillna(0.0).gt(0.0)
        ].copy()
        for _, charge_chunk in charge_events.groupby("service_date", sort=True, dropna=False):
            slot_records: list[dict[str, Any]] = []
            for row in charge_chunk.itertuples(index=False):
                slot_records.extend(_split_event_to_slots(row))
            if not slot_records:
                continue
            slots = pd.DataFrame.from_records(slot_records)
            daily_count_frames.append(
                slots.groupby(["depot_id", "service_date", "slot_date"], as_index=False, sort=True)
                .agg(n_charging_vehicles=("vehicle_day_id", "nunique"))
            )
            chunk_grouped = (
                slots.groupby(
                    [
                        "depot_id",
                        "depot_lsoa",
                        "service_date",
                        "slot_date",
                        "slot_index",
                        "slot_start_datetime",
                        "slot_end_datetime",
                        "scenario_mode",
                    ],
                    as_index=False,
                    sort=True,
                )
                .agg(
                    charge_kwh=("charge_kwh", "sum"),
                    n_charging_vehicles=("vehicle_day_id", "nunique"),
                )
            )
            grouped_frames.append(chunk_grouped)

    daily_charging_counts = pd.DataFrame(columns=["depot_id", "service_date", "slot_date", "n_charging_vehicles"])
    if grouped_frames:
        daily_charging_counts = pd.concat(daily_count_frames, ignore_index=True)
        daily_charging_counts = (
            daily_charging_counts.groupby(["depot_id", "service_date", "slot_date"], as_index=False, sort=True)
            .agg(n_charging_vehicles=("n_charging_vehicles", "max"))
        )
        grouped = (
            pd.concat(grouped_frames, ignore_index=True)
            .groupby(
                [
                    "depot_id",
                    "depot_lsoa",
                    "service_date",
                    "slot_date",
                    "slot_index",
                    "slot_start_datetime",
                    "slot_end_datetime",
                    "scenario_mode",
                ],
                as_index=False,
                sort=True,
            )
            .agg(
                charge_kwh=("charge_kwh", "sum"),
                n_charging_vehicles=("n_charging_vehicles", "max"),
            )
        )
        grouped["average_kw"] = grouped["charge_kwh"] / (SLOT_MINUTES / 60.0)
    else:
        grouped = pd.DataFrame(columns=_load_columns_without_registry())

    load = _attach_registry_fields(grouped, depot_registry)
    daily = build_depot_daily_summary(load, soc_summary, daily_charging_counts=daily_charging_counts)
    return load.loc[:, _load_columns()].reset_index(drop=True), daily.reset_index(drop=True)


def build_depot_daily_summary(
    load_15min: pd.DataFrame,
    soc_summary: pd.DataFrame | None = None,
    *,
    daily_charging_counts: pd.DataFrame | None = None,
) -> pd.DataFrame:
    if load_15min.empty:
        return pd.DataFrame(columns=_daily_columns())
    daily = (
        load_15min.groupby(
            [
                "depot_id",
                "depot_lsoa",
                "depot_lat",
                "depot_lon",
                "depot_confidence",
                "service_date",
                "slot_date",
            ],
            as_index=False,
            sort=True,
        )
        .agg(
            daily_charge_kwh=("charge_kwh", "sum"),
            daily_peak_kw=("average_kw", "max"),
            n_charging_vehicles=("n_charging_vehicles", "max"),
        )
    )
    if daily_charging_counts is not None and not daily_charging_counts.empty:
        daily = daily.drop(columns=["n_charging_vehicles"]).merge(
            daily_charging_counts,
            on=["depot_id", "service_date", "slot_date"],
            how="left",
        )
    if soc_summary is not None and not soc_summary.empty:
        status = (
            soc_summary.groupby(["depot_id", "service_date"], as_index=False, sort=True)
            .agg(
                n_vehicle_days=("vehicle_day_id", "nunique"),
                n_infeasible_vehicle_days=("depot_only_feasible", lambda s: int((~s.astype(bool)).sum())),
            )
        )
        daily = daily.merge(status, on=["depot_id", "service_date"], how="left")
    else:
        daily["n_vehicle_days"] = np.nan
        daily["n_infeasible_vehicle_days"] = np.nan
    daily["n_vehicle_days"] = daily["n_vehicle_days"].fillna(0).astype(int)
    daily["n_infeasible_vehicle_days"] = daily["n_infeasible_vehicle_days"].fillna(0).astype(int)
    daily["share_infeasible_vehicle_days"] = np.where(
        daily["n_vehicle_days"].gt(0),
        daily["n_infeasible_vehicle_days"] / daily["n_vehicle_days"],
        np.nan,
    )
    return daily.loc[:, _daily_columns()]


def depot_load_energy_matches_events(
    load_15min: pd.DataFrame,
    events: pd.DataFrame,
    *,
    atol: float = 1e-6,
    rtol: float = 1e-9,
) -> bool:
    load_energy = float(pd.to_numeric(load_15min.get("charge_kwh", pd.Series(dtype=float)), errors="coerce").fillna(0.0).sum())
    event_energy = float(
        pd.to_numeric(
            events.loc[events["event_type"].isin(DEPOT_CHARGING_EVENT_TYPES), "charge_kwh_added"]
            if not events.empty and "event_type" in events.columns
            else pd.Series(dtype=float),
            errors="coerce",
        )
        .fillna(0.0)
        .sum()
    )
    return bool(abs(load_energy - event_energy) <= float(atol) + float(rtol) * abs(event_energy))


def _split_event_to_slots(row: Any) -> list[dict[str, Any]]:
    start = pd.Timestamp(row.start_datetime)
    end = pd.Timestamp(getattr(row, "charging_end_datetime", pd.NaT))
    if pd.isna(end):
        end = pd.Timestamp(row.end_datetime)
    end = min(end, pd.Timestamp(row.end_datetime))
    charge = float(getattr(row, "charge_kwh_added", 0.0) or 0.0)
    if charge <= 0.0 or end <= start:
        return []
    duration_min = (end - start).total_seconds() / 60.0
    energy_per_min = charge / duration_min
    cursor = start.floor(f"{SLOT_MINUTES}min")
    records: list[dict[str, Any]] = []
    while cursor < end:
        slot_end = cursor + pd.Timedelta(minutes=SLOT_MINUTES)
        overlap_start = max(start, cursor)
        overlap_end = min(end, slot_end)
        overlap_min = max(0.0, (overlap_end - overlap_start).total_seconds() / 60.0)
        if overlap_min > 0:
            slot_index = int((cursor.hour * 60 + cursor.minute) / SLOT_MINUTES)
            records.append(
                {
                    "depot_id": str(row.depot_id),
                    "depot_lsoa": str(row.depot_lsoa),
                    "service_date": str(row.service_date),
                    "slot_date": cursor.date().isoformat(),
                    "slot_index": slot_index,
                    "slot_start_datetime": cursor,
                    "slot_end_datetime": slot_end,
                    "charge_kwh": overlap_min * energy_per_min,
                    "vehicle_day_id": str(row.vehicle_day_id),
                    "scenario_mode": str(getattr(row, "scenario_mode", "ev_stock_scale")),
                }
            )
        cursor = slot_end
    return records


def _attach_registry_fields(load: pd.DataFrame, depot_registry: pd.DataFrame) -> pd.DataFrame:
    out = load.copy()
    if out.empty:
        for col in ("depot_lat", "depot_lon", "depot_confidence"):
            out[col] = []
        return out
    registry_cols = ["depot_id", "depot_lat", "depot_lon", "depot_confidence"]
    registry = depot_registry.loc[:, [col for col in registry_cols if col in depot_registry.columns]].drop_duplicates("depot_id", keep="first")
    out = out.merge(registry, on="depot_id", how="left")
    return out


def _load_columns_without_registry() -> list[str]:
    return [
        "depot_id",
        "depot_lsoa",
        "service_date",
        "slot_date",
        "slot_index",
        "slot_start_datetime",
        "slot_end_datetime",
        "charge_kwh",
        "n_charging_vehicles",
        "average_kw",
        "scenario_mode",
    ]


def _load_columns() -> list[str]:
    return [
        "depot_id",
        "depot_lsoa",
        "depot_lat",
        "depot_lon",
        "depot_confidence",
        "service_date",
        "slot_date",
        "slot_index",
        "slot_start_datetime",
        "slot_end_datetime",
        "charge_kwh",
        "average_kw",
        "n_charging_vehicles",
        "scenario_mode",
    ]


def _daily_columns() -> list[str]:
    return [
        "depot_id",
        "depot_lsoa",
        "depot_lat",
        "depot_lon",
        "depot_confidence",
        "service_date",
        "slot_date",
        "daily_charge_kwh",
        "daily_peak_kw",
        "n_vehicle_days",
        "n_charging_vehicles",
        "n_infeasible_vehicle_days",
        "share_infeasible_vehicle_days",
    ]


__all__ = [
    "SLOT_MINUTES",
    "aggregate_depot_load_15min",
    "build_depot_daily_summary",
    "depot_load_energy_matches_events",
]

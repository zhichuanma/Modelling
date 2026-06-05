"""Stage 5 depot-only event ledger construction."""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

import numpy as np
import pandas as pd


DEPOT_CHARGING_EVENT_TYPES = {"depot_parking_pre", "depot_parking_midday", "depot_parking_post"}
MOVEMENT_EVENT_TYPES = {"depot_to_block_deadhead", "passenger_block", "passenger_trip", "block_to_depot_deadhead"}
FORBIDDEN_CHARGING_EVENT_TYPES = {"public_charger_event", "opportunity_charging", "terminal_public_charging", "OCM_station_event"}


def haversine_km(lat1: Any, lon1: Any, lat2: Any, lon2: Any) -> float:
    try:
        values = [float(lat1), float(lon1), float(lat2), float(lon2)]
    except (TypeError, ValueError):
        return float("nan")
    if not all(np.isfinite(values)):
        return float("nan")
    radius_km = 6371.0088
    phi1, phi2 = math.radians(values[0]), math.radians(values[2])
    d_phi = math.radians(values[2] - values[0])
    d_lam = math.radians(values[3] - values[1])
    a = math.sin(d_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2.0) ** 2
    return float(2.0 * radius_km * math.asin(min(1.0, math.sqrt(a))))


def datetime_from_service_hour(service_date: str | dt.date | pd.Timestamp, hour: float) -> pd.Timestamp:
    base = pd.Timestamp(service_date).normalize()
    seconds = int(round(float(hour) * 3600.0))
    return base + pd.to_timedelta(seconds, unit="s")


def build_vehicle_day_events(
    simulation_cases: pd.DataFrame,
    *,
    depot_power_kw: float = 100.0,
    deadhead_speed_kmh: float = 30.0,
    use_trip_level_events: bool = True,
) -> pd.DataFrame:
    if simulation_cases.empty:
        return pd.DataFrame(columns=_event_columns())
    records: list[dict[str, Any]] = []
    for row in simulation_cases.itertuples(index=False):
        records.extend(
            _events_for_case(
                row,
                depot_power_kw=float(depot_power_kw),
                deadhead_speed_kmh=float(deadhead_speed_kmh),
                use_trip_level_events=use_trip_level_events,
            )
        )
    events = pd.DataFrame.from_records(records, columns=_event_columns())
    if not events.empty:
        events = events.sort_values(["simulation_case_id", "start_datetime", "event_seq"], kind="stable").reset_index(drop=True)
        events["event_seq"] = events.groupby("simulation_case_id", sort=False).cumcount()
    return events


def _events_for_case(row: Any, *, depot_power_kw: float, deadhead_speed_kmh: float, use_trip_level_events: bool) -> list[dict[str, Any]]:
    service_date = str(getattr(row, "service_date", "2026-06-03"))
    start_dt = datetime_from_service_hour(service_date, float(_get(row, "start_h", 0.0)))
    end_dt = datetime_from_service_hour(service_date, float(_get(row, "end_h", 0.0)))
    depot_lat = float(_get(row, "depot_lat", np.nan))
    depot_lon = float(_get(row, "depot_lon", np.nan))
    start_lat = float(_get(row, "start_lat", np.nan))
    start_lon = float(_get(row, "start_lon", np.nan))
    end_lat = float(_get(row, "end_lat", np.nan))
    end_lon = float(_get(row, "end_lon", np.nan))
    depot_lsoa = _clean(_get(row, "operational_depot_lsoa", ""))
    start_lsoa = _clean(_get(row, "start_lsoa", ""))
    end_lsoa = _clean(_get(row, "end_lsoa", ""))
    effective_kw = max(0.0, min(float(_get(row, "ac_charge_kw_max", 0.0)), float(depot_power_kw)))
    base = _base(row, depot_power_kw=depot_power_kw, effective_kw=effective_kw)

    to_block_km = haversine_km(depot_lat, depot_lon, start_lat, start_lon)
    to_block_h = 0.0 if not np.isfinite(to_block_km) else to_block_km / deadhead_speed_kmh
    deadhead_start = start_dt - pd.to_timedelta(to_block_h, unit="h")
    service_midnight = pd.Timestamp(service_date).normalize()
    pre_start = min(service_midnight, deadhead_start)
    pre_end = max(pre_start, deadhead_start)
    out: list[dict[str, Any]] = [
        _event(base, 0, "depot_parking_pre", pre_start, pre_end, depot_lat, depot_lon, depot_lat, depot_lon, depot_lsoa, depot_lsoa, 0.0, "none", True, effective_kw, "scheduled_midnight_to_pull_out"),
        _event(base, 1, "depot_to_block_deadhead", deadhead_start, start_dt, depot_lat, depot_lon, start_lat, start_lon, depot_lsoa, start_lsoa, to_block_km, "haversine_x_1.0", False, 0.0, ""),
    ]

    passenger_events = _passenger_events(row, base, service_date, start_dt, end_dt, start_lat, start_lon, end_lat, end_lon, start_lsoa, end_lsoa, use_trip_level_events)
    out.extend(passenger_events)
    out = _insert_layovers(out, base, depot_lsoa, effective_kw)

    last_end = pd.Timestamp(out[-1]["end_datetime"])
    from_block_km = haversine_km(end_lat, end_lon, depot_lat, depot_lon)
    from_block_h = 0.0 if not np.isfinite(from_block_km) else from_block_km / deadhead_speed_kmh
    return_dt = last_end + pd.to_timedelta(from_block_h, unit="h")
    out.append(
        _event(base, len(out), "block_to_depot_deadhead", last_end, return_dt, end_lat, end_lon, depot_lat, depot_lon, end_lsoa, depot_lsoa, from_block_km, "haversine_x_1.0", False, 0.0, "")
    )
    post_end = pd.Timestamp(service_date).normalize() + pd.Timedelta(days=1, hours=6)
    overnight_method = "next_day_06_local"
    if return_dt >= post_end:
        post_end = return_dt + pd.Timedelta(hours=6)
        overnight_method = "fallback_6h_after_return"
    out.append(
        _event(base, len(out), "depot_parking_post", return_dt, post_end, depot_lat, depot_lon, depot_lat, depot_lon, depot_lsoa, depot_lsoa, 0.0, "none", True, effective_kw, overnight_method)
    )
    for seq, event in enumerate(sorted(out, key=lambda item: (pd.Timestamp(item["start_datetime"]), item["event_seq"]))):
        event["event_seq"] = seq
    return out


def _passenger_events(row: Any, base: dict[str, Any], service_date: str, start_dt: pd.Timestamp, end_dt: pd.Timestamp, start_lat: float, start_lon: float, end_lat: float, end_lon: float, start_lsoa: str, end_lsoa: str, use_trip_level_events: bool) -> list[dict[str, Any]]:
    starts = _as_list(_get(row, "trip_start_times", []))
    ends = _as_list(_get(row, "trip_end_times", []))
    distances = _as_list(_get(row, "trip_distances_km", []))
    if not use_trip_level_events or not starts or len(starts) != len(ends):
        return [
            _event(base, 0, "passenger_block", start_dt, end_dt, start_lat, start_lon, end_lat, end_lon, start_lsoa, end_lsoa, float(_get(row, "passenger_distance_km", 0.0)), "gtfs_shape_or_stop_distance", False, 0.0, "")
        ]
    trip_ids = _as_list(_get(row, "trip_ids", []))
    start_lats = _as_list(_get(row, "trip_start_lats", []))
    start_lons = _as_list(_get(row, "trip_start_lons", []))
    end_lats = _as_list(_get(row, "trip_end_lats", []))
    end_lons = _as_list(_get(row, "trip_end_lons", []))
    start_lsoas = _as_list(_get(row, "trip_start_lsoas", []))
    end_lsoas = _as_list(_get(row, "trip_end_lsoas", []))
    events: list[dict[str, Any]] = []
    for idx, start_h in enumerate(starts):
        event = _event(
            base,
            idx,
            "passenger_trip",
            datetime_from_service_hour(service_date, float(start_h)),
            datetime_from_service_hour(service_date, float(ends[idx])),
            _list_get(start_lats, idx, start_lat),
            _list_get(start_lons, idx, start_lon),
            _list_get(end_lats, idx, end_lat),
            _list_get(end_lons, idx, end_lon),
            _clean(_list_get(start_lsoas, idx, start_lsoa if idx == 0 else "")),
            _clean(_list_get(end_lsoas, idx, end_lsoa if idx == len(starts) - 1 else "")),
            float(_list_get(distances, idx, 0.0)),
            "gtfs_shape_or_stop_distance",
            False,
            0.0,
            "",
        )
        event["trip_id"] = str(_list_get(trip_ids, idx, ""))
        events.append(event)
    return events


def _insert_layovers(events: list[dict[str, Any]], base: dict[str, Any], depot_lsoa: str, effective_kw: float) -> list[dict[str, Any]]:
    if len(events) < 2:
        return events
    out: list[dict[str, Any]] = []
    for left, right in zip(events[:-1], events[1:]):
        out.append(left)
        if left["event_type"] not in {"passenger_trip", "passenger_block"} or right["event_type"] not in {"passenger_trip", "passenger_block"}:
            continue
        duration_min = (pd.Timestamp(right["start_datetime"]) - pd.Timestamp(left["end_datetime"])).total_seconds() / 60.0
        if duration_min <= 0:
            continue
        at_depot = depot_lsoa and _clean(left.get("end_lsoa", "")) == depot_lsoa and _clean(right.get("start_lsoa", "")) == depot_lsoa
        event_type = "depot_parking_midday" if at_depot and duration_min >= 30.0 else "layover"
        out.append(
            _event(
                base,
                0,
                event_type,
                pd.Timestamp(left["end_datetime"]),
                pd.Timestamp(right["start_datetime"]),
                left["end_lat"],
                left["end_lon"],
                right["start_lat"],
                right["start_lon"],
                left["end_lsoa"],
                right["start_lsoa"],
                0.0,
                "none",
                event_type == "depot_parking_midday",
                effective_kw if event_type == "depot_parking_midday" else 0.0,
                "",
            )
        )
    out.append(events[-1])
    return out


def _base(row: Any, *, depot_power_kw: float, effective_kw: float) -> dict[str, Any]:
    return {
        "simulation_case_id": str(_get(row, "simulation_case_id", "")),
        "service_date": str(_get(row, "service_date", "")),
        "vehicle_id": str(_get(row, "vehicle_id", "")),
        "vehicle_model": str(_get(row, "vehicle_model", "")),
        "vehicle_subtype": str(_get(row, "vehicle_subtype", "")),
        "block_template_id": str(_get(row, "block_template_id", "")),
        "block_id": str(_get(row, "block_id", "")),
        "agency_id": str(_get(row, "agency_id", "")),
        "depot_id": str(_get(row, "depot_id", "")),
        "operational_depot_lsoa": _clean(_get(row, "operational_depot_lsoa", "")),
        "region_key": str(_get(row, "region_key", "unknown")),
        "sample_mode": str(_get(row, "sample_mode", "")),
        "weighting_mode": "unweighted_ev_stock_scenario",
        "battery_kwh": float(_get(row, "battery_kwh", np.nan)),
        "consumption_kwh_per_km": float(_get(row, "consumption_kwh_per_km", np.nan)),
        "ac_charge_kw_max": float(_get(row, "ac_charge_kw_max", np.nan)),
        "dc_charge_kw_max": float(_get(row, "dc_charge_kw_max", np.nan)),
        "usable_soc_min": float(_get(row, "usable_soc_min", 0.10)),
        "usable_soc_max": float(_get(row, "usable_soc_max", 0.95)),
        "depot_power_kw": float(depot_power_kw),
        "depot_power_source": "fixed_default_100kw" if float(depot_power_kw) == 100.0 else "fixed_cli_depot_power_kw",
        "effective_charge_kw": float(effective_kw),
    }


def _event(base: dict[str, Any], seq: int, event_type: str, start: pd.Timestamp, end: pd.Timestamp, start_lat: Any, start_lon: Any, end_lat: Any, end_lon: Any, start_lsoa: str, end_lsoa: str, distance_km: float, distance_method: str, can_charge: bool, charge_power_kw: float, overnight_window_method: str) -> dict[str, Any]:
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    duration_min = max(0.0, (end_ts - start_ts).total_seconds() / 60.0)
    return {
        **base,
        "event_seq": int(seq),
        "event_type": event_type,
        "trip_id": "",
        "start_datetime": start_ts,
        "end_datetime": end_ts,
        "duration_min": float(duration_min),
        "start_lat": float(start_lat) if _finite(start_lat) else np.nan,
        "start_lon": float(start_lon) if _finite(start_lon) else np.nan,
        "end_lat": float(end_lat) if _finite(end_lat) else np.nan,
        "end_lon": float(end_lon) if _finite(end_lon) else np.nan,
        "start_lsoa": _clean(start_lsoa),
        "end_lsoa": _clean(end_lsoa),
        "distance_km": float(distance_km) if _finite(distance_km) else 0.0,
        "distance_method": distance_method,
        "can_charge": bool(can_charge),
        "charge_power_kw": float(charge_power_kw) if can_charge else 0.0,
        "charge_kwh_added": 0.0,
        "energy_kwh": 0.0,
        "soc_start_kwh": np.nan,
        "soc_end_kwh": np.nan,
        "charging_end_datetime": pd.NaT,
        "overnight_window_method": overnight_window_method,
    }


def _get(row: Any, name: str, default: Any = None) -> Any:
    return getattr(row, name, default)


def _clean(value: Any) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "<na>"} else text


def _finite(value: Any) -> bool:
    try:
        return bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if value is None:
        return []
    try:
        if pd.isna(value):
            return []
    except ValueError:
        return []
    return [value]


def _list_get(values: list[Any], idx: int, default: Any) -> Any:
    return values[idx] if idx < len(values) and not pd.isna(values[idx]) else default


def _event_columns() -> list[str]:
    return [
        "simulation_case_id",
        "service_date",
        "vehicle_id",
        "vehicle_model",
        "vehicle_subtype",
        "block_template_id",
        "block_id",
        "agency_id",
        "depot_id",
        "operational_depot_lsoa",
        "region_key",
        "sample_mode",
        "weighting_mode",
        "event_seq",
        "event_type",
        "trip_id",
        "start_datetime",
        "end_datetime",
        "duration_min",
        "start_lat",
        "start_lon",
        "end_lat",
        "end_lon",
        "start_lsoa",
        "end_lsoa",
        "distance_km",
        "distance_method",
        "can_charge",
        "charge_power_kw",
        "charge_kwh_added",
        "energy_kwh",
        "soc_start_kwh",
        "soc_end_kwh",
        "charging_end_datetime",
        "overnight_window_method",
        "battery_kwh",
        "consumption_kwh_per_km",
        "ac_charge_kw_max",
        "dc_charge_kw_max",
        "usable_soc_min",
        "usable_soc_max",
        "depot_power_kw",
        "depot_power_source",
        "effective_charge_kw",
    ]


__all__ = [
    "DEPOT_CHARGING_EVENT_TYPES",
    "FORBIDDEN_CHARGING_EVENT_TYPES",
    "MOVEMENT_EVENT_TYPES",
    "build_vehicle_day_events",
    "datetime_from_service_hour",
    "haversine_km",
]

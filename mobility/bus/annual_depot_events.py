"""Depot-only annual vehicle-day event ledger construction."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from .annual_block_instances import datetime_from_service_hour


DEPOT_CHARGING_EVENT_TYPES = {
    "depot_parking_pre",
    "depot_parking_midday",
    "depot_parking_post",
    "depot_parking_overnight",
    # Carry-over mode only (plan v2 §5.3/§14.4): explicit home-depot idle
    # charging for vehicles without a duty that day; never emitted under
    # daily_reset.
    "idle_home_depot_charging",
}
MOVEMENT_EVENT_TYPES = {
    "depot_to_block_deadhead",
    "passenger_block",
    "passenger_trip",
    "block_to_depot_deadhead",
    # Coach port only (default off for bus): explicit repositioning between
    # consecutive trips whose endpoints differ (first-fit chain relocation).
    "inter_trip_relocation",
}
FORBIDDEN_CHARGING_EVENT_TYPES = {"public_charger_event", "opportunity_charging", "terminal_public_charging", "OCM_station_event"}
MIDDAY_DEPOT_DISTANCE_THRESHOLD_KM = 1.0


def haversine_km(lat1: Any, lon1: Any, lat2: Any, lon2: Any) -> float:
    try:
        values = [float(lat1), float(lon1), float(lat2), float(lon2)]
    except (TypeError, ValueError):
        return float("nan")
    if not all(np.isfinite(values)):
        return float("nan")
    r_km = 6371.0088
    phi1, phi2 = math.radians(values[0]), math.radians(values[2])
    d_phi = math.radians(values[2] - values[0])
    d_lam = math.radians(values[3] - values[1])
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2) ** 2
    return float(2 * r_km * math.asin(min(1.0, math.sqrt(a))))


def build_vehicle_day_events(
    assignments: pd.DataFrame,
    block_instances: pd.DataFrame,
    block_templates: pd.DataFrame,
    ev_bus_specs: pd.DataFrame,
    depot_registry: pd.DataFrame,
    *,
    depot_power_kw: float = 100.0,
    default_overnight_end_hour: float = 6.0,
    deadhead_speed_kmh: float = 30.0,
    use_trip_level_events: bool = True,
    stitch_start_by_spec: dict[str, pd.Timestamp] | None = None,
    inter_trip_relocation: bool = False,
    relocation_speed_kmh: float = 50.0,
) -> pd.DataFrame:
    """Build the per-vehicle-day event ledger.

    ``inter_trip_relocation`` (coach port, default off so bus output stays
    byte-stable): when consecutive trips end/start more than
    ``MIDDAY_DEPOT_DISTANCE_THRESHOLD_KM`` apart, emit an explicit
    ``inter_trip_relocation`` movement event (haversine x 1.0 at
    ``relocation_speed_kmh``) instead of treating the gap as a stationary
    layover, so repositioning energy is no longer free.

    ``stitch_start_by_spec`` switches on carry-over stitching (plan v2 §3.3):
    each vehicle's day opens at its stitch point (the previous day's pending
    window seam) instead of service midnight, so the ``depot_parking_pre``
    window — emitted only when the pull-out is after the seam — never overlaps
    the previous day's overnight window. A spec missing from the map is fatal:
    carry-over state must cover every matched vehicle.
    """
    if assignments.empty:
        return pd.DataFrame(columns=_event_columns())
    merged = assignments.merge(block_instances, on=["service_date", "block_instance_id", "block_template_id", "agency_id", "service_id", "block_id"], how="left", suffixes=("", "_block"))
    merged = _apply_home_depot_override(merged, depot_registry)
    template_cols = [
        "block_template_id",
        "trip_ids",
        "trip_start_times",
        "trip_end_times",
        "trip_start_lats",
        "trip_start_lons",
        "trip_end_lats",
        "trip_end_lons",
        "trip_distances_km",
        "trip_start_stops",
        "trip_end_stops",
        "trip_start_lsoas",
        "trip_end_lsoas",
    ]
    present_template_cols = [col for col in template_cols if col in block_templates.columns]
    merged = merged.merge(block_templates.loc[:, present_template_cols], on="block_template_id", how="left", suffixes=("", "_template"))
    merged = merged.merge(ev_bus_specs, on="vehicle_spec_id", how="left")
    registry_cols = ["depot_id", "depot_lat", "depot_lon", "depot_lsoa", "depot_confidence"]
    merged = merged.merge(depot_registry.loc[:, [col for col in registry_cols if col in depot_registry.columns]], on="depot_id", how="left", suffixes=("", "_registry"))

    records: list[dict[str, Any]] = []
    for row in merged.itertuples(index=False):
        records.extend(
            _events_for_vehicle_day(
                row,
                depot_power_kw=depot_power_kw,
                default_overnight_end_hour=default_overnight_end_hour,
                deadhead_speed_kmh=deadhead_speed_kmh,
                use_trip_level_events=use_trip_level_events,
                stitch_start_by_spec=stitch_start_by_spec,
                inter_trip_relocation=inter_trip_relocation,
                relocation_speed_kmh=relocation_speed_kmh,
            )
        )
    events = pd.DataFrame.from_records(records, columns=_event_columns())
    if not events.empty:
        events = events.sort_values(["vehicle_day_id", "start_datetime", "event_seq"], kind="stable").reset_index(drop=True)
        events["event_seq"] = events.groupby("vehicle_day_id", sort=False).cumcount()
    return events


def _apply_home_depot_override(merged: pd.DataFrame, depot_registry: pd.DataFrame) -> pd.DataFrame:
    """Home-depot mode (plan v2 §15): the vehicle deadheads from and charges at its HOME depot.

    Constrained assignments carry a non-empty ``home_depot_id``. The feasibility
    screen budgets deadhead as home depot <-> block start/end, so the event walk
    must resolve depot coordinates against the same depot; using the block-attached
    depot diverges from the screen (PR 1.5 full-year audit: 929 infeasible
    vehicle-days from block-depot deadheads) and attributes charging load to the
    wrong depot. Rows with an empty ``home_depot_id`` (PR 1 semantics) keep the
    block-attached depot; the block depot is retained as ``block_depot_id``.
    """
    if "home_depot_id" not in merged.columns:
        return merged
    home_ids = merged["home_depot_id"].fillna("").astype(str)
    use_home = home_ids.ne("")
    if not use_home.any():
        return merged
    merged = merged.copy()
    merged["block_depot_id"] = merged["depot_id"]
    merged.loc[use_home, "depot_id"] = home_ids[use_home]
    if "depot_lsoa" in merged.columns and "depot_lsoa" in depot_registry.columns:
        lsoa_by_depot = depot_registry.drop_duplicates("depot_id", keep="first").set_index("depot_id")["depot_lsoa"]
        merged.loc[use_home, "depot_lsoa"] = home_ids[use_home].map(lsoa_by_depot).fillna("")
    return merged


def _events_for_vehicle_day(row: Any, *, depot_power_kw: float, default_overnight_end_hour: float, deadhead_speed_kmh: float, use_trip_level_events: bool, stitch_start_by_spec: dict[str, pd.Timestamp] | None = None, inter_trip_relocation: bool = False, relocation_speed_kmh: float = 50.0) -> list[dict[str, Any]]:
    service_date = str(getattr(row, "service_date"))
    base = {
        "service_date": service_date,
        "vehicle_day_id": str(getattr(row, "vehicle_day_id")),
        "vehicle_spec_id": str(getattr(row, "vehicle_spec_id")),
        "block_instance_id": str(getattr(row, "block_instance_id")),
        "block_template_id": str(getattr(row, "block_template_id")),
        "agency_id": str(getattr(row, "agency_id")),
        "block_id": str(getattr(row, "block_id")),
        "depot_id": str(getattr(row, "depot_id", "")),
        "depot_lsoa": _get(row, "depot_lsoa", _get(row, "depot_lsoa_registry", "")),
        "scenario_mode": str(getattr(row, "scenario_mode", "ev_stock_scale")),
        "battery_kwh": float(_get(row, "battery_kwh", np.nan)),
        "consumption_kwh_per_km": float(_get(row, "consumption_kwh_per_km", np.nan)),
        "ac_charge_kw_max": float(_get(row, "ac_charge_kw_max", np.nan)),
        "usable_soc_min": float(_get(row, "usable_soc_min", 0.10)),
        "usable_soc_max": float(_get(row, "usable_soc_max", 0.95)),
    }
    depot_lat = float(_get(row, "depot_lat", np.nan))
    depot_lon = float(_get(row, "depot_lon", np.nan))
    depot_lsoa = str(base["depot_lsoa"])
    start_dt = pd.Timestamp(getattr(row, "start_datetime"))
    end_dt = pd.Timestamp(getattr(row, "end_datetime"))
    block_start_lat = float(_get(row, "start_lat", np.nan))
    block_start_lon = float(_get(row, "start_lon", np.nan))
    block_end_lat = float(_get(row, "end_lat", np.nan))
    block_end_lon = float(_get(row, "end_lon", np.nan))
    block_start_lsoa = str(_get(row, "start_lsoa", ""))
    block_end_lsoa = str(_get(row, "end_lsoa", ""))

    out: list[dict[str, Any]] = []
    seq = 0
    to_block_km = haversine_km(depot_lat, depot_lon, block_start_lat, block_start_lon)
    to_block_h = 0.0 if not np.isfinite(to_block_km) else to_block_km / float(deadhead_speed_kmh)
    deadhead_start = start_dt - pd.to_timedelta(to_block_h, unit="h")
    service_midnight = pd.Timestamp(service_date)
    effective_kw = _effective_charge_kw(base["ac_charge_kw_max"], depot_power_kw)
    if stitch_start_by_spec is None:
        window_open = service_midnight
    else:
        # Carry-over stitching (§3.3): the day opens at the previous pending
        # window's seam. Missing state for a matched spec is fatal (§8.5).
        spec_id = str(base["vehicle_spec_id"])
        if spec_id not in stitch_start_by_spec:
            raise KeyError(f"carryover: missing stitch start for vehicle_spec_id={spec_id}")
        window_open = pd.Timestamp(stitch_start_by_spec[spec_id])
    if deadhead_start > window_open:
        out.append(_event(base, seq, "depot_parking_pre", window_open, deadhead_start, depot_lat, depot_lon, depot_lat, depot_lon, depot_lsoa, depot_lsoa, 0.0, "none", True, effective_kw))
        seq += 1
    out.append(_event(base, seq, "depot_to_block_deadhead", deadhead_start, start_dt, depot_lat, depot_lon, block_start_lat, block_start_lon, depot_lsoa, block_start_lsoa, to_block_km, "haversine_x_1.0", False, 0.0))
    seq += 1

    passenger_events = _passenger_events(row, service_date, base, start_dt, end_dt, block_start_lat, block_start_lon, block_end_lat, block_end_lon, block_start_lsoa, block_end_lsoa, use_trip_level_events)
    for event in passenger_events:
        event["event_seq"] = seq
        out.append(event)
        seq += 1
    out = _insert_midday_layovers(out, base, depot_lsoa, depot_lat, depot_lon, effective_kw, inter_trip_relocation=inter_trip_relocation, relocation_speed_kmh=relocation_speed_kmh)
    seq = len(out)

    last_end = pd.Timestamp(out[-1]["end_datetime"]) if out else end_dt
    from_block_km = haversine_km(block_end_lat, block_end_lon, depot_lat, depot_lon)
    from_block_h = 0.0 if not np.isfinite(from_block_km) else from_block_km / float(deadhead_speed_kmh)
    return_dt = last_end + pd.to_timedelta(from_block_h, unit="h")
    out.append(_event(base, seq, "block_to_depot_deadhead", last_end, return_dt, block_end_lat, block_end_lon, depot_lat, depot_lon, block_end_lsoa, depot_lsoa, from_block_km, "haversine_x_1.0", False, 0.0))
    seq += 1
    overnight_end = pd.Timestamp(service_date) + pd.Timedelta(days=1) + pd.to_timedelta(float(default_overnight_end_hour), unit="h")
    if return_dt >= overnight_end:
        # Late cross-midnight return: the scheduled overnight window has already
        # passed, so fall back to a post-return window of the same duration to
        # keep the recharge load observable instead of dropping it entirely.
        overnight_end = return_dt + pd.to_timedelta(float(default_overnight_end_hour), unit="h")
    event_type = "depot_parking_overnight" if overnight_end.date() != return_dt.date() else "depot_parking_post"
    out.append(_event(base, seq, event_type, return_dt, overnight_end, depot_lat, depot_lon, depot_lat, depot_lon, depot_lsoa, depot_lsoa, 0.0, "none", True, effective_kw))
    for index, event in enumerate(sorted(out, key=lambda item: (pd.Timestamp(item["start_datetime"]), item["event_seq"]))):
        event["event_seq"] = index
    return out


def _passenger_events(row: Any, service_date: str, base: dict[str, Any], start_dt: pd.Timestamp, end_dt: pd.Timestamp, start_lat: float, start_lon: float, end_lat: float, end_lon: float, start_lsoa: str, end_lsoa: str, use_trip_level_events: bool) -> list[dict[str, Any]]:
    trip_starts = _as_list(_get(row, "trip_start_times", []))
    trip_ends = _as_list(_get(row, "trip_end_times", []))
    trip_distances = _as_list(_get(row, "trip_distances_km", []))
    trip_ids = _as_list(_get(row, "trip_ids", []))
    if not use_trip_level_events or not trip_starts or len(trip_starts) != len(trip_ends):
        event = _event(
            base,
            0,
            "passenger_block",
            start_dt,
            end_dt,
            start_lat,
            start_lon,
            end_lat,
            end_lon,
            start_lsoa,
            end_lsoa,
            float(_get(row, "passenger_distance_km", np.nansum(trip_distances) if trip_distances else 0.0)),
            "gtfs_shape_or_stop_distance",
            False,
            0.0,
        )
        event["trip_id"] = str(_get(row, "block_id", ""))
        return [event]
    start_lats = _as_list(_get(row, "trip_start_lats", []))
    start_lons = _as_list(_get(row, "trip_start_lons", []))
    end_lats = _as_list(_get(row, "trip_end_lats", []))
    end_lons = _as_list(_get(row, "trip_end_lons", []))
    start_lsoas = _as_list(_get(row, "trip_start_lsoas", []))
    end_lsoas = _as_list(_get(row, "trip_end_lsoas", []))
    events: list[dict[str, Any]] = []
    for idx, start_h in enumerate(trip_starts):
        event = _event(
            base,
            idx,
            "passenger_trip",
            datetime_from_service_hour(service_date, float(start_h)),
            datetime_from_service_hour(service_date, float(trip_ends[idx])),
            _list_get(start_lats, idx, start_lat),
            _list_get(start_lons, idx, start_lon),
            _list_get(end_lats, idx, end_lat),
            _list_get(end_lons, idx, end_lon),
            str(_list_get(start_lsoas, idx, start_lsoa if idx == 0 else "")),
            str(_list_get(end_lsoas, idx, end_lsoa if idx == len(trip_starts) - 1 else "")),
            float(_list_get(trip_distances, idx, 0.0)),
            "gtfs_shape_or_stop_distance",
            False,
            0.0,
        )
        event["trip_id"] = str(_list_get(trip_ids, idx, ""))
        events.append(event)
    return events


def _insert_midday_layovers(
    events: list[dict[str, Any]],
    base: dict[str, Any],
    depot_lsoa: str,
    depot_lat: float,
    depot_lon: float,
    charge_power_kw: float,
    *,
    inter_trip_relocation: bool = False,
    relocation_speed_kmh: float = 50.0,
) -> list[dict[str, Any]]:
    if len(events) < 2:
        return events
    out: list[dict[str, Any]] = []
    for left, right in zip(events[:-1], events[1:]):
        out.append(left)
        if left["event_type"] not in {"passenger_trip", "passenger_block"} or right["event_type"] not in {"passenger_trip", "passenger_block"}:
            continue
        gap_min = (pd.Timestamp(right["start_datetime"]) - pd.Timestamp(left["end_datetime"])).total_seconds() / 60.0
        if gap_min <= 0:
            continue
        layover_start = pd.Timestamp(left["end_datetime"])
        layover_left = left
        if inter_trip_relocation:
            reloc_km = haversine_km(left["end_lat"], left["end_lon"], right["start_lat"], right["start_lon"])
            if np.isfinite(reloc_km) and reloc_km > MIDDAY_DEPOT_DISTANCE_THRESHOLD_KM:
                # Explicit repositioning: distance always counted in full;
                # duration capped at the available gap (the chain builder's
                # transit buffer can be tighter than distance/speed).
                reloc_minutes = min(gap_min, reloc_km / float(relocation_speed_kmh) * 60.0)
                reloc_end = layover_start + pd.to_timedelta(reloc_minutes, unit="m")
                out.append(
                    _event(
                        base,
                        0,
                        "inter_trip_relocation",
                        layover_start,
                        reloc_end,
                        left["end_lat"],
                        left["end_lon"],
                        right["start_lat"],
                        right["start_lon"],
                        left["end_lsoa"],
                        right["start_lsoa"],
                        float(reloc_km),
                        "haversine_x_1.0",
                        False,
                        0.0,
                    )
                )
                remaining_min = gap_min - reloc_minutes
                if remaining_min <= 0:
                    continue
                # The vehicle now waits at the NEXT trip's start location.
                layover_start = reloc_end
                layover_left = {**left, "end_lat": right["start_lat"], "end_lon": right["start_lon"], "end_lsoa": right["start_lsoa"]}
                gap_min = remaining_min
        is_depot = _is_depot_layover(layover_left, right, depot_lsoa, depot_lat, depot_lon)
        event_type = "depot_parking_midday" if is_depot and gap_min >= 30.0 else "terminal_layover"
        out.append(
            _event(
                base,
                0,
                event_type,
                layover_start,
                pd.Timestamp(right["start_datetime"]),
                layover_left["end_lat"],
                layover_left["end_lon"],
                right["start_lat"],
                right["start_lon"],
                layover_left["end_lsoa"],
                right["start_lsoa"],
                0.0,
                "none",
                event_type == "depot_parking_midday",
                charge_power_kw if event_type == "depot_parking_midday" else 0.0,
            )
        )
    out.append(events[-1])
    return out


def _is_depot_layover(
    left: dict[str, Any],
    right: dict[str, Any],
    depot_lsoa: str,
    depot_lat: float,
    depot_lon: float,
) -> bool:
    left_lsoa = str(left.get("end_lsoa", "")).strip()
    right_lsoa = str(right.get("start_lsoa", "")).strip()
    if depot_lsoa and left_lsoa == depot_lsoa and right_lsoa == depot_lsoa:
        return True
    left_distance = haversine_km(left.get("end_lat"), left.get("end_lon"), depot_lat, depot_lon)
    right_distance = haversine_km(right.get("start_lat"), right.get("start_lon"), depot_lat, depot_lon)
    return bool(
        np.isfinite(left_distance)
        and np.isfinite(right_distance)
        and left_distance <= MIDDAY_DEPOT_DISTANCE_THRESHOLD_KM
        and right_distance <= MIDDAY_DEPOT_DISTANCE_THRESHOLD_KM
    )


def _event(base: dict[str, Any], seq: int, event_type: str, start: pd.Timestamp, end: pd.Timestamp, start_lat: Any, start_lon: Any, end_lat: Any, end_lon: Any, start_lsoa: str, end_lsoa: str, distance_km: float, distance_method: str, can_charge: bool, charge_power_kw: float) -> dict[str, Any]:
    duration_min = max(0.0, (pd.Timestamp(end) - pd.Timestamp(start)).total_seconds() / 60.0)
    record = {
        **base,
        "event_seq": int(seq),
        "event_type": event_type,
        "trip_id": "",
        "start_datetime": pd.Timestamp(start),
        "end_datetime": pd.Timestamp(end),
        "duration_min": float(duration_min),
        "start_lat": float(start_lat) if _is_finite(start_lat) else np.nan,
        "start_lon": float(start_lon) if _is_finite(start_lon) else np.nan,
        "end_lat": float(end_lat) if _is_finite(end_lat) else np.nan,
        "end_lon": float(end_lon) if _is_finite(end_lon) else np.nan,
        "start_lsoa": "" if pd.isna(start_lsoa) else str(start_lsoa),
        "end_lsoa": "" if pd.isna(end_lsoa) else str(end_lsoa),
        "distance_km": float(distance_km) if _is_finite(distance_km) else 0.0,
        "distance_method": distance_method,
        "energy_kwh": 0.0,
        "can_charge": bool(can_charge),
        "charge_power_kw": float(charge_power_kw) if bool(can_charge) else 0.0,
        "charge_kwh_added": 0.0,
        "soc_start_kwh": np.nan,
        "soc_end_kwh": np.nan,
        "charging_end_datetime": pd.NaT,
    }
    return record


def _effective_charge_kw(ac_charge_kw: float, depot_power_kw: float) -> float:
    if not np.isfinite(float(ac_charge_kw)) or not np.isfinite(float(depot_power_kw)):
        return 0.0
    return max(0.0, min(float(ac_charge_kw), float(depot_power_kw)))


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if pd.isna(value):
        return []
    return [value]


def _list_get(values: list[Any], idx: int, default: Any) -> Any:
    return values[idx] if idx < len(values) and not pd.isna(values[idx]) else default


def _get(row: Any, name: str, default: Any = None) -> Any:
    return getattr(row, name, default)


def _is_finite(value: Any) -> bool:
    try:
        return bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False


def _event_columns() -> list[str]:
    return [
        "service_date",
        "vehicle_day_id",
        "vehicle_spec_id",
        "block_instance_id",
        "block_template_id",
        "agency_id",
        "block_id",
        "depot_id",
        "depot_lsoa",
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
        "energy_kwh",
        "can_charge",
        "charge_power_kw",
        "charge_kwh_added",
        "soc_start_kwh",
        "soc_end_kwh",
        "charging_end_datetime",
        "scenario_mode",
        "battery_kwh",
        "consumption_kwh_per_km",
        "ac_charge_kw_max",
        "usable_soc_min",
        "usable_soc_max",
    ]


__all__ = [
    "DEPOT_CHARGING_EVENT_TYPES",
    "FORBIDDEN_CHARGING_EVENT_TYPES",
    "MIDDAY_DEPOT_DISTANCE_THRESHOLD_KM",
    "MOVEMENT_EVENT_TYPES",
    "build_vehicle_day_events",
    "haversine_km",
]

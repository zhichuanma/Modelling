"""Private-car-aligned observability artifacts for annual bus depot runs."""

from __future__ import annotations

import datetime as dt
from typing import Any

import numpy as np
import pandas as pd

from .annual_depot_events import DEPOT_CHARGING_EVENT_TYPES, MOVEMENT_EVENT_TYPES
from .annual_depot_load import SLOT_MINUTES


BUS_TRIP_RECORD_COLUMNS = [
    "ev_id",
    "person_id",
    "bus_id",
    "vehicle_day_id",
    "vehicle_spec_id",
    "block_instance_id",
    "block_template_id",
    "agency_id",
    "block_id",
    "trip_id",
    "trip_sequence_id",
    "simulation_week",
    "date",
    "service_date",
    "day_of_week",
    "origin_lsoa",
    "destination_lsoa",
    "origin_lat",
    "origin_lon",
    "destination_lat",
    "destination_lon",
    "purpose_original",
    "purpose_final",
    "departure_time",
    "arrival_time",
    "departure_datetime",
    "arrival_datetime",
    "distance_km",
    "energy_consumed_kwh",
    "soc_before_trip",
    "soc_after_trip",
    "soc_before_trip_kwh",
    "soc_after_trip_kwh",
    "holiday_week",
    "is_holiday_modified",
    "holiday_rule_applied",
    "scenario_mode",
]

BUS_CHARGING_EVENT_COLUMNS = [
    "ev_id",
    "person_id",
    "bus_id",
    "vehicle_day_id",
    "vehicle_spec_id",
    "event_id",
    "event_seq",
    "block_instance_id",
    "block_template_id",
    "agency_id",
    "block_id",
    "simulation_week",
    "date",
    "service_date",
    "charging_start_time",
    "charging_end_time",
    "charging_lsoa",
    "home_lsoa",
    "depot_lsoa",
    "charging_type",
    "depot_event_type",
    "can_charge",
    "station_id",
    "depot_id",
    "charging_power_kw",
    "charged_energy_kwh",
    "soc_before_charging",
    "soc_after_charging",
    "soc_before_charging_kwh",
    "soc_after_charging_kwh",
    "reason",
    "holiday_week",
    "scenario_mode",
]

BUS_EV_STATE_RECORD_COLUMNS = [
    "ev_id",
    "person_id",
    "bus_id",
    "vehicle_day_id",
    "vehicle_spec_id",
    "block_instance_id",
    "block_template_id",
    "agency_id",
    "block_id",
    "event_seq",
    "event_type",
    "trip_id",
    "simulation_week",
    "date",
    "service_date",
    "slot_date",
    "slot_index",
    "time_bin_start",
    "time_bin_end",
    "slot_start_datetime",
    "slot_end_datetime",
    "current_lsoa",
    "location_lsoa",
    "origin_lsoa",
    "destination_lsoa",
    "current_lat",
    "current_lon",
    "origin_lat",
    "origin_lon",
    "destination_lat",
    "destination_lon",
    "location_status",
    "location_confidence",
    "is_moving",
    "is_charging",
    "can_charge",
    "charging_type",
    "depot_id",
    "depot_lsoa",
    "charge_kwh",
    "energy_consumed_kwh",
    "soc_start",
    "soc_end",
    "soc_start_kwh",
    "soc_end_kwh",
    "scenario_mode",
]


def build_bus_trip_records(
    events: pd.DataFrame,
    *,
    feed_year_start: str | dt.date | pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Build private-car-style bus trip records from passenger events."""
    if events.empty:
        return pd.DataFrame(columns=BUS_TRIP_RECORD_COLUMNS)
    passenger = events[events["event_type"].isin({"passenger_trip", "passenger_block"})].copy()
    if passenger.empty:
        return pd.DataFrame(columns=BUS_TRIP_RECORD_COLUMNS)
    start = _feed_start(passenger, feed_year_start)
    rows: list[dict[str, Any]] = []
    for _, row in passenger.sort_values(["vehicle_day_id", "event_seq"], kind="stable").iterrows():
        service_date = _date(row["service_date"])
        rows.append(
            {
                "ev_id": str(row["vehicle_spec_id"]),
                "person_id": "",
                "bus_id": str(row["vehicle_spec_id"]),
                "vehicle_day_id": str(row["vehicle_day_id"]),
                "vehicle_spec_id": str(row["vehicle_spec_id"]),
                "block_instance_id": str(row["block_instance_id"]),
                "block_template_id": str(row["block_template_id"]),
                "agency_id": str(row["agency_id"]),
                "block_id": str(row["block_id"]),
                "trip_id": _event_trip_id(row),
                "trip_sequence_id": int(row["event_seq"]),
                "simulation_week": _simulation_week(service_date, start),
                "date": service_date.isoformat(),
                "service_date": service_date.isoformat(),
                "day_of_week": int(service_date.isoweekday()),
                "origin_lsoa": _string(row.get("start_lsoa", "")),
                "destination_lsoa": _string(row.get("end_lsoa", "")),
                "origin_lat": _float_or_nan(row.get("start_lat")),
                "origin_lon": _float_or_nan(row.get("start_lon")),
                "destination_lat": _float_or_nan(row.get("end_lat")),
                "destination_lon": _float_or_nan(row.get("end_lon")),
                "purpose_original": _string(row["event_type"]),
                "purpose_final": _string(row["event_type"]),
                "departure_time": _decimal_hour(row["start_datetime"], service_date),
                "arrival_time": _decimal_hour(row["end_datetime"], service_date),
                "departure_datetime": pd.Timestamp(row["start_datetime"]),
                "arrival_datetime": pd.Timestamp(row["end_datetime"]),
                "distance_km": _float_or_zero(row.get("distance_km")),
                "energy_consumed_kwh": _float_or_zero(row.get("energy_kwh")),
                "soc_before_trip": _soc_fraction(row.get("soc_start_kwh"), row.get("battery_kwh")),
                "soc_after_trip": _soc_fraction(row.get("soc_end_kwh"), row.get("battery_kwh")),
                "soc_before_trip_kwh": _float_or_nan(row.get("soc_start_kwh")),
                "soc_after_trip_kwh": _float_or_nan(row.get("soc_end_kwh")),
                "holiday_week": False,
                "is_holiday_modified": False,
                "holiday_rule_applied": False,
                "scenario_mode": _string(row.get("scenario_mode", "ev_stock_scale")),
            }
        )
    return pd.DataFrame(rows, columns=BUS_TRIP_RECORD_COLUMNS)


def build_bus_charging_event_records(
    events: pd.DataFrame,
    *,
    feed_year_start: str | dt.date | pd.Timestamp | None = None,
    energy_epsilon: float = 1e-10,
) -> pd.DataFrame:
    """Build unified bus charging-event records aligned to private cars."""
    if events.empty:
        return pd.DataFrame(columns=BUS_CHARGING_EVENT_COLUMNS)
    charging = events[
        events["event_type"].isin(DEPOT_CHARGING_EVENT_TYPES)
        & pd.to_numeric(events["charge_kwh_added"], errors="coerce").fillna(0.0).gt(float(energy_epsilon))
    ].copy()
    if charging.empty:
        return pd.DataFrame(columns=BUS_CHARGING_EVENT_COLUMNS)
    start = _feed_start(charging, feed_year_start)
    rows: list[dict[str, Any]] = []
    for _, row in charging.sort_values(["vehicle_day_id", "event_seq"], kind="stable").iterrows():
        service_date = _date(row["service_date"])
        charging_end = _charging_end(row)
        rows.append(
            {
                "ev_id": str(row["vehicle_spec_id"]),
                "person_id": "",
                "bus_id": str(row["vehicle_spec_id"]),
                "vehicle_day_id": str(row["vehicle_day_id"]),
                "vehicle_spec_id": str(row["vehicle_spec_id"]),
                "event_id": f"{row['vehicle_day_id']}_e{int(row['event_seq']):03d}_depot",
                "event_seq": int(row["event_seq"]),
                "block_instance_id": str(row["block_instance_id"]),
                "block_template_id": str(row["block_template_id"]),
                "agency_id": str(row["agency_id"]),
                "block_id": str(row["block_id"]),
                "simulation_week": _simulation_week(service_date, start),
                "date": service_date.isoformat(),
                "service_date": service_date.isoformat(),
                "charging_start_time": pd.Timestamp(row["start_datetime"]),
                "charging_end_time": charging_end,
                "charging_lsoa": _string(row.get("depot_lsoa") or row.get("start_lsoa") or row.get("end_lsoa")),
                "home_lsoa": _string(row.get("depot_lsoa", "")),
                "depot_lsoa": _string(row.get("depot_lsoa", "")),
                "charging_type": "depot",
                "depot_event_type": _string(row["event_type"]),
                "can_charge": bool(row.get("can_charge", False)),
                "station_id": _string(row.get("depot_id", "")),
                "depot_id": _string(row.get("depot_id", "")),
                "charging_power_kw": _float_or_zero(row.get("charge_power_kw")),
                "charged_energy_kwh": _float_or_zero(row.get("charge_kwh_added")),
                "soc_before_charging": _soc_fraction(row.get("soc_start_kwh"), row.get("battery_kwh")),
                "soc_after_charging": _soc_fraction(row.get("soc_end_kwh"), row.get("battery_kwh")),
                "soc_before_charging_kwh": _float_or_nan(row.get("soc_start_kwh")),
                "soc_after_charging_kwh": _float_or_nan(row.get("soc_end_kwh")),
                "reason": "",
                "holiday_week": False,
                "scenario_mode": _string(row.get("scenario_mode", "ev_stock_scale")),
            }
        )
    return pd.DataFrame(rows, columns=BUS_CHARGING_EVENT_COLUMNS)


def build_bus_ev_state_records(
    events: pd.DataFrame,
    *,
    feed_year_start: str | dt.date | pd.Timestamp | None = None,
    slot_minutes: int = SLOT_MINUTES,
) -> pd.DataFrame:
    """Build 15-minute bus location/SOC state records.

    For moving events the true route geometry is not reconstructed. The row
    carries origin/destination LSOAs and an interpolated midpoint coordinate;
    ``current_lsoa`` is only populated when both endpoints share the same LSOA.
    """
    if events.empty:
        return pd.DataFrame(columns=BUS_EV_STATE_RECORD_COLUMNS)
    start = _feed_start(events, feed_year_start)
    rows: list[dict[str, Any]] = []
    for _, event in events.sort_values(["vehicle_day_id", "event_seq"], kind="stable").iterrows():
        rows.extend(_state_rows_for_event(event, feed_start=start, slot_minutes=slot_minutes))
    return pd.DataFrame(rows, columns=BUS_EV_STATE_RECORD_COLUMNS)


def _state_rows_for_event(event: pd.Series, *, feed_start: dt.date, slot_minutes: int) -> list[dict[str, Any]]:
    start = pd.Timestamp(event["start_datetime"])
    end = pd.Timestamp(event["end_datetime"])
    if end <= start:
        return []
    cursor = start.floor(f"{int(slot_minutes)}min")
    rows: list[dict[str, Any]] = []
    while cursor < end:
        slot_end = cursor + pd.Timedelta(minutes=int(slot_minutes))
        overlap_start = max(start, cursor)
        overlap_end = min(end, slot_end)
        if overlap_end > overlap_start:
            rows.append(_state_row(event, overlap_start, overlap_end, cursor, slot_end, feed_start))
        cursor = slot_end
    return rows


def _state_row(
    event: pd.Series,
    overlap_start: pd.Timestamp,
    overlap_end: pd.Timestamp,
    slot_start: pd.Timestamp,
    slot_end: pd.Timestamp,
    feed_start: dt.date,
) -> dict[str, Any]:
    service_date = _date(event["service_date"])
    event_start = pd.Timestamp(event["start_datetime"])
    event_end = pd.Timestamp(event["end_datetime"])
    event_type = _string(event["event_type"])
    is_moving = event_type in MOVEMENT_EVENT_TYPES
    current_lsoa, location_lsoa, location_confidence = _current_lsoa(event, is_moving=is_moving)
    midpoint = overlap_start + (overlap_end - overlap_start) / 2
    lat, lon = _interpolated_position(event, midpoint)
    charge_kwh = _slot_charge_kwh(event, overlap_start, overlap_end)
    energy_kwh = _slot_energy_kwh(event, overlap_start, overlap_end)
    soc_start_kwh = _soc_at(event, overlap_start)
    soc_end_kwh = _soc_at(event, overlap_end)
    return {
        "ev_id": str(event["vehicle_spec_id"]),
        "person_id": "",
        "bus_id": str(event["vehicle_spec_id"]),
        "vehicle_day_id": str(event["vehicle_day_id"]),
        "vehicle_spec_id": str(event["vehicle_spec_id"]),
        "block_instance_id": str(event["block_instance_id"]),
        "block_template_id": str(event["block_template_id"]),
        "agency_id": str(event["agency_id"]),
        "block_id": str(event["block_id"]),
        "event_seq": int(event["event_seq"]),
        "event_type": event_type,
        "trip_id": _event_trip_id(event),
        "simulation_week": _simulation_week(service_date, feed_start),
        "date": service_date.isoformat(),
        "service_date": service_date.isoformat(),
        "slot_date": slot_start.date().isoformat(),
        "slot_index": int((slot_start.hour * 60 + slot_start.minute) / SLOT_MINUTES),
        "time_bin_start": slot_start,
        "time_bin_end": slot_end,
        "slot_start_datetime": slot_start,
        "slot_end_datetime": slot_end,
        "current_lsoa": current_lsoa,
        "location_lsoa": location_lsoa,
        "origin_lsoa": _string(event.get("start_lsoa", "")),
        "destination_lsoa": _string(event.get("end_lsoa", "")),
        "current_lat": lat,
        "current_lon": lon,
        "origin_lat": _float_or_nan(event.get("start_lat")),
        "origin_lon": _float_or_nan(event.get("start_lon")),
        "destination_lat": _float_or_nan(event.get("end_lat")),
        "destination_lon": _float_or_nan(event.get("end_lon")),
        "location_status": _location_status(event_type),
        "location_confidence": location_confidence,
        "is_moving": bool(is_moving),
        "is_charging": bool(charge_kwh > 0.0),
        "can_charge": bool(event.get("can_charge", False)),
        "charging_type": "depot" if event_type in DEPOT_CHARGING_EVENT_TYPES else "",
        "depot_id": _string(event.get("depot_id", "")),
        "depot_lsoa": _string(event.get("depot_lsoa", "")),
        "charge_kwh": charge_kwh,
        "energy_consumed_kwh": energy_kwh,
        "soc_start": _soc_fraction(soc_start_kwh, event.get("battery_kwh")),
        "soc_end": _soc_fraction(soc_end_kwh, event.get("battery_kwh")),
        "soc_start_kwh": soc_start_kwh,
        "soc_end_kwh": soc_end_kwh,
        "scenario_mode": _string(event.get("scenario_mode", "ev_stock_scale")),
    }


def _current_lsoa(event: pd.Series, *, is_moving: bool) -> tuple[str, str, str]:
    start_lsoa = _string(event.get("start_lsoa", ""))
    end_lsoa = _string(event.get("end_lsoa", ""))
    if not is_moving:
        location = start_lsoa or end_lsoa or _string(event.get("depot_lsoa", ""))
        return location, location, "event_location_lsoa"
    if start_lsoa and start_lsoa == end_lsoa:
        return start_lsoa, start_lsoa, "same_endpoint_lsoa"
    return "", "", "movement_endpoint_interval"


def _location_status(event_type: str) -> str:
    if event_type in DEPOT_CHARGING_EVENT_TYPES:
        return "parked_depot"
    if event_type == "terminal_layover":
        return "parked_terminal"
    if event_type in {"passenger_trip", "passenger_block"}:
        return "in_service"
    if event_type in {"depot_to_block_deadhead", "block_to_depot_deadhead"}:
        return "deadhead"
    return "other"


def _interpolated_position(event: pd.Series, timestamp: pd.Timestamp) -> tuple[float, float]:
    start_lat = _float_or_nan(event.get("start_lat"))
    start_lon = _float_or_nan(event.get("start_lon"))
    end_lat = _float_or_nan(event.get("end_lat"))
    end_lon = _float_or_nan(event.get("end_lon"))
    if not all(np.isfinite(value) for value in (start_lat, start_lon, end_lat, end_lon)):
        return np.nan, np.nan
    start = pd.Timestamp(event["start_datetime"])
    end = pd.Timestamp(event["end_datetime"])
    duration = (end - start).total_seconds()
    if duration <= 0:
        return start_lat, start_lon
    ratio = min(1.0, max(0.0, (timestamp - start).total_seconds() / duration))
    return start_lat + (end_lat - start_lat) * ratio, start_lon + (end_lon - start_lon) * ratio


def _slot_energy_kwh(event: pd.Series, overlap_start: pd.Timestamp, overlap_end: pd.Timestamp) -> float:
    event_type = _string(event.get("event_type", ""))
    if event_type not in MOVEMENT_EVENT_TYPES:
        return 0.0
    return _proportional_value(event.get("energy_kwh"), event["start_datetime"], event["end_datetime"], overlap_start, overlap_end)


def _slot_charge_kwh(event: pd.Series, overlap_start: pd.Timestamp, overlap_end: pd.Timestamp) -> float:
    event_type = _string(event.get("event_type", ""))
    if event_type not in DEPOT_CHARGING_EVENT_TYPES:
        return 0.0
    charge = _float_or_zero(event.get("charge_kwh_added"))
    if charge <= 0.0:
        return 0.0
    start = pd.Timestamp(event["start_datetime"])
    charge_end = _charging_end(event)
    overlap = max(0.0, (min(overlap_end, charge_end) - max(overlap_start, start)).total_seconds())
    duration = max(0.0, (charge_end - start).total_seconds())
    if duration <= 0.0:
        return 0.0
    return charge * overlap / duration


def _proportional_value(value: Any, event_start: Any, event_end: Any, overlap_start: pd.Timestamp, overlap_end: pd.Timestamp) -> float:
    amount = _float_or_zero(value)
    if amount == 0.0:
        return 0.0
    start = pd.Timestamp(event_start)
    end = pd.Timestamp(event_end)
    duration = (end - start).total_seconds()
    if duration <= 0.0:
        return 0.0
    overlap = max(0.0, (overlap_end - overlap_start).total_seconds())
    return amount * overlap / duration


def _soc_at(event: pd.Series, timestamp: pd.Timestamp) -> float:
    start_soc = _float_or_nan(event.get("soc_start_kwh"))
    end_soc = _float_or_nan(event.get("soc_end_kwh"))
    if not np.isfinite(start_soc) or not np.isfinite(end_soc):
        return np.nan
    event_start = pd.Timestamp(event["start_datetime"])
    event_end = pd.Timestamp(event["end_datetime"])
    interpolation_end = event_end
    if _string(event.get("event_type", "")) in DEPOT_CHARGING_EVENT_TYPES:
        charging_end = _charging_end(event)
        if charging_end > event_start:
            interpolation_end = charging_end
        if timestamp >= interpolation_end:
            return end_soc
    duration = (interpolation_end - event_start).total_seconds()
    if duration <= 0.0:
        return end_soc
    ratio = min(1.0, max(0.0, (timestamp - event_start).total_seconds() / duration))
    return start_soc + (end_soc - start_soc) * ratio


def _charging_end(event: pd.Series) -> pd.Timestamp:
    value = event.get("charging_end_datetime", pd.NaT)
    if pd.isna(value):
        return pd.Timestamp(event["end_datetime"])
    return min(pd.Timestamp(value), pd.Timestamp(event["end_datetime"]))


def _event_trip_id(row: pd.Series) -> str:
    trip_id = _string(row.get("trip_id", ""))
    if trip_id:
        return trip_id
    return f"{row.get('vehicle_day_id', 'vehicle_day')}_event_{int(row.get('event_seq', 0)):03d}"


def _feed_start(events: pd.DataFrame, feed_year_start: str | dt.date | pd.Timestamp | None) -> dt.date:
    if feed_year_start is not None:
        return _date(feed_year_start)
    return min(_date(value) for value in events["service_date"].dropna())


def _simulation_week(service_date: dt.date, feed_start: dt.date) -> int:
    return int((service_date - feed_start).days // 7)


def _date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, pd.Timestamp):
        return value.date()
    return dt.date.fromisoformat(str(value))


def _decimal_hour(timestamp: Any, service_date: dt.date) -> float:
    delta = pd.Timestamp(timestamp) - pd.Timestamp(service_date)
    return float(delta.total_seconds() / 3600.0)


def _soc_fraction(soc_kwh: Any, battery_kwh: Any) -> float:
    soc = _float_or_nan(soc_kwh)
    battery = _float_or_nan(battery_kwh)
    if not np.isfinite(soc) or not np.isfinite(battery) or battery <= 0.0:
        return np.nan
    return float(soc / battery)


def _float_or_nan(value: Any) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return np.nan
    return numeric if np.isfinite(numeric) else np.nan


def _float_or_zero(value: Any) -> float:
    numeric = _float_or_nan(value)
    return float(numeric) if np.isfinite(numeric) else 0.0


def _string(value: Any) -> str:
    if pd.isna(value):
        return ""
    return str(value)


__all__ = [
    "BUS_CHARGING_EVENT_COLUMNS",
    "BUS_EV_STATE_RECORD_COLUMNS",
    "BUS_TRIP_RECORD_COLUMNS",
    "build_bus_charging_event_records",
    "build_bus_ev_state_records",
    "build_bus_trip_records",
]

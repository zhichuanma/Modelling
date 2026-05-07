"""Calendar-year schedule expansion for bus blocks."""

from __future__ import annotations

import datetime as dt
import warnings
from typing import Any, Iterable

import pandas as pd

from mobility.core.constants import STEP_HOURS_DECISION, STEPS_PER_DAY_DECISION
from mobility.core.data_structures import DailySchedule, ParkingEvent, Trip

from .calendar import FEED_YEAR_END, FEED_YEAR_START
# Cross-module reuse: annual path must use the exact same deadhead helper and
# split logic as the single-day path. Importing private names is intentional —
# duplicating the implementation would let the two paths drift.
from .trip_chain_bus import (
    DeadheadInjectionStats,
    _inject_deadhead_trips,
    _ordered_valid_block_rows,
    _split_absolute_trip,
)


HOURS_PER_DAY = float(STEPS_PER_DAY_DECISION) * float(STEP_HOURS_DECISION)


def _coerce_date(value: str | dt.date | pd.Timestamp) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, pd.Timestamp):
        return value.date()
    return dt.date.fromisoformat(str(value))


def _date_range(start_date: dt.date, end_date: dt.date) -> list[dt.date]:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date.")
    return [start_date + dt.timedelta(days=offset) for offset in range((end_date - start_date).days + 1)]


def _as_str_or_empty(value: Any) -> str:
    return str(value) if pd.notna(value) else ""


def _block_metadata(block_df: pd.DataFrame) -> dict[str, Any]:
    ordered = block_df.sort_values("start_h")
    first = ordered.iloc[0]
    last = ordered.iloc[-1]
    return {
        "block_id": str(first["block_id"]) if "block_id" in ordered else "",
        "agency_id": str(first["agency_id"]) if "agency_id" in ordered else "",
        "service_id": str(first["service_id"]) if "service_id" in ordered else "",
        "block_source": str(first["block_source"]) if "block_source" in ordered else "",
        "start_stop": str(first["start_stop"]) if "start_stop" in ordered else "",
        "end_stop": str(last["end_stop"]) if "end_stop" in ordered else "",
    }


def _build_absolute_template_trip(
    row: pd.Series,
    *,
    consumption_kwh_per_km: float,
) -> Trip:
    """Build one block-template Trip with absolute hours, ready for deadhead injection."""
    distance_km = float(row["distance_km"])
    trip = Trip(
        trip_id=str(row["trip_id"]),
        departure_time=float(row["start_h"]),
        arrival_time=float(row["end_h"]),
        distance_km=distance_km,
        origin_purpose="bus_stop",
        destination_purpose="bus_stop",
        energy_consumed_kwh=distance_km * float(consumption_kwh_per_km),
        origin_lsoa=_as_str_or_empty(row.get("start_lsoa", "")),
        destination_lsoa=_as_str_or_empty(row.get("end_lsoa", "")),
    )
    for col in (
        "agency_id",
        "route_id",
        "service_id",
        "direction_id",
        "block_id",
        "block_source",
        "start_stop",
        "end_stop",
        "start_lat",
        "start_lon",
        "end_lat",
        "end_lon",
        "distance_source",
        "shape_id",
    ):
        if col in row.index:
            setattr(trip, col, row[col])
    trip.original_start_h = float(row["start_h"])
    trip.original_end_h = float(row["end_h"])
    return trip


def _stamp_year_trip(
    trip: Trip,
    *,
    service_date: dt.date,
    schedule_date: dt.date,
    local_start_h: float,
    local_end_h: float,
    distance_share: float,
) -> Trip:
    """Project an augmented template trip onto a specific (service_date, schedule_date)."""
    distance_km = float(trip.distance_km) * float(distance_share)
    energy_kwh = float(trip.energy_consumed_kwh) * float(distance_share)
    stamped = Trip(
        trip_id=f"{trip.trip_id}__{service_date.isoformat()}__{schedule_date.isoformat()}",
        departure_time=float(local_start_h),
        arrival_time=float(local_end_h),
        distance_km=distance_km,
        origin_purpose=trip.origin_purpose,
        destination_purpose=trip.destination_purpose,
        energy_consumed_kwh=energy_kwh,
        origin_lsoa=trip.origin_lsoa,
        destination_lsoa=trip.destination_lsoa,
        is_deadhead=bool(trip.is_deadhead),
        deadhead_class=str(trip.deadhead_class),
    )
    for col in (
        "agency_id",
        "route_id",
        "service_id",
        "direction_id",
        "block_id",
        "block_source",
        "start_stop",
        "end_stop",
        "start_lat",
        "start_lon",
        "end_lat",
        "end_lon",
        "distance_source",
        "shape_id",
    ):
        if hasattr(trip, col):
            setattr(stamped, col, getattr(trip, col))
    stamped.service_date = service_date
    stamped.schedule_date = schedule_date
    stamped.original_start_h = float(getattr(trip, "original_start_h", trip.departure_time))
    stamped.original_end_h = float(getattr(trip, "original_end_h", trip.arrival_time))
    return stamped


def _parking_event(
    start_h: float,
    end_h: float,
    purpose: str,
    *,
    can_charge: bool,
    charge_power_kw: float,
    location_lsoa: str = "",
) -> ParkingEvent | None:
    if end_h <= start_h:
        return None
    return ParkingEvent(
        start_time=float(start_h),
        end_time=float(end_h),
        duration_hours=float(end_h - start_h),
        location_purpose=purpose,
        location_lsoa=location_lsoa,
        can_charge=bool(can_charge),
        charge_power_kw=float(charge_power_kw) if can_charge else 0.0,
    )


def _attach_parking(
    schedule: DailySchedule,
    *,
    depot_charge_kw: float,
    allow_layover_charging: bool,
    layover_charge_kw: float,
    min_layover_for_charging_h: float,
) -> None:
    trips = sorted(schedule.trips, key=lambda trip: (trip.departure_time, trip.arrival_time, trip.trip_id))
    schedule.trips = trips
    schedule.parking_events = []
    metadata = getattr(schedule, "metadata", {})
    overlaps = [
        (left.trip_id, right.trip_id, left.arrival_time, right.departure_time)
        for left, right in zip(trips[:-1], trips[1:])
        if right.departure_time < left.arrival_time
    ]
    metadata["n_trip_overlaps"] = int(len(overlaps))
    schedule.metadata = metadata
    if overlaps:
        suffix = "..." if len(overlaps) > 3 else ""
        warnings.warn(
            f"Trip overlap on aggregated day: {overlaps[:3]}{suffix}. "
            "Day-1 tail of a cross-midnight block collided with the next "
            "service day; layovers in the overlap window are dropped.",
            UserWarning,
            stacklevel=2,
        )
    if not trips:
        event = _parking_event(
            0.0,
            HOURS_PER_DAY,
            "depot_terminus",
            can_charge=True,
            charge_power_kw=depot_charge_kw,
        )
        if event is not None:
            schedule.parking_events.append(event)
        return

    first_event = _parking_event(
        0.0,
        trips[0].departure_time,
        "depot_terminus",
        can_charge=True,
        charge_power_kw=depot_charge_kw,
        location_lsoa=trips[0].origin_lsoa,
    )
    if first_event is not None:
        schedule.parking_events.append(first_event)

    for left, right in zip(trips[:-1], trips[1:]):
        if right.departure_time <= left.arrival_time:
            continue
        duration_h = right.departure_time - left.arrival_time
        can_charge = bool(allow_layover_charging and duration_h >= min_layover_for_charging_h)
        layover_event = _parking_event(
            left.arrival_time,
            right.departure_time,
            "layover",
            can_charge=can_charge,
            charge_power_kw=layover_charge_kw,
            location_lsoa=left.destination_lsoa,
        )
        if layover_event is not None:
            schedule.parking_events.append(layover_event)

    last_event = _parking_event(
        trips[-1].arrival_time,
        HOURS_PER_DAY,
        "depot_terminus",
        can_charge=True,
        charge_power_kw=depot_charge_kw,
        location_lsoa=trips[-1].destination_lsoa,
    )
    if last_event is not None:
        schedule.parking_events.append(last_event)


def _set_deadhead_metadata(
    metadata: dict[str, Any],
    stats: DeadheadInjectionStats,
    *,
    is_active: bool,
) -> None:
    if is_active:
        metadata["deadhead_short_count"] = int(stats.short_count)
        metadata["deadhead_long_count"] = int(stats.long_count)
        metadata["deadhead_total_km"] = float(stats.total_km)
        metadata["deadhead_total_kwh"] = float(stats.total_kwh)
        metadata["deadhead_skipped_time_count"] = int(stats.skipped_time_count)
        metadata["deadhead_skipped_time_km"] = float(stats.skipped_time_km)
        metadata["deadhead_skipped_missing_coord_count"] = int(stats.skipped_missing_coord_count)
    else:
        metadata["deadhead_short_count"] = 0
        metadata["deadhead_long_count"] = 0
        metadata["deadhead_total_km"] = 0.0
        metadata["deadhead_total_kwh"] = 0.0
        metadata["deadhead_skipped_time_count"] = 0
        metadata["deadhead_skipped_time_km"] = 0.0
        metadata["deadhead_skipped_missing_coord_count"] = 0


def block_to_year_schedules(
    block_df: pd.DataFrame,
    active_dates: Iterable[str | dt.date | pd.Timestamp],
    start_date: str | dt.date | pd.Timestamp = FEED_YEAR_START,
    end_date: str | dt.date | pd.Timestamp = FEED_YEAR_END,
    ev_id: str | None = None,
    *,
    consumption_kwh_per_km: float,
    depot_charge_kw: float,
    allow_layover_charging: bool = False,
    layover_charge_kw: float = 0.0,
    min_layover_for_charging_h: float = 0.0,
) -> list[DailySchedule]:
    """Expand one representative bus block into dated annual schedules.

    Deadhead injection runs once on the block template (absolute hours) and
    is then stamped onto every active service_date — this matches the
    single-day path's behaviour and keeps the same constants/thresholds.
    Each active service_date independently fires one template's worth of
    deadheads; inactive dates carry zero deadhead audit values.
    """
    if block_df.empty:
        raise ValueError("block_df must contain at least one trip.")
    if consumption_kwh_per_km <= 0.0:
        raise ValueError("consumption_kwh_per_km must be positive.")
    if depot_charge_kw < 0.0 or layover_charge_kw < 0.0:
        raise ValueError("charge powers must be non-negative.")

    start = _coerce_date(start_date)
    end = _coerce_date(end_date)
    dates = _date_range(start, end)
    block_id = str(block_df["block_id"].iloc[0]) if "block_id" in block_df else "bus_block"
    schedule_ev_id = ev_id or f"bus_{block_id}"
    metadata = _block_metadata(block_df)
    active_set = {_coerce_date(value) for value in active_dates}
    schedules_by_date = {
        date_value: DailySchedule(
            ev_id=schedule_ev_id,
            day=day_index,
            day_type="weekend" if date_value.weekday() >= 5 else "weekday",
            date=date_value,
        )
        for day_index, date_value in enumerate(dates)
    }

    ordered = _ordered_valid_block_rows(block_df)
    raw_trips = [
        _build_absolute_template_trip(row, consumption_kwh_per_km=consumption_kwh_per_km)
        for _, row in ordered.iterrows()
    ]
    augmented_trips, deadhead_stats = _inject_deadhead_trips(
        raw_trips,
        consumption_kwh_per_km=consumption_kwh_per_km,
    )

    for service_date in sorted(active_set):
        for trip in augmented_trips:
            for day_offset, local_start_h, local_end_h, distance_share in _split_absolute_trip(trip):
                schedule_date = service_date + dt.timedelta(days=day_offset)
                schedule = schedules_by_date.get(schedule_date)
                if schedule is None:
                    continue
                schedule.trips.append(
                    _stamp_year_trip(
                        trip,
                        service_date=service_date,
                        schedule_date=schedule_date,
                        local_start_h=local_start_h,
                        local_end_h=local_end_h,
                        distance_share=distance_share,
                    )
                )

    output: list[DailySchedule] = []
    for date_value in dates:
        schedule = schedules_by_date[date_value]
        schedule.metadata = dict(metadata)
        schedule.metadata["schedule_date"] = date_value.isoformat()
        is_active = bool(date_value in active_set)
        schedule.metadata["service_active"] = is_active
        schedule.metadata["n_trips"] = int(len(schedule.trips))
        _set_deadhead_metadata(schedule.metadata, deadhead_stats, is_active=is_active)
        _attach_parking(
            schedule,
            depot_charge_kw=depot_charge_kw,
            allow_layover_charging=allow_layover_charging,
            layover_charge_kw=layover_charge_kw,
            min_layover_for_charging_h=min_layover_for_charging_h,
        )
        output.append(schedule)
    return output

"""Calendar-year schedule expansion for bus blocks."""

from __future__ import annotations

import datetime as dt
from typing import Any, Iterable

import pandas as pd

from mobility.core.constants import STEP_HOURS_DECISION, STEPS_PER_DAY_DECISION
from mobility.core.data_structures import DailySchedule, ParkingEvent, Trip

from .calendar import FEED_YEAR_END, FEED_YEAR_START


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


def _split_row(row: pd.Series) -> list[tuple[int, float, float, float]]:
    start_h = float(row["start_h"])
    end_h = float(row["end_h"])
    if end_h <= start_h:
        raise ValueError(f"Trip {row.get('trip_id', '<unknown>')} has end_h <= start_h.")
    if start_h < 0.0 or end_h > 2.0 * HOURS_PER_DAY:
        raise ValueError("Bus annual schedules may span at most one wrapped service day.")

    total_duration_h = end_h - start_h
    segments: list[tuple[int, float, float, float]] = []

    def add(day_offset: int, local_start_h: float, local_end_h: float, abs_start_h: float, abs_end_h: float) -> None:
        if local_end_h <= local_start_h:
            return
        share = (abs_end_h - abs_start_h) / total_duration_h
        segments.append((day_offset, local_start_h, local_end_h, share))

    if start_h < HOURS_PER_DAY:
        day0_end_h = min(end_h, HOURS_PER_DAY)
        add(0, start_h, day0_end_h, start_h, day0_end_h)
        if end_h > HOURS_PER_DAY:
            add(1, 0.0, end_h - HOURS_PER_DAY, HOURS_PER_DAY, end_h)
    else:
        add(1, start_h - HOURS_PER_DAY, end_h - HOURS_PER_DAY, start_h, end_h)
    return segments


def _build_trip(
    row: pd.Series,
    *,
    service_date: dt.date,
    schedule_date: dt.date,
    local_start_h: float,
    local_end_h: float,
    distance_share: float,
    consumption_kwh_per_km: float,
) -> Trip:
    distance_km = float(row["distance_km"]) * float(distance_share)
    trip = Trip(
        trip_id=f"{row['trip_id']}__{service_date.isoformat()}__{schedule_date.isoformat()}",
        departure_time=float(local_start_h),
        arrival_time=float(local_end_h),
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
        "distance_source",
        "shape_id",
    ):
        if col in row.index:
            setattr(trip, col, row[col])
    trip.service_date = service_date
    trip.schedule_date = schedule_date
    trip.original_start_h = float(row["start_h"])
    trip.original_end_h = float(row["end_h"])
    return trip


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
    """Expand one representative bus block into dated annual schedules."""
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

    ordered = block_df.sort_values(["start_h", "end_h", "trip_id"])
    for service_date in sorted(active_set):
        for _, row in ordered.iterrows():
            for day_offset, local_start_h, local_end_h, distance_share in _split_row(row):
                schedule_date = service_date + dt.timedelta(days=day_offset)
                schedule = schedules_by_date.get(schedule_date)
                if schedule is None:
                    continue
                schedule.trips.append(
                    _build_trip(
                        row,
                        service_date=service_date,
                        schedule_date=schedule_date,
                        local_start_h=local_start_h,
                        local_end_h=local_end_h,
                        distance_share=distance_share,
                        consumption_kwh_per_km=consumption_kwh_per_km,
                    )
                )

    output: list[DailySchedule] = []
    for date_value in dates:
        schedule = schedules_by_date[date_value]
        schedule.metadata = dict(metadata)
        schedule.metadata["schedule_date"] = date_value.isoformat()
        schedule.metadata["service_active"] = bool(date_value in active_set)
        schedule.metadata["n_trips"] = int(len(schedule.trips))
        _attach_parking(
            schedule,
            depot_charge_kw=depot_charge_kw,
            allow_layover_charging=allow_layover_charging,
            layover_charge_kw=layover_charge_kw,
            min_layover_for_charging_h=min_layover_for_charging_h,
        )
        output.append(schedule)
    return output

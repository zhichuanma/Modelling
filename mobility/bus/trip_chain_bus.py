"""Convert bus blocks into simulator-ready daily schedules."""

from __future__ import annotations

from typing import Any

import pandas as pd

from mobility.core.constants import STEP_HOURS_DECISION, STEPS_PER_DAY_DECISION
from mobility.core.data_structures import DailySchedule, ParkingEvent, Trip


HOURS_PER_DAY = float(STEPS_PER_DAY_DECISION) * float(STEP_HOURS_DECISION)


def _as_float(value: Any) -> float:
    return float(value) if pd.notna(value) else float("nan")


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
        "terminus_start_lat": _as_float(first.get("start_lat")),
        "terminus_start_lon": _as_float(first.get("start_lon")),
        "terminus_end_lat": _as_float(last.get("end_lat")),
        "terminus_end_lon": _as_float(last.get("end_lon")),
    }


def _split_row(row: pd.Series) -> list[tuple[int, float, float, float]]:
    start_h = float(row["start_h"])
    end_h = float(row["end_h"])
    if end_h <= start_h:
        raise ValueError(f"Trip {row.get('trip_id', '<unknown>')} has end_h <= start_h.")
    if start_h < 0.0 or end_h > 2.0 * HOURS_PER_DAY:
        raise ValueError("Bus block schedules may span at most one wrapped service day.")

    total_duration_h = end_h - start_h
    segments: list[tuple[int, float, float, float]] = []

    def add(day: int, local_start_h: float, local_end_h: float, abs_start_h: float, abs_end_h: float) -> None:
        if local_end_h <= local_start_h:
            return
        share = (abs_end_h - abs_start_h) / total_duration_h
        segments.append((day, local_start_h, local_end_h, share))

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
    day: int,
    local_start_h: float,
    local_end_h: float,
    distance_share: float,
    consumption_kwh_per_km: float,
) -> Trip:
    distance_km = float(row["distance_km"]) * float(distance_share)
    trip = Trip(
        trip_id=f"{row['trip_id']}__d{day}",
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
    trip.original_start_h = float(row["start_h"])
    trip.original_end_h = float(row["end_h"])
    trip.day_segment = int(day)
    return trip


def _depot_event(
    start_h: float,
    end_h: float,
    depot_charge_kw: float,
    *,
    location_lsoa: str = "",
) -> ParkingEvent:
    return ParkingEvent(
        start_time=float(start_h),
        end_time=float(end_h),
        duration_hours=float(end_h - start_h),
        location_purpose="depot_terminus",
        location_lsoa=location_lsoa,
        can_charge=True,
        charge_power_kw=float(depot_charge_kw),
    )


def _layover_event(
    start_h: float,
    end_h: float,
    *,
    allow_layover_charging: bool,
    layover_charge_kw: float,
    min_layover_for_charging_h: float,
    location_lsoa: str = "",
) -> ParkingEvent:
    duration_h = float(end_h - start_h)
    can_charge = bool(allow_layover_charging and duration_h >= min_layover_for_charging_h)
    return ParkingEvent(
        start_time=float(start_h),
        end_time=float(end_h),
        duration_hours=duration_h,
        location_purpose="layover",
        location_lsoa=location_lsoa,
        can_charge=can_charge,
        charge_power_kw=float(layover_charge_kw) if can_charge else 0.0,
    )


def _attach_parking(
    schedule: DailySchedule,
    *,
    depot_charge_kw: float,
    allow_layover_charging: bool,
    layover_charge_kw: float,
    min_layover_for_charging_h: float,
) -> None:
    trips = sorted(schedule.trips, key=lambda trip: trip.departure_time)
    schedule.trips = trips
    if not trips:
        # No trip endpoints exist, so the all-day depot placeholder has no LSOA.
        schedule.parking_events.append(_depot_event(0.0, HOURS_PER_DAY, depot_charge_kw))
        return

    schedule.parking_events.append(
        _depot_event(
            0.0,
            trips[0].departure_time,
            depot_charge_kw,
            location_lsoa=trips[0].origin_lsoa,
        )
    )
    for left, right in zip(trips[:-1], trips[1:]):
        if right.departure_time <= left.arrival_time:
            continue
        schedule.parking_events.append(
            _layover_event(
                left.arrival_time,
                right.departure_time,
                allow_layover_charging=allow_layover_charging,
                layover_charge_kw=layover_charge_kw,
                min_layover_for_charging_h=min_layover_for_charging_h,
                location_lsoa=left.destination_lsoa,
            )
        )
    schedule.parking_events.append(
        _depot_event(
            trips[-1].arrival_time,
            HOURS_PER_DAY,
            depot_charge_kw,
            location_lsoa=trips[-1].destination_lsoa,
        )
    )


def block_to_daily_schedules(
    block_df: pd.DataFrame,
    ev_id: str,
    *,
    consumption_kwh_per_km: float,
    depot_charge_kw: float,
    allow_layover_charging: bool = False,
    layover_charge_kw: float = 0.0,
    min_layover_for_charging_h: float = 0.0,
) -> list[DailySchedule]:
    """Convert one bus block into one or two ``DailySchedule`` objects."""
    if block_df.empty:
        raise ValueError("block_df must contain at least one trip.")
    if consumption_kwh_per_km <= 0.0:
        raise ValueError("consumption_kwh_per_km must be positive.")
    if depot_charge_kw < 0.0 or layover_charge_kw < 0.0:
        raise ValueError("charge powers must be non-negative.")

    ordered = block_df.sort_values("start_h")
    metadata = _block_metadata(ordered)
    needs_wrapped_day = bool((ordered["end_h"] > HOURS_PER_DAY).any() or (ordered["start_h"] >= HOURS_PER_DAY).any())
    days = [0, 1] if needs_wrapped_day else [0]
    schedules = {
        day: DailySchedule(ev_id=ev_id, day=day, day_type="representative_service_day")
        for day in days
    }

    for _, row in ordered.iterrows():
        for day, local_start_h, local_end_h, distance_share in _split_row(row):
            if day not in schedules:
                schedules[day] = DailySchedule(ev_id=ev_id, day=day, day_type="representative_service_day")
            schedules[day].trips.append(
                _build_trip(
                    row,
                    day=day,
                    local_start_h=local_start_h,
                    local_end_h=local_end_h,
                    distance_share=distance_share,
                    consumption_kwh_per_km=consumption_kwh_per_km,
                )
            )

    output: list[DailySchedule] = []
    for day in sorted(schedules):
        schedule = schedules[day]
        schedule.metadata = dict(metadata)
        schedule.metadata["schedule_day"] = int(day)
        schedule.metadata["n_original_trips"] = int(len(ordered))
        schedule.metadata["original_total_km"] = float(ordered["distance_km"].sum())
        _attach_parking(
            schedule,
            depot_charge_kw=depot_charge_kw,
            allow_layover_charging=allow_layover_charging,
            layover_charge_kw=layover_charge_kw,
            min_layover_for_charging_h=min_layover_for_charging_h,
        )
        output.append(schedule)
    return output

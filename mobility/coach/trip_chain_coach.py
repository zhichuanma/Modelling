"""Convert one coach vehicle journey into simulator-ready schedules."""

from __future__ import annotations

from typing import Any

import pandas as pd

from mobility.core.constants import STEP_HOURS_DECISION, STEPS_PER_DAY_DECISION
from mobility.core.data_structures import DailySchedule, ParkingEvent, Trip

from ._compat import field


HOURS_PER_DAY = float(STEPS_PER_DAY_DECISION) * float(STEP_HOURS_DECISION)


def _clock_to_hours(value: Any) -> float:
    if value is None or pd.isna(value):
        return float("nan")
    if isinstance(value, (int, float)):
        return float(value)
    parts = str(value).split(":")
    if len(parts) < 2:
        return float("nan")
    hours = float(parts[0])
    minutes = float(parts[1])
    seconds = float(parts[2]) if len(parts) > 2 else 0.0
    return hours + minutes / 60.0 + seconds / 3600.0


def _journey_times(row: Any) -> tuple[float, float]:
    start_h = field(row, "start_h", None)
    end_h = field(row, "end_h", None)
    start = float(start_h) if start_h is not None and pd.notna(start_h) else _clock_to_hours(field(row, "departure_time"))
    end = float(end_h) if end_h is not None and pd.notna(end_h) else _clock_to_hours(field(row, "arrival_time"))
    runtime_min = field(row, "runtime_min", None)
    if pd.notna(runtime_min) and (not pd.notna(end) or end <= start):
        end = start + float(runtime_min) / 60.0
    if not pd.notna(start) or not pd.notna(end):
        raise ValueError("journey_row must provide start/end hours or departure/arrival times.")
    if end <= start:
        raise ValueError("coach journey end_h must be greater than start_h.")
    if start < 0.0:
        raise ValueError("start_h must be non-negative.")
    if start >= HOURS_PER_DAY:
        raise ValueError(
            f"start_h={start} must be < 24 (vehicle journeys never start in 'next day' clock)."
        )
    if end > 2.0 * HOURS_PER_DAY:
        raise ValueError(f"end_h={end} exceeds 48h; cross-midnight beyond day1 not supported.")
    return float(start), float(end)


def _stop_text(stop_seq: pd.DataFrame, position: str, column: str, fallback: str = "") -> str:
    if not isinstance(stop_seq, pd.DataFrame) or stop_seq.empty or column not in stop_seq.columns:
        return fallback
    ordered = stop_seq.sort_values("stop_sequence") if "stop_sequence" in stop_seq.columns else stop_seq
    row = ordered.iloc[0] if position == "first" else ordered.iloc[-1]
    value = row.get(column, fallback)
    return str(value) if pd.notna(value) else fallback


def _split_trip(start_h: float, end_h: float) -> list[tuple[int, float, float, float]]:
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


def _terminus_event(
    start_h: float,
    end_h: float,
    terminus_charge_kw: float,
    *,
    location_lsoa: str = "",
) -> ParkingEvent | None:
    if end_h <= start_h:
        return None
    return ParkingEvent(
        start_time=float(start_h),
        end_time=float(end_h),
        duration_hours=float(end_h - start_h),
        location_purpose="terminus_dwell",
        location_lsoa=location_lsoa,
        can_charge=terminus_charge_kw > 0.0,
        charge_power_kw=float(terminus_charge_kw),
    )


def journey_to_daily_schedules(
    journey_row: Any,
    stop_seq: pd.DataFrame,
    *,
    consumption_kwh_per_km: float,
    terminus_charge_kw: float = 50.0,
    pre_journey_dwell_h: float = 6.0,
) -> list[DailySchedule]:
    """Convert one coach journey into one or two ``DailySchedule`` objects."""
    if consumption_kwh_per_km <= 0.0:
        raise ValueError("consumption_kwh_per_km must be positive.")
    if terminus_charge_kw < 0.0:
        raise ValueError("terminus_charge_kw must be non-negative.")
    if pre_journey_dwell_h < 0.0:
        raise ValueError("pre_journey_dwell_h must be non-negative.")

    start_h, end_h = _journey_times(journey_row)
    distance_km = field(journey_row, "distance_km", None)
    if distance_km is None or pd.isna(distance_km):
        raise ValueError("journey_row must contain a known distance_km.")
    distance_km = float(distance_km)
    if distance_km < 0.0:
        raise ValueError("distance_km must be non-negative.")

    journey_code = str(field(journey_row, "vehicle_journey_code", "coach_journey"))
    ev_id = f"coach_{journey_code}"
    segments = _split_trip(start_h, end_h)
    schedules = {
        day: DailySchedule(ev_id=ev_id, day=day, day_type="representative_service_day")
        for day in sorted({segment[0] for segment in segments})
    }

    for day, local_start_h, local_end_h, distance_share in segments:
        segment_distance_km = distance_km * distance_share
        trip = Trip(
            trip_id=f"{journey_code}__d{day}",
            departure_time=float(local_start_h),
            arrival_time=float(local_end_h),
            distance_km=float(segment_distance_km),
            origin_purpose="coach_stop",
            destination_purpose="coach_stop",
            energy_consumed_kwh=float(segment_distance_km * consumption_kwh_per_km),
            origin_lsoa=str(field(journey_row, "start_lsoa", "") or ""),
            destination_lsoa=str(field(journey_row, "end_lsoa", "") or ""),
        )
        for col in (
            "operator_code",
            "operator_name",
            "service_code",
            "line_name",
            "vehicle_journey_code",
            "distance_source",
            "start_stop_ref",
            "end_stop_ref",
        ):
            setattr(trip, col, field(journey_row, col, ""))
        trip.original_start_h = float(start_h)
        trip.original_end_h = float(end_h)
        trip.day_segment = int(day)
        schedules[day].trips.append(trip)

    first_day = segments[0][0]
    first_start = segments[0][1]
    pre_start = max(0.0, first_start - float(pre_journey_dwell_h))
    pre_event = _terminus_event(pre_start, first_start, terminus_charge_kw, location_lsoa=str(field(journey_row, "start_lsoa", "") or ""))
    if pre_event is not None:
        schedules[first_day].parking_events.append(pre_event)

    last_day = segments[-1][0]
    last_end = segments[-1][2]
    post_event = _terminus_event(last_end, HOURS_PER_DAY, terminus_charge_kw, location_lsoa=str(field(journey_row, "end_lsoa", "") or ""))
    if post_event is not None:
        schedules[last_day].parking_events.append(post_event)

    output: list[DailySchedule] = []
    for day in sorted(schedules):
        schedule = schedules[day]
        schedule.trips.sort(key=lambda trip: trip.departure_time)
        schedule.parking_events.sort(key=lambda event: event.start_time)
        schedule.metadata = {
            "vehicle_journey_code": journey_code,
            "operator_code": str(field(journey_row, "operator_code", "")),
            "operator_name": str(field(journey_row, "operator_name", "")),
            "line_name": str(field(journey_row, "line_name", "")),
            "start_stop_ref": _stop_text(stop_seq, "first", "stop_point_ref", str(field(journey_row, "start_stop_ref", ""))),
            "end_stop_ref": _stop_text(stop_seq, "last", "stop_point_ref", str(field(journey_row, "end_stop_ref", ""))),
            "start_stop_name": _stop_text(stop_seq, "first", "common_name", str(field(journey_row, "start_stop_name", ""))),
            "end_stop_name": _stop_text(stop_seq, "last", "common_name", str(field(journey_row, "end_stop_name", ""))),
            "schedule_day": int(day),
            "original_distance_km": float(distance_km),
            "original_start_h": float(start_h),
            "original_end_h": float(end_h),
        }
        output.append(schedule)
    return output

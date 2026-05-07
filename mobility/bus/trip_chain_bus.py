"""Convert bus blocks into simulator-ready daily schedules."""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from mobility.core.constants import STEP_HOURS_DECISION, STEPS_PER_DAY_DECISION
from mobility.core.data_structures import DailySchedule, ParkingEvent, Trip

from .distance import haversine_km


HOURS_PER_DAY = float(STEPS_PER_DAY_DECISION) * float(STEP_HOURS_DECISION)
DEADHEAD_NOISE_KM = 0.5
DEADHEAD_SHORT_KM = 5.0
DEADHEAD_SPEED_KMH = 30.0
DEADHEAD_MIN_DWELL_H = 0.05


@dataclass
class DeadheadInjectionStats:
    short_count: int = 0
    long_count: int = 0
    total_km: float = 0.0
    total_kwh: float = 0.0
    skipped_time_count: int = 0
    skipped_time_km: float = 0.0
    skipped_missing_coord_count: int = 0


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


def _ordered_valid_block_rows(block_df: pd.DataFrame) -> pd.DataFrame:
    """Return stable-ordered rows with strictly positive trip durations only."""
    ordered = block_df.sort_values(["start_h", "end_h", "trip_id"])
    valid = ordered["end_h"].astype(float) > ordered["start_h"].astype(float)
    if not bool(valid.all()):
        invalid_count = int((~valid).sum())
        warnings.warn(
            f"Dropping {invalid_count} non-positive-duration bus trips before schedule assembly.",
            UserWarning,
            stacklevel=2,
        )
        ordered = ordered.loc[valid].copy()
    if ordered.empty:
        raise ValueError("block_df has no trips with end_h > start_h.")
    return ordered


def _split_absolute_trip(trip: Trip) -> list[tuple[int, float, float, float]]:
    start_h = float(trip.departure_time)
    end_h = float(trip.arrival_time)
    if end_h <= start_h:
        raise ValueError(f"Trip {trip.trip_id} has end_h <= start_h.")
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


def _set_trip_attrs_from_row(trip: Trip, row: pd.Series) -> None:
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


def _build_absolute_trip(
    row: pd.Series,
    *,
    consumption_kwh_per_km: float,
) -> Trip:
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
    _set_trip_attrs_from_row(trip, row)
    trip.original_start_h = float(row["start_h"])
    trip.original_end_h = float(row["end_h"])
    return trip


def _copy_trip_attrs(source: Trip, target: Trip) -> None:
    for key, value in vars(source).items():
        if key not in {
            "trip_id",
            "departure_time",
            "arrival_time",
            "distance_km",
            "origin_purpose",
            "destination_purpose",
            "energy_consumed_kwh",
            "origin_lsoa",
            "destination_lsoa",
            "distance_km_nts",
            "fallback_distance",
            "is_deadhead",
            "deadhead_class",
        }:
            setattr(target, key, value)


def _segment_trip(trip: Trip, *, day: int, local_start_h: float, local_end_h: float, distance_share: float) -> Trip:
    segmented = Trip(
        trip_id=f"{trip.trip_id}__d{day}",
        departure_time=float(local_start_h),
        arrival_time=float(local_end_h),
        distance_km=float(trip.distance_km) * float(distance_share),
        origin_purpose=trip.origin_purpose,
        destination_purpose=trip.destination_purpose,
        energy_consumed_kwh=float(trip.energy_consumed_kwh) * float(distance_share),
        origin_lsoa=trip.origin_lsoa,
        destination_lsoa=trip.destination_lsoa,
        distance_km_nts=float(trip.distance_km_nts) * float(distance_share),
        fallback_distance=bool(trip.fallback_distance),
        is_deadhead=bool(trip.is_deadhead),
        deadhead_class=str(trip.deadhead_class),
    )
    _copy_trip_attrs(trip, segmented)
    segmented.original_start_h = float(getattr(trip, "original_start_h", trip.departure_time))
    segmented.original_end_h = float(getattr(trip, "original_end_h", trip.arrival_time))
    segmented.day_segment = int(day)
    return segmented


def _endpoint_float(trip: Trip, attr: str) -> float:
    return _as_float(getattr(trip, attr, np.nan))


def _inject_deadhead_trips(
    trips: list[Trip],
    *,
    consumption_kwh_per_km: float,
) -> tuple[list[Trip], DeadheadInjectionStats]:
    """Insert simulator-visible deadhead trips between discontinuous service trips."""
    augmented: list[Trip] = []
    stats = DeadheadInjectionStats()

    for idx, left in enumerate(trips):
        augmented.append(left)
        if idx == len(trips) - 1:
            break

        right = trips[idx + 1]
        if left.is_deadhead or right.is_deadhead:
            continue

        coords = (
            _endpoint_float(left, "end_lat"),
            _endpoint_float(left, "end_lon"),
            _endpoint_float(right, "start_lat"),
            _endpoint_float(right, "start_lon"),
        )
        if not all(np.isfinite(value) for value in coords):
            stats.skipped_missing_coord_count += 1
            continue

        deadhead_km = float(haversine_km(coords[0], coords[1], coords[2], coords[3]))
        if deadhead_km < DEADHEAD_NOISE_KM:
            continue

        deadhead_h = deadhead_km / DEADHEAD_SPEED_KMH
        depart_h = float(left.arrival_time)
        arrive_h = depart_h + deadhead_h
        if not np.isfinite(arrive_h) or arrive_h <= depart_h:
            stats.skipped_time_count += 1
            stats.skipped_time_km += deadhead_km
            continue
        if arrive_h + DEADHEAD_MIN_DWELL_H > float(right.departure_time):
            stats.skipped_time_count += 1
            stats.skipped_time_km += deadhead_km
            continue

        deadhead_class = "short" if deadhead_km < DEADHEAD_SHORT_KM else "long"
        energy_kwh = deadhead_km * float(consumption_kwh_per_km)
        deadhead = Trip(
            trip_id=f"DH_{left.trip_id}__{right.trip_id}",
            departure_time=depart_h,
            arrival_time=arrive_h,
            distance_km=deadhead_km,
            origin_purpose="deadhead",
            destination_purpose="deadhead",
            energy_consumed_kwh=energy_kwh,
            origin_lsoa=left.destination_lsoa,
            destination_lsoa=right.origin_lsoa,
            is_deadhead=True,
            deadhead_class=deadhead_class,
        )
        for attr in ("agency_id", "service_id", "direction_id", "block_id", "block_source"):
            if hasattr(left, attr):
                setattr(deadhead, attr, getattr(left, attr))
        deadhead.route_id = getattr(left, "route_id", "")
        deadhead.start_stop = getattr(left, "end_stop", "")
        deadhead.end_stop = getattr(right, "start_stop", "")
        deadhead.start_lat = coords[0]
        deadhead.start_lon = coords[1]
        deadhead.end_lat = coords[2]
        deadhead.end_lon = coords[3]
        deadhead.distance_source = "deadhead_haversine"
        deadhead.shape_id = ""
        deadhead.original_start_h = depart_h
        deadhead.original_end_h = arrive_h
        augmented.append(deadhead)

        if deadhead_class == "short":
            stats.short_count += 1
        else:
            stats.long_count += 1
        stats.total_km += deadhead_km
        stats.total_kwh += energy_kwh

    return augmented, stats


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

    ordered = _ordered_valid_block_rows(block_df)
    metadata = _block_metadata(ordered)
    needs_wrapped_day = bool((ordered["end_h"] > HOURS_PER_DAY).any() or (ordered["start_h"] >= HOURS_PER_DAY).any())
    days = [0, 1] if needs_wrapped_day else [0]
    schedules = {
        day: DailySchedule(ev_id=ev_id, day=day, day_type="representative_service_day")
        for day in days
    }

    raw_trips = [
        _build_absolute_trip(row, consumption_kwh_per_km=consumption_kwh_per_km)
        for _, row in ordered.iterrows()
    ]
    augmented_trips, deadhead_stats = _inject_deadhead_trips(
        raw_trips,
        consumption_kwh_per_km=consumption_kwh_per_km,
    )
    if augmented_trips:
        needs_wrapped_day = needs_wrapped_day or any(trip.arrival_time > HOURS_PER_DAY for trip in augmented_trips)
        if needs_wrapped_day and 1 not in schedules:
            schedules[1] = DailySchedule(ev_id=ev_id, day=1, day_type="representative_service_day")

    for trip in augmented_trips:
        for day, local_start_h, local_end_h, distance_share in _split_absolute_trip(trip):
            if day not in schedules:
                schedules[day] = DailySchedule(ev_id=ev_id, day=day, day_type="representative_service_day")
            schedules[day].trips.append(
                _segment_trip(
                    trip,
                    day=day,
                    local_start_h=local_start_h,
                    local_end_h=local_end_h,
                    distance_share=distance_share,
                )
            )

    output: list[DailySchedule] = []
    stats_dict = {
        "deadhead_short_count": int(deadhead_stats.short_count),
        "deadhead_long_count": int(deadhead_stats.long_count),
        "deadhead_total_km": float(deadhead_stats.total_km),
        "deadhead_total_kwh": float(deadhead_stats.total_kwh),
        "deadhead_skipped_time_count": int(deadhead_stats.skipped_time_count),
        "deadhead_skipped_time_km": float(deadhead_stats.skipped_time_km),
        "deadhead_skipped_missing_coord_count": int(deadhead_stats.skipped_missing_coord_count),
    }
    for day in sorted(schedules):
        schedule = schedules[day]
        schedule.metadata = dict(metadata)
        schedule.metadata["schedule_day"] = int(day)
        schedule.metadata["n_original_trips"] = int(len(ordered))
        schedule.metadata["original_total_km"] = float(ordered["distance_km"].sum())
        schedule.metadata.update(stats_dict)
        _attach_parking(
            schedule,
            depot_charge_kw=depot_charge_kw,
            allow_layover_charging=allow_layover_charging,
            layover_charge_kw=layover_charge_kw,
            min_layover_for_charging_h=min_layover_for_charging_h,
        )
        output.append(schedule)
    return output

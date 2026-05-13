"""Feed-year schedule expansion for synthetic coach chains."""

from __future__ import annotations

import copy
import datetime as dt
from typing import Iterable

import pandas as pd

from mobility.core.constants import STEP_HOURS_DECISION, STEPS_PER_DAY_DECISION
from mobility.core.data_structures import DailySchedule, ParkingEvent

from .calendar import COACH_FEED_YEAR_END, COACH_FEED_YEAR_START
from .trip_chain_coach import journey_to_daily_schedules


HOURS_PER_DAY = float(STEPS_PER_DAY_DECISION) * float(STEP_HOURS_DECISION)


def _coerce_date(value: object) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, pd.Timestamp):
        return value.date()
    return dt.date.fromisoformat(str(value))


def annual_dates(
    start_date: dt.date = COACH_FEED_YEAR_START,
    end_date: dt.date = COACH_FEED_YEAR_END,
) -> list[dt.date]:
    """Return the inclusive coach feed-year date list."""
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date.")
    return [start_date + dt.timedelta(days=offset) for offset in range((end_date - start_date).days + 1)]


def _chain_id(chain_journeys: pd.DataFrame) -> str:
    for column in ("coach_chain_template_id", "coach_chain_id", "chain_id"):
        if column in chain_journeys.columns:
            values = chain_journeys[column].dropna().astype(str).unique()
            if len(values):
                return str(values[0])
    return "coach_chain"


def _ordered_journeys(chain_journeys: pd.DataFrame) -> pd.DataFrame:
    sort_cols = [col for col in ("position_in_chain", "start_h", "end_h", "journey_id") if col in chain_journeys.columns]
    return chain_journeys.sort_values(sort_cols, kind="stable") if sort_cols else chain_journeys


def _consumption_for_row(row: pd.Series, consumption_kwh_per_km: float | None) -> float:
    value = row.get("consumption_kwh_per_km", consumption_kwh_per_km)
    if value is None or pd.isna(value):
        raise ValueError("consumption_kwh_per_km must be provided for chain schedule expansion.")
    value = float(value)
    if value <= 0.0:
        raise ValueError("consumption_kwh_per_km must be positive.")
    return value


def _event(
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
    can_charge = bool(can_charge and float(charge_power_kw) > 0.0)
    return ParkingEvent(
        start_time=float(start_h),
        end_time=float(end_h),
        duration_hours=float(end_h - start_h),
        location_purpose=purpose,
        location_lsoa=str(location_lsoa or ""),
        can_charge=can_charge,
        charge_power_kw=float(charge_power_kw) if can_charge else 0.0,
    )


def _attach_chain_parking(
    schedule: DailySchedule,
    *,
    pre_journey_dwell_h: float,
    terminus_charge_kw: float,
    terminus_dwell_purpose: str,
    allow_layover_charging: bool,
    layover_charge_kw: float,
    min_layover_for_charging_h: float,
) -> None:
    trips = sorted(schedule.trips, key=lambda trip: (trip.departure_time, trip.arrival_time, trip.trip_id))
    schedule.trips = trips
    schedule.parking_events = []
    if not trips:
        full_day = _event(
            0.0,
            HOURS_PER_DAY,
            terminus_dwell_purpose,
            can_charge=True,
            charge_power_kw=terminus_charge_kw,
        )
        if full_day is not None:
            schedule.parking_events.append(full_day)
        return

    first = trips[0]
    pre_event = _event(
        max(0.0, float(first.departure_time) - float(pre_journey_dwell_h)),
        float(first.departure_time),
        terminus_dwell_purpose,
        can_charge=True,
        charge_power_kw=terminus_charge_kw,
        location_lsoa=getattr(first, "origin_lsoa", ""),
    )
    if pre_event is not None:
        schedule.parking_events.append(pre_event)

    for left, right in zip(trips[:-1], trips[1:]):
        duration_h = float(right.departure_time) - float(left.arrival_time)
        location_lsoa = str(getattr(left, "destination_lsoa", "") or "")
        can_charge = bool(
            allow_layover_charging
            and duration_h >= float(min_layover_for_charging_h)
        )
        dwell = _event(
            float(left.arrival_time),
            float(right.departure_time),
            "layover",
            can_charge=can_charge,
            charge_power_kw=layover_charge_kw,
            location_lsoa=location_lsoa,
        )
        if dwell is not None:
            schedule.parking_events.append(dwell)

    last = trips[-1]
    post_event = _event(
        float(last.arrival_time),
        HOURS_PER_DAY,
        terminus_dwell_purpose,
        can_charge=True,
        charge_power_kw=terminus_charge_kw,
        location_lsoa=getattr(last, "destination_lsoa", ""),
    )
    if post_event is not None:
        schedule.parking_events.append(post_event)


def chain_to_year_schedules(
    chain_journeys: pd.DataFrame,
    active_dates: Iterable[dt.date],
    *,
    pre_journey_dwell_h: float = 6.0,
    terminus_dwell_purpose: str = "depot_terminus",
    consumption_kwh_per_km: float | None = None,
    terminus_charge_kw: float = 50.0,
    allow_layover_charging: bool = False,
    layover_charge_kw: float = 0.0,
    min_layover_for_charging_h: float = 0.0,
) -> list[DailySchedule]:
    """Expand one synthetic coach chain across the coach feed year."""
    if pre_journey_dwell_h < 0.0:
        raise ValueError("pre_journey_dwell_h must be non-negative.")
    if terminus_charge_kw < 0.0 or layover_charge_kw < 0.0:
        raise ValueError("charge powers must be non-negative.")
    if chain_journeys.empty:
        raise ValueError("chain_journeys must contain at least one journey.")

    dates = annual_dates()
    date_to_index = {date: index for index, date in enumerate(dates)}
    chain_id = _chain_id(chain_journeys)
    schedules = {
        date: DailySchedule(ev_id=chain_id, day=index, day_type=date.strftime("%A").lower(), date=date)
        for date, index in date_to_index.items()
    }
    active_date_set = {_coerce_date(value) for value in active_dates}
    ordered = _ordered_journeys(chain_journeys)

    empty_stops = pd.DataFrame(columns=["stop_sequence", "stop_point_ref"])
    for service_date in sorted(active_date_set):
        if service_date not in date_to_index:
            continue
        for _, row in ordered.iterrows():
            row_consumption = _consumption_for_row(row, consumption_kwh_per_km)
            daily_segments = journey_to_daily_schedules(
                row,
                empty_stops,
                consumption_kwh_per_km=row_consumption,
                terminus_charge_kw=terminus_charge_kw,
                pre_journey_dwell_h=pre_journey_dwell_h,
            )
            for segment_schedule in daily_segments:
                target_date = service_date + dt.timedelta(days=int(segment_schedule.day))
                if target_date not in schedules:
                    continue
                target = schedules[target_date]
                for trip in segment_schedule.trips:
                    stamped = copy.copy(trip)
                    stamped.trip_id = f"{trip.trip_id}__{service_date.isoformat()}__d{int(segment_schedule.day)}"
                    stamped.service_date = service_date
                    stamped.schedule_date = target_date
                    target.trips.append(stamped)

    for schedule in schedules.values():
        _attach_chain_parking(
            schedule,
            pre_journey_dwell_h=pre_journey_dwell_h,
            terminus_charge_kw=terminus_charge_kw,
            terminus_dwell_purpose=terminus_dwell_purpose,
            allow_layover_charging=allow_layover_charging,
            layover_charge_kw=layover_charge_kw,
            min_layover_for_charging_h=min_layover_for_charging_h,
        )
        schedule.metadata = {
            "chain_id": chain_id,
            "date": schedule.date.isoformat() if schedule.date is not None else "",
            "is_active": bool(schedule.trips),
            "n_trips": int(len(schedule.trips)),
        }

    return [schedules[date] for date in dates]


__all__ = ["annual_dates", "chain_to_year_schedules"]

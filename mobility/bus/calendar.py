"""GTFS service-calendar helpers for annual bus simulation."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_GTFS_DIR = PROJECT_ROOT / "Data" / "EV_behavior" / "Bus_Data" / "GTFS_timetable"
FEED_YEAR_START = dt.date(2026, 4, 17)
FEED_YEAR_END = dt.date(2027, 4, 16)
WEEKDAY_COLUMNS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


@dataclass(frozen=True)
class ServiceCalendar:
    """Loaded GTFS service calendar tables."""

    calendar: pd.DataFrame
    calendar_dates: pd.DataFrame
    gtfs_dir: Path


def _coerce_date(value: str | dt.date | pd.Timestamp) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, pd.Timestamp):
        return value.date()
    text = str(value)
    fmt = "%Y%m%d" if text.isdigit() and len(text) == 8 else "%Y-%m-%d"
    return dt.datetime.strptime(text, fmt).date()


def _date_range(start_date: dt.date, end_date: dt.date) -> list[dt.date]:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date.")
    days = (end_date - start_date).days + 1
    return [start_date + dt.timedelta(days=offset) for offset in range(days)]


def _normalise_calendar(raw: pd.DataFrame) -> pd.DataFrame:
    data = raw.copy()
    data["service_id"] = data["service_id"].astype(str)
    data["start_date_dt"] = data["start_date"].map(_coerce_date)
    data["end_date_dt"] = data["end_date"].map(_coerce_date)
    for column in WEEKDAY_COLUMNS:
        data[column] = pd.to_numeric(data[column], errors="coerce").fillna(0).astype(int)
    return data


def _normalise_calendar_dates(raw: pd.DataFrame) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame(columns=["service_id", "date", "exception_type", "date_dt"])
    data = raw.copy()
    data["service_id"] = data["service_id"].astype(str)
    data["date_dt"] = data["date"].map(_coerce_date)
    data["exception_type"] = pd.to_numeric(data["exception_type"], errors="coerce").astype(int)
    return data


def load_service_calendar(gtfs_dir: str | Path = DEFAULT_GTFS_DIR) -> ServiceCalendar:
    """Load GTFS ``calendar.txt`` and ``calendar_dates.txt``."""
    gtfs_path = Path(gtfs_dir)
    calendar_path = gtfs_path / "calendar.txt"
    calendar_dates_path = gtfs_path / "calendar_dates.txt"
    if not calendar_path.exists():
        raise FileNotFoundError(f"Missing GTFS calendar file: {calendar_path}")
    calendar = _normalise_calendar(pd.read_csv(calendar_path, dtype={"service_id": "string"}))
    if calendar_dates_path.exists():
        calendar_dates = pd.read_csv(calendar_dates_path, dtype={"service_id": "string", "date": "string"})
    else:
        calendar_dates = pd.DataFrame(columns=["service_id", "date", "exception_type"])
    return ServiceCalendar(
        calendar=calendar,
        calendar_dates=_normalise_calendar_dates(calendar_dates),
        gtfs_dir=gtfs_path,
    )


def active_dates_for_service(
    service_id: str | int,
    start_date: str | dt.date | pd.Timestamp,
    end_date: str | dt.date | pd.Timestamp,
    calendar: ServiceCalendar,
) -> tuple[dt.date, ...]:
    """Return active dates for one GTFS service ID using standard exception rules."""
    sid = str(service_id)
    start = _coerce_date(start_date)
    end = _coerce_date(end_date)
    service_rows = calendar.calendar[calendar.calendar["service_id"].eq(sid)]
    exceptions = calendar.calendar_dates[
        calendar.calendar_dates["service_id"].eq(sid)
        & calendar.calendar_dates["date_dt"].between(start, end)
    ]
    return _active_dates_from_rows(service_rows, exceptions, start, end)


def _active_dates_from_rows(
    service_rows: pd.DataFrame,
    exceptions: pd.DataFrame,
    start: dt.date,
    end: dt.date,
) -> tuple[dt.date, ...]:
    active: set[dt.date] = set()
    for row in service_rows.itertuples(index=False):
        row_start = max(start, row.start_date_dt)
        row_end = min(end, row.end_date_dt)
        if row_end < row_start:
            continue
        for day in _date_range(row_start, row_end):
            if getattr(row, WEEKDAY_COLUMNS[day.weekday()]) == 1:
                active.add(day)

    for row in exceptions.itertuples(index=False):
        if int(row.exception_type) == 1:
            active.add(row.date_dt)
        elif int(row.exception_type) == 2:
            active.discard(row.date_dt)
    return tuple(sorted(active))


def build_service_date_index(
    service_ids: Iterable[str | int],
    start_date: str | dt.date | pd.Timestamp = FEED_YEAR_START,
    end_date: str | dt.date | pd.Timestamp = FEED_YEAR_END,
    calendar: ServiceCalendar | None = None,
) -> dict[str, tuple[dt.date, ...]]:
    """Build a service_id -> active dates mapping for a simulation window."""
    service_calendar = load_service_calendar() if calendar is None else calendar
    unique_service_ids = sorted({str(service_id) for service_id in service_ids})
    start = _coerce_date(start_date)
    end = _coerce_date(end_date)
    calendar_groups = {
        service_id: frame
        for service_id, frame in service_calendar.calendar.groupby("service_id", sort=False)
    }
    exception_groups = {
        service_id: frame[frame["date_dt"].between(start, end)]
        for service_id, frame in service_calendar.calendar_dates.groupby("service_id", sort=False)
    }
    return {
        service_id: _active_dates_from_rows(
            calendar_groups.get(service_id, service_calendar.calendar.iloc[0:0]),
            exception_groups.get(service_id, service_calendar.calendar_dates.iloc[0:0]),
            start,
            end,
        )
        for service_id in unique_service_ids
    }

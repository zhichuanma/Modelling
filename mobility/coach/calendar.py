"""TransXChange operating-profile helpers for annual coach simulation."""

from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable

import pandas as pd

from mobility.bus.calendar import FEED_YEAR_END as _BUS_FEED_YEAR_END
from mobility.bus.calendar import FEED_YEAR_START as _BUS_FEED_YEAR_START
from mobility.cars.holiday_rules import UK_BANK_HOLIDAYS_2025_2026
from mobility.core.txc_parser import TXC_NS, _findtext, _local_name


ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_COACH_ROOT = PROJECT_ROOT / "Data" / "EV_behavior" / "Coach_Data" / "TxC-2.4"
DEFAULT_INVENTORY_PATH = DEFAULT_COACH_ROOT / "TxCInventory17APR26.csv"

WEEKDAY_TAGS = {
    "Monday": 0,
    "Tuesday": 1,
    "Wednesday": 2,
    "Thursday": 3,
    "Friday": 4,
    "Saturday": 5,
    "Sunday": 6,
}
WEEKDAY_GROUPS = {
    "MondayToFriday": {0, 1, 2, 3, 4},
    "MondayToSaturday": {0, 1, 2, 3, 4, 5},
    "Weekend": {5, 6},
    "Everyday": {0, 1, 2, 3, 4, 5, 6},
    "Daily": {0, 1, 2, 3, 4, 5, 6},
}
PROFILE_SOURCE_TXC = "txc"
PROFILE_SOURCE_FALLBACK = "fallback_uniform"


def _coerce_date(value: object) -> dt.date | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        try:
            return dt.datetime.strptime(text, "%Y%m%d").date()
        except ValueError:
            return None


def _date_range(start_date: dt.date, end_date: dt.date) -> list[dt.date]:
    if end_date < start_date:
        return []
    return [start_date + dt.timedelta(days=offset) for offset in range((end_date - start_date).days + 1)]


def _default_feed_year_bounds() -> tuple[dt.date, dt.date]:
    """Infer the full-year coach feed window, falling back to the bus feed year.

    The TxC inventory includes special-event services whose operating periods
    are much shorter than a feed year, so the default constant uses year-like
    service periods when they are available.
    """
    try:
        inventory = pd.read_csv(
            DEFAULT_INVENTORY_PATH,
            usecols=["ServiceStartDate", "ServiceEndDate"],
        )
    except (FileNotFoundError, ValueError, pd.errors.EmptyDataError):
        return _BUS_FEED_YEAR_START, _BUS_FEED_YEAR_END

    starts = inventory["ServiceStartDate"].map(_coerce_date)
    ends = inventory["ServiceEndDate"].map(_coerce_date)
    periods = pd.DataFrame({"start": starts, "end": ends}).dropna()
    if periods.empty:
        return _BUS_FEED_YEAR_START, _BUS_FEED_YEAR_END

    periods["span_days"] = [
        (end - start).days
        for start, end in zip(periods["start"], periods["end"])
    ]
    year_like = periods.loc[periods["span_days"].between(300, 370)]
    source = year_like if not year_like.empty else periods
    start = min(source["start"])
    end = max(source["end"])
    if (end - start).days >= 365:
        end = start + dt.timedelta(days=364)
    return start, end


COACH_FEED_YEAR_START, COACH_FEED_YEAR_END = _default_feed_year_bounds()


def _service_period(root: ET.Element) -> tuple[dt.date, dt.date]:
    period = root.find("./tx:Services/tx:Service/tx:OperatingPeriod", TXC_NS)
    start = _coerce_date(_findtext(period, "tx:StartDate") if period is not None else None)
    end = _coerce_date(_findtext(period, "tx:EndDate") if period is not None else None)
    if start is None:
        start = COACH_FEED_YEAR_START
    if end is None:
        end = COACH_FEED_YEAR_END
    return max(start, COACH_FEED_YEAR_START), min(end, COACH_FEED_YEAR_END)


def _profile_weekdays(profile: ET.Element) -> set[int]:
    weekdays: set[int] = set()
    regular = profile.find("tx:RegularDayType", TXC_NS)
    if regular is None:
        return weekdays
    for element in regular.iter():
        name = _local_name(element.tag)
        if name in WEEKDAY_TAGS:
            weekdays.add(WEEKDAY_TAGS[name])
        elif name in WEEKDAY_GROUPS:
            weekdays.update(WEEKDAY_GROUPS[name])
        elif name == "HolidaysOnly":
            weekdays.update(range(7))
    return weekdays


def _date_ranges(parent: ET.Element, xpath: str) -> list[tuple[dt.date, dt.date]]:
    ranges: list[tuple[dt.date, dt.date]] = []
    for elem in parent.findall(xpath, TXC_NS):
        start = _coerce_date(_findtext(elem, "tx:StartDate"))
        end = _coerce_date(_findtext(elem, "tx:EndDate"))
        if start is not None and end is not None:
            ranges.append((start, end))
    return ranges


def _has_bank_holiday_tag(parent: ET.Element, xpath: str) -> bool:
    elem = parent.find(xpath, TXC_NS)
    return elem is not None and len(list(elem.iter())) > 1


def _bank_holidays(start: dt.date, end: dt.date) -> set[dt.date]:
    holidays = set().union(*UK_BANK_HOLIDAYS_2025_2026.values())
    return {day for day in holidays if start <= day <= end}


def _active_dates_from_profile(
    profile: ET.Element | None,
    *,
    start: dt.date,
    end: dt.date,
) -> tuple[list[dt.date], str]:
    if profile is None or end < start:
        return _date_range(start, end), PROFILE_SOURCE_FALLBACK

    active: set[dt.date] = set()
    weekdays = _profile_weekdays(profile)
    for day in _date_range(start, end):
        if day.weekday() in weekdays:
            active.add(day)

    operation_ranges = _date_ranges(
        profile,
        ".//tx:SpecialDaysOperation/tx:DaysOfOperation/tx:DateRange",
    )
    for range_start, range_end in operation_ranges:
        for day in _date_range(max(start, range_start), min(end, range_end)):
            active.add(day)

    if _has_bank_holiday_tag(profile, ".//tx:SpecialDaysOperation/tx:DaysOfOperation/tx:BankHolidays"):
        active.update(_bank_holidays(start, end))

    non_operation_ranges = _date_ranges(
        profile,
        ".//tx:SpecialDaysOperation/tx:DaysOfNonOperation/tx:DateRange",
    )
    for range_start, range_end in non_operation_ranges:
        for day in _date_range(max(start, range_start), min(end, range_end)):
            active.discard(day)

    if _has_bank_holiday_tag(profile, ".//tx:SpecialDaysOperation/tx:DaysOfNonOperation/tx:BankHolidays"):
        active.difference_update(_bank_holidays(start, end))

    if not weekdays and not operation_ranges and not active:
        return _date_range(start, end), PROFILE_SOURCE_FALLBACK
    return sorted(active), PROFILE_SOURCE_TXC


def _parse_operating_profile_with_sources(xml_path: str | Path) -> tuple[dict[str, list[dt.date]], dict[str, str]]:
    xml_path = Path(xml_path)
    root = ET.parse(xml_path).getroot()
    start, end = _service_period(root)
    profiles: dict[str, list[dt.date]] = {}
    sources: dict[str, str] = {}
    for vehicle_journey in root.findall("./tx:VehicleJourneys/tx:VehicleJourney", TXC_NS):
        code = _findtext(vehicle_journey, "tx:VehicleJourneyCode")
        if not code:
            continue
        dates, source = _active_dates_from_profile(
            vehicle_journey.find("tx:OperatingProfile", TXC_NS),
            start=start,
            end=end,
        )
        profiles[str(code)] = dates
        sources[str(code)] = source
    return profiles, sources


def parse_operating_profile(xml_path: Path) -> dict[str, list[dt.date]]:
    """Return ``vehicle_journey_code -> active dates`` parsed from one TxC XML."""
    profiles, _sources = _parse_operating_profile_with_sources(xml_path)
    return profiles


def _resolve_xml_path(row: pd.Series, root: Path) -> Path:
    for column in ("xml_path", "FilePath", "file_name"):
        if column in row.index and pd.notna(row[column]) and str(row[column]).strip():
            candidate = Path(str(row[column]))
            if candidate.is_absolute():
                return candidate
            return root / candidate
    raise ValueError("journeys must include one of xml_path, FilePath, or file_name.")


def _uniform_dates() -> list[dt.date]:
    return _date_range(COACH_FEED_YEAR_START, COACH_FEED_YEAR_END)


def build_journey_date_index(journeys: pd.DataFrame, root: Path) -> pd.DataFrame:
    """Build a long ``(journey_id, date)`` operating-date index for coach journeys."""
    required = {"journey_id", "vehicle_journey_code"}
    missing = required - set(journeys.columns)
    if missing:
        raise ValueError(f"journeys is missing required columns: {sorted(missing)}")
    if journeys.empty:
        return pd.DataFrame(columns=["journey_id", "date", "profile_source"])

    root = Path(root)
    records: list[dict[str, object]] = []
    profile_cache: dict[Path, tuple[dict[str, list[dt.date]], dict[str, str]]] = {}
    for _, row in journeys.iterrows():
        journey_id = str(row["journey_id"])
        code = str(row["vehicle_journey_code"])
        xml_path = _resolve_xml_path(row, root)
        if xml_path not in profile_cache:
            if xml_path.exists():
                profile_cache[xml_path] = _parse_operating_profile_with_sources(xml_path)
            else:
                profile_cache[xml_path] = ({}, {})
        profiles, sources = profile_cache[xml_path]
        dates = profiles.get(code)
        source = sources.get(code, PROFILE_SOURCE_FALLBACK)
        if dates is None:
            dates = _uniform_dates()
            source = PROFILE_SOURCE_FALLBACK
        for active_date in dates:
            if COACH_FEED_YEAR_START <= active_date <= COACH_FEED_YEAR_END:
                records.append(
                    {
                        "journey_id": journey_id,
                        "date": active_date,
                        "profile_source": source,
                    }
                )
    out = pd.DataFrame.from_records(records, columns=["journey_id", "date", "profile_source"])
    if out.empty:
        return out
    out["profile_source"] = out["profile_source"].where(
        out["profile_source"].isin({PROFILE_SOURCE_TXC, PROFILE_SOURCE_FALLBACK}),
        PROFILE_SOURCE_FALLBACK,
    )
    return out.sort_values(["journey_id", "date"], kind="stable").reset_index(drop=True)


__all__ = [
    "COACH_FEED_YEAR_END",
    "COACH_FEED_YEAR_START",
    "PROFILE_SOURCE_FALLBACK",
    "PROFILE_SOURCE_TXC",
    "build_journey_date_index",
    "parse_operating_profile",
]

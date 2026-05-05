from __future__ import annotations

import pandas as pd

from mobility.bus.calendar import active_dates_for_service, load_service_calendar


def _write_calendar(tmp_path, *, write_exceptions: bool = True):
    gtfs = tmp_path / "gtfs"
    gtfs.mkdir()
    pd.DataFrame(
        [
            (7, 0, 0, 0, 0, 0, 0, 1, 20270228, 20270228),
            (8, 0, 0, 0, 0, 0, 0, 0, 20270228, 20270302),
        ],
        columns=[
            "service_id",
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
            "start_date",
            "end_date",
        ],
    ).to_csv(gtfs / "calendar.txt", index=False)
    if write_exceptions:
        pd.DataFrame(
            [(8, 20270301, 1)],
            columns=["service_id", "date", "exception_type"],
        ).to_csv(gtfs / "calendar_dates.txt", index=False)
    return gtfs


def test_calendar_numeric_service_id_and_feed_year_february_edge(tmp_path) -> None:
    calendar = load_service_calendar(_write_calendar(tmp_path))

    dates = active_dates_for_service(7, "2027-02-28", "2027-02-28", calendar)

    assert [date.isoformat() for date in dates] == ["2027-02-28"]


def test_calendar_dates_exception_adds_service_date(tmp_path) -> None:
    calendar = load_service_calendar(_write_calendar(tmp_path))

    dates = active_dates_for_service("8", "2027-02-28", "2027-03-02", calendar)

    assert [date.isoformat() for date in dates] == ["2027-03-01"]


def test_missing_calendar_dates_file_is_empty_exceptions_table(tmp_path) -> None:
    calendar = load_service_calendar(_write_calendar(tmp_path, write_exceptions=False))

    assert calendar.calendar_dates.empty
    assert list(calendar.calendar_dates.columns) == ["service_id", "date", "exception_type", "date_dt"]

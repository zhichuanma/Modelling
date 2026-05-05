from __future__ import annotations

import pandas as pd

from mobility.bus.calendar import (
    active_dates_for_service,
    build_service_date_index,
    load_service_calendar,
)


def _write_calendar(tmp_path):
    gtfs = tmp_path / "gtfs"
    gtfs.mkdir()
    pd.DataFrame(
        [
            ("S1", 1, 1, 0, 0, 0, 0, 0, 20260417, 20260430),
            ("S2", 0, 0, 0, 0, 0, 1, 0, 20260417, 20260430),
        ],
        columns=[
            "service_id", "monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday", "start_date", "end_date",
        ],
    ).to_csv(gtfs / "calendar.txt", index=False)
    pd.DataFrame(
        [
            ("S1", 20260420, 2),
            ("S1", 20260422, 1),
        ],
        columns=["service_id", "date", "exception_type"],
    ).to_csv(gtfs / "calendar_dates.txt", index=False)
    return gtfs


def test_gtfs_calendar_weekday_and_exceptions(tmp_path) -> None:
    calendar = load_service_calendar(_write_calendar(tmp_path))

    dates = active_dates_for_service("S1", "2026-04-17", "2026-04-23", calendar)

    assert [date.isoformat() for date in dates] == ["2026-04-21", "2026-04-22"]


def test_gtfs_calendar_2025_has_no_current_feed_coverage() -> None:
    calendar = load_service_calendar()
    index = build_service_date_index(["56"], "2025-01-01", "2025-12-31", calendar)

    assert index["56"] == ()

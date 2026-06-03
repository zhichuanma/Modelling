from __future__ import annotations

import pandas as pd

from mobility.bus.annual_block_instances import expand_block_instances
from mobility.bus.calendar import ServiceCalendar


def _templates() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "block_template_id": ["bt1"],
            "agency_id": ["OP"],
            "service_id": ["S1"],
            "block_id": ["B1"],
            "block_source": ["native"],
            "start_h": [23.5],
            "end_h": [25.0],
            "duration_h": [1.5],
            "passenger_distance_km": [20.0],
            "start_stop": ["A"],
            "end_stop": ["B"],
            "start_lat": [51.0],
            "start_lon": [-1.0],
            "end_lat": [51.1],
            "end_lon": [-1.1],
            "start_lsoa": ["E0101"],
            "end_lsoa": ["E0101"],
            "region_key": ["London"],
        }
    )


def _calendar() -> ServiceCalendar:
    calendar = pd.DataFrame(
        {
            "service_id": ["S1"],
            "monday": [0],
            "tuesday": [0],
            "wednesday": [0],
            "thursday": [0],
            "friday": [1],
            "saturday": [0],
            "sunday": [0],
            "start_date": [20260417],
            "end_date": [20260419],
        }
    )
    from mobility.bus.calendar import _normalise_calendar, _normalise_calendar_dates

    exceptions = pd.DataFrame({"service_id": ["S1", "S1"], "date": [20260418, 20260417], "exception_type": [1, 2]})
    return ServiceCalendar(_normalise_calendar(calendar), _normalise_calendar_dates(exceptions), gtfs_dir=pd.Path if False else None)


def test_calendar_dates_adds_service() -> None:
    instances, _ = expand_block_instances(_templates(), start_date="2026-04-17", end_date="2026-04-19", calendar=_calendar())
    assert "2026-04-18" in set(instances["service_date"])


def test_calendar_dates_removes_service() -> None:
    instances, _ = expand_block_instances(_templates(), start_date="2026-04-17", end_date="2026-04-19", calendar=_calendar())
    assert "2026-04-17" not in set(instances["service_date"])


def test_block_instance_ids_are_unique() -> None:
    instances, _ = expand_block_instances(_templates(), service_date_index={"S1": [pd.Timestamp("2026-04-17").date()]})
    assert instances["block_instance_id"].is_unique


def test_cross_midnight_instances_keep_order() -> None:
    instances, _ = expand_block_instances(_templates(), service_date_index={"S1": [pd.Timestamp("2026-04-17").date()]})
    assert instances.loc[0, "end_datetime"] > instances.loc[0, "start_datetime"]
    assert instances.loc[0, "end_datetime"].strftime("%Y-%m-%d") == "2026-04-18"


def test_service_date_range_respected() -> None:
    instances, _ = expand_block_instances(
        _templates(),
        start_date="2026-04-18",
        end_date="2026-04-18",
        service_date_index={"S1": [pd.Timestamp("2026-04-17").date(), pd.Timestamp("2026-04-18").date()]},
    )
    assert set(instances["service_date"]) == {"2026-04-18"}

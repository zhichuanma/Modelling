"""Expand block templates into active annual block instances."""

from __future__ import annotations

import datetime as dt
from typing import Any

import numpy as np
import pandas as pd

from .calendar import FEED_YEAR_END, FEED_YEAR_START, ServiceCalendar, build_service_date_index


def coerce_date(value: str | dt.date | pd.Timestamp) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, pd.Timestamp):
        return value.date()
    return dt.date.fromisoformat(str(value))


def datetime_from_service_hour(service_date: str | dt.date | pd.Timestamp, hour: float) -> pd.Timestamp:
    base = pd.Timestamp(coerce_date(service_date))
    seconds = int(round(float(hour) * 3600.0))
    return base + pd.to_timedelta(seconds, unit="s")


def expand_block_instances(
    block_templates_lsoa: pd.DataFrame,
    *,
    start_date: str | dt.date | pd.Timestamp = FEED_YEAR_START,
    end_date: str | dt.date | pd.Timestamp = FEED_YEAR_END,
    calendar: ServiceCalendar | None = None,
    service_date_index: dict[str, tuple[dt.date, ...] | list[dt.date]] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Expand templates to active service dates using GTFS calendar rules."""
    if block_templates_lsoa.empty:
        diagnostics = pd.DataFrame([_diagnostics_record(block_templates_lsoa, pd.DataFrame(), start_date, end_date)])
        return pd.DataFrame(), diagnostics

    required = {
        "block_template_id",
        "agency_id",
        "service_id",
        "block_id",
        "block_source",
        "start_h",
        "end_h",
        "duration_h",
        "passenger_distance_km",
        "start_stop",
        "end_stop",
        "start_lat",
        "start_lon",
        "end_lat",
        "end_lon",
    }
    missing = sorted(required - set(block_templates_lsoa.columns))
    if missing:
        raise ValueError(f"block_templates_lsoa is missing required columns: {missing}")

    service_ids = block_templates_lsoa["service_id"].dropna().astype(str).unique()
    # ASSUMPTION: GTFS calendar_dates.txt correctly reflects bank-holiday service.
    # No separate holiday scenario is modelled in this pipeline.
    active_by_service = service_date_index or build_service_date_index(
        service_ids,
        start_date=start_date,
        end_date=end_date,
        calendar=calendar,
    )
    start = coerce_date(start_date)
    end = coerce_date(end_date)

    records: list[dict[str, Any]] = []
    pass_columns = [
        "block_template_id",
        "agency_id",
        "service_id",
        "block_id",
        "block_source",
        "start_h",
        "end_h",
        "duration_h",
        "passenger_distance_km",
        "start_stop",
        "end_stop",
        "start_lat",
        "start_lon",
        "end_lat",
        "end_lon",
        "start_lsoa",
        "end_lsoa",
        "region_key",
    ]
    optional = [col for col in pass_columns if col in block_templates_lsoa.columns]
    for row in block_templates_lsoa.itertuples(index=False):
        row_dict = row._asdict()
        service_id = str(row_dict["service_id"])
        active_dates = [
            coerce_date(value)
            for value in active_by_service.get(service_id, ())
            if start <= coerce_date(value) <= end
        ]
        for service_date in active_dates:
            record = {col: row_dict.get(col) for col in optional}
            record["service_date"] = coerce_date(service_date).isoformat()
            record["start_datetime"] = datetime_from_service_hour(service_date, float(row_dict["start_h"]))
            record["end_datetime"] = datetime_from_service_hour(service_date, float(row_dict["end_h"]))
            records.append(record)

    instances = pd.DataFrame.from_records(records)
    if not instances.empty:
        instances = instances.sort_values(["service_date", "block_template_id", "start_datetime"], kind="stable").reset_index(drop=True)
        seq = instances.groupby(["service_date", "block_template_id"], sort=False).cumcount()
        instances.insert(
            1,
            "block_instance_id",
            [
                f"{service_date}_{template_id}_{index:02d}"
                for service_date, template_id, index in zip(instances["service_date"], instances["block_template_id"], seq)
            ],
        )
        ordered_cols = [
            "service_date",
            "block_instance_id",
            "block_template_id",
            "agency_id",
            "service_id",
            "block_id",
            "block_source",
            "start_datetime",
            "end_datetime",
            "start_h",
            "end_h",
            "duration_h",
            "passenger_distance_km",
            "start_stop",
            "end_stop",
            "start_lat",
            "start_lon",
            "end_lat",
            "end_lon",
            "start_lsoa",
            "end_lsoa",
            "region_key",
        ]
        instances = instances.loc[:, [col for col in ordered_cols if col in instances.columns]]

    diagnostics = pd.DataFrame([_diagnostics_record(block_templates_lsoa, instances, start_date, end_date)])
    return instances, diagnostics


def _diagnostics_record(
    templates: pd.DataFrame,
    instances: pd.DataFrame,
    start_date: str | dt.date | pd.Timestamp,
    end_date: str | dt.date | pd.Timestamp,
) -> dict[str, Any]:
    active_templates = int(instances["block_template_id"].nunique()) if not instances.empty else 0
    return {
        "feed_year_start": coerce_date(start_date).isoformat(),
        "feed_year_end": coerce_date(end_date).isoformat(),
        "n_block_templates": int(len(templates)),
        "n_active_block_templates": active_templates,
        "n_block_instances_annual": int(len(instances)),
        "n_templates_with_no_active_dates": int(len(templates) - active_templates),
        "block_instance_ids_unique": bool(instances["block_instance_id"].is_unique) if not instances.empty else True,
    }


__all__ = ["coerce_date", "datetime_from_service_hour", "expand_block_instances"]

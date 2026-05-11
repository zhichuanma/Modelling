"""Expand GTFS block templates into dated block instances."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd

from .calendar import (
    FEED_YEAR_END,
    FEED_YEAR_START,
    ServiceCalendar,
    build_service_date_index,
)


BLOCK_INSTANCE_COLUMNS = [
    "service_date",
    "agency_id",
    "service_id",
    "block_id",
    "seq",
    "block_instance_id",
    "block_source",
    "start_time",
    "end_time",
    "start_stop",
    "end_stop",
    "start_lat",
    "start_lon",
    "end_lat",
    "end_lon",
    "passenger_distance_km",
    "n_trips",
]


def _coerce_date(value: str | dt.date | pd.Timestamp) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, pd.Timestamp):
        return value.date()
    return dt.date.fromisoformat(str(value))


def _template_rows(blocks_df: pd.DataFrame) -> pd.DataFrame:
    required = {
        "agency_id",
        "service_id",
        "block_id",
        "block_source",
        "start_h",
        "end_h",
        "start_stop",
        "end_stop",
        "start_lat",
        "start_lon",
        "end_lat",
        "end_lon",
        "distance_km",
    }
    missing = required - set(blocks_df.columns)
    if missing:
        raise ValueError(f"blocks_df is missing required columns: {sorted(missing)}")
    data = blocks_df.copy()
    data["service_id"] = data["service_id"].astype(str)
    data["agency_id"] = data["agency_id"].astype(str)
    data["block_id"] = data["block_id"].astype(str)
    data = data.sort_values(["agency_id", "service_id", "block_id", "start_h", "end_h"], kind="stable")
    rows: list[dict] = []
    for (agency_id, service_id, block_id), group in data.groupby(
        ["agency_id", "service_id", "block_id"],
        sort=False,
    ):
        ordered = group.sort_values(["start_h", "end_h"], kind="stable")
        first = ordered.iloc[0]
        last = ordered.iloc[-1]
        rows.append(
            {
                "agency_id": str(agency_id),
                "service_id": str(service_id),
                "block_id": str(block_id),
                "block_source": str(first["block_source"]),
                "start_time": float(first["start_h"]) * 60.0,
                "end_time": float(last["end_h"]) * 60.0,
                "start_stop": str(first["start_stop"]),
                "end_stop": str(last["end_stop"]),
                "start_lat": float(first["start_lat"]),
                "start_lon": float(first["start_lon"]),
                "end_lat": float(last["end_lat"]),
                "end_lon": float(last["end_lon"]),
                "passenger_distance_km": float(ordered["distance_km"].sum()),
                "n_trips": int(len(ordered)),
            }
        )
    return pd.DataFrame(rows)


def build_block_templates(blocks_df: pd.DataFrame) -> pd.DataFrame:
    """Collapse trip rows to one row per service/block template."""
    if blocks_df is None or blocks_df.empty:
        return pd.DataFrame(
            columns=[
                "agency_id",
                "service_id",
                "block_id",
                "block_source",
                "start_time",
                "end_time",
                "start_stop",
                "end_stop",
                "start_lat",
                "start_lon",
                "end_lat",
                "end_lon",
                "passenger_distance_km",
                "n_trips",
            ]
        )
    return _template_rows(blocks_df)


def build_block_instances_from_templates(
    templates: pd.DataFrame,
    service_date_index: dict[str, tuple[dt.date, ...] | list[dt.date]] | None = None,
    *,
    start_date: str | dt.date | pd.Timestamp = FEED_YEAR_START,
    end_date: str | dt.date | pd.Timestamp = FEED_YEAR_END,
    calendar: ServiceCalendar | None = None,
) -> pd.DataFrame:
    """Expand pre-collapsed block templates into dated block instances."""
    if templates is None or templates.empty:
        return pd.DataFrame(columns=BLOCK_INSTANCE_COLUMNS)
    if service_date_index is None:
        service_date_index = build_service_date_index(
            templates["service_id"].unique(),
            start_date=start_date,
            end_date=end_date,
            calendar=calendar,
        )
    start = _coerce_date(start_date)
    end = _coerce_date(end_date)
    rows: list[dict] = []
    for row in templates.itertuples(index=False):
        active_dates = service_date_index.get(str(row.service_id), ())
        for date_value in active_dates:
            service_date = _coerce_date(date_value)
            if service_date < start or service_date > end:
                continue
            rows.append(
                {
                    "service_date": service_date.isoformat(),
                    "agency_id": row.agency_id,
                    "service_id": row.service_id,
                    "block_id": row.block_id,
                    "seq": 0,
                    "block_instance_id": "",
                    "block_source": row.block_source,
                    "start_time": row.start_time,
                    "end_time": row.end_time,
                    "start_stop": row.start_stop,
                    "end_stop": row.end_stop,
                    "start_lat": row.start_lat,
                    "start_lon": row.start_lon,
                    "end_lat": row.end_lat,
                    "end_lon": row.end_lon,
                    "passenger_distance_km": row.passenger_distance_km,
                    "n_trips": row.n_trips,
                }
            )
    if not rows:
        return pd.DataFrame(columns=BLOCK_INSTANCE_COLUMNS)
    out = pd.DataFrame(rows)
    out = out.sort_values(
        ["service_date", "block_id", "start_time", "agency_id", "service_id"],
        kind="stable",
    ).reset_index(drop=True)
    out["seq"] = out.groupby(["service_date", "block_id"], sort=False).cumcount() + 1
    out["block_instance_id"] = [
        f"{service_date}_{block_id}_{seq:02d}"
        for service_date, block_id, seq in zip(out["service_date"], out["block_id"], out["seq"])
    ]
    return out.loc[:, BLOCK_INSTANCE_COLUMNS].reset_index(drop=True)


def build_block_instances(
    blocks_df: pd.DataFrame,
    service_date_index: dict[str, tuple[dt.date, ...] | list[dt.date]] | None = None,
    *,
    start_date: str | dt.date | pd.Timestamp = FEED_YEAR_START,
    end_date: str | dt.date | pd.Timestamp = FEED_YEAR_END,
    calendar: ServiceCalendar | None = None,
) -> pd.DataFrame:
    """Expand each active block template into dated block instances.

    ``block_instance_id`` is ``{service_date}_{block_id}_{seq:02d}``; the seq
    suffix is assigned per service date and block ID after calendar expansion.
    """
    if blocks_df is None or blocks_df.empty:
        return pd.DataFrame(columns=BLOCK_INSTANCE_COLUMNS)
    templates = _template_rows(blocks_df)
    return build_block_instances_from_templates(
        templates,
        service_date_index,
        start_date=start_date,
        end_date=end_date,
        calendar=calendar,
    )

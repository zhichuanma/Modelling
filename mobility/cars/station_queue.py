"""Queue-aware public charging station post-processing for private cars.

This module is deliberately downstream of the private-car demand generator. It
treats the existing public charging sessions as requested demand, then schedules
that demand through a deterministic finite-connector FCFS queue per station.
It does not change trip generation, station matching, SOC equations, or the
uncontrolled baseline outputs.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Mapping

import numpy as np
import pandas as pd

from mobility.core.simulator import STEP_HOURS


QUEUE_MODEL_NAME = "deterministic_fcfs_finite_connectors"
QUEUE_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class QueueModelConfig:
    """Configuration for the deterministic station queue model."""

    time_resolution_minutes: int = 15
    fallback_connector_power_kw: float = 7.0
    fallback_connector_count: int | None = None
    allow_service_after_window: bool = False
    max_delay_min: float | None = 0.0
    availability_model: str = "deterministic"
    energy_epsilon_kwh: float = 1e-9

    def validate(self) -> None:
        if self.time_resolution_minutes <= 0:
            raise ValueError("time_resolution_minutes must be positive.")
        if self.fallback_connector_power_kw <= 0.0:
            raise ValueError("fallback_connector_power_kw must be positive.")
        if self.fallback_connector_count is not None and self.fallback_connector_count <= 0:
            raise ValueError("fallback_connector_count must be positive when supplied.")
        if self.max_delay_min is not None and self.max_delay_min < 0.0:
            raise ValueError("max_delay_min must be non-negative or None.")
        if self.availability_model != "deterministic":
            raise ValueError("Only availability_model='deterministic' is currently implemented.")
        if self.energy_epsilon_kwh < 0.0:
            raise ValueError("energy_epsilon_kwh must be non-negative.")


CAPACITY_COLUMNS = [
    "station_id",
    "connector_count",
    "connector_power_kw",
    "station_capacity_kw",
    "capacity_source",
]

SESSION_DEMAND_COLUMNS = [
    "session_id",
    "vehicle_id",
    "station_id",
    "arrival_time",
    "window_end_time",
    "requested_energy_kwh",
    "requested_power_kw",
]

QUEUE_SESSION_COLUMNS = [
    *SESSION_DEMAND_COLUMNS,
    "connector_id",
    "connector_count",
    "connector_power_kw",
    "station_capacity_kw",
    "capacity_source",
    "queue_length_on_arrival",
    "first_available_connector_time",
    "queue_wait_end_time",
    "scheduled_service_start_time",
    "scheduled_service_end_time",
    "waiting_time_min",
    "service_duration_min",
    "delivered_energy_after_queue_kwh",
    "unmet_energy_kwh",
    "delayed",
    "rejected",
    "unmet",
    "queue_status",
]

QUEUE_CURVE_15MIN_COLUMNS = [
    "station_id",
    "time_bin_start",
    "time_bin_end",
    "date",
    "energy_kwh",
    "avg_power_kw",
    "active_vehicle_count",
    "charging_session_count",
    "occupied_connector_count",
    "occupied_connector_time_h",
    "queued_session_count",
    "waiting_session_time_h",
    "connector_count",
    "station_capacity_kw",
    "bin_utilization_rate",
]

QUEUE_CURVE_HOURLY_COLUMNS = [
    "station_id",
    "hour_start_time",
    "hour_end_time",
    "date",
    "energy_kwh",
    "avg_power_kw",
    "peak_active_vehicle_count",
    "peak_queued_session_count",
    "mean_bin_utilization_rate",
    "connector_count",
    "station_capacity_kw",
]

QUEUE_SUMMARY_COLUMNS = [
    "station_id",
    "session_count",
    "delayed_session_count",
    "rejected_session_count",
    "unmet_session_count",
    "requested_energy_kwh",
    "delivered_energy_after_queue_kwh",
    "unmet_energy_kwh",
    "rejected_energy_kwh",
    "average_waiting_time_min",
    "median_waiting_time_min",
    "p95_waiting_time_min",
    "max_waiting_time_min",
    "average_queue_length_on_arrival",
    "max_queue_length",
    "service_duration_h",
    "connector_count",
    "connector_power_kw",
    "station_capacity_kw",
    "study_period_h",
    "station_utilization_rate",
    "capacity_source",
]

QUEUE_COMPARISON_COLUMNS = [
    "station_id",
    "baseline_energy_kwh",
    "queue_energy_kwh",
    "energy_delta_kwh",
    "unmet_energy_kwh",
    "baseline_peak_power_kw",
    "queue_peak_power_kw",
    "peak_power_delta_kw",
    "session_count",
    "delayed_session_count",
    "rejected_session_count",
    "unmet_session_count",
    "average_waiting_time_min",
    "median_waiting_time_min",
    "p95_waiting_time_min",
    "max_queue_length",
    "station_utilization_rate",
]


def build_public_session_demand_from_events(
    charging_events: pd.DataFrame,
    *,
    energy_epsilon_kwh: float = 1e-9,
) -> pd.DataFrame:
    """Build queue-model demand rows from existing private-car charging events."""

    if charging_events.empty:
        return pd.DataFrame(columns=SESSION_DEMAND_COLUMNS)
    frame = charging_events.copy()
    if "charging_type" in frame.columns:
        frame = frame.loc[frame["charging_type"] == "public_current_lsoa"].copy()
    if "station_id" not in frame.columns:
        return pd.DataFrame(columns=SESSION_DEMAND_COLUMNS)
    frame = frame.loc[frame["station_id"].notna()].copy()
    if frame.empty:
        return pd.DataFrame(columns=SESSION_DEMAND_COLUMNS)

    result = pd.DataFrame(
        {
            "session_id": _column_or_index(frame, "event_id"),
            "vehicle_id": _column_or_index(frame, "ev_id"),
            "station_id": frame["station_id"].astype(str),
            "arrival_time": pd.to_datetime(frame["charging_start_time"], errors="coerce"),
            "window_end_time": pd.to_datetime(frame["charging_end_time"], errors="coerce"),
            "requested_energy_kwh": pd.to_numeric(
                frame.get("charged_energy_kwh", 0.0), errors="coerce"
            ).fillna(0.0),
            "requested_power_kw": pd.to_numeric(
                frame.get("charging_power_kw", 0.0), errors="coerce"
            ).fillna(0.0),
        }
    )
    return _clean_session_demand(result, energy_epsilon_kwh=energy_epsilon_kwh)


def normalise_session_demand(
    sessions: pd.DataFrame,
    *,
    energy_epsilon_kwh: float = 1e-9,
) -> pd.DataFrame:
    """Normalize event/session rows to the queue demand schema."""

    if sessions.empty:
        return pd.DataFrame(columns=SESSION_DEMAND_COLUMNS)

    session_id_col = _first_existing_column(sessions, ["session_id", "event_id"])
    vehicle_col = _first_existing_column(sessions, ["vehicle_id", "ev_id"])
    station_col = _first_existing_column(sessions, ["station_id", "StationID"])
    arrival_col = _first_existing_column(
        sessions,
        ["arrival_time", "window_start_time", "charging_start_time"],
    )
    end_col = _first_existing_column(
        sessions,
        ["window_end_time", "charging_end_time"],
    )
    energy_col = _first_existing_column(
        sessions,
        ["requested_energy_kwh", "delivered_energy_kwh", "charged_energy_kwh"],
    )
    power_col = _first_existing_column(
        sessions,
        ["requested_power_kw", "charging_power_kw"],
    )
    missing = [
        name
        for name, value in {
            "station_id": station_col,
            "arrival_time": arrival_col,
            "window_end_time": end_col,
            "requested_energy_kwh": energy_col,
            "requested_power_kw": power_col,
        }.items()
        if value is None
    ]
    if missing:
        raise KeyError(f"Missing queue session columns: {missing}")

    result = pd.DataFrame(
        {
            "session_id": sessions[session_id_col].astype(str)
            if session_id_col is not None
            else pd.Series([f"session_{idx}" for idx in sessions.index], index=sessions.index),
            "vehicle_id": sessions[vehicle_col].astype(str)
            if vehicle_col is not None
            else pd.Series([""] * len(sessions), index=sessions.index),
            "station_id": sessions[station_col].astype(str),
            "arrival_time": pd.to_datetime(sessions[arrival_col], errors="coerce"),
            "window_end_time": pd.to_datetime(sessions[end_col], errors="coerce"),
            "requested_energy_kwh": pd.to_numeric(sessions[energy_col], errors="coerce").fillna(0.0),
            "requested_power_kw": pd.to_numeric(sessions[power_col], errors="coerce").fillna(0.0),
        }
    )
    return _clean_session_demand(result, energy_epsilon_kwh=energy_epsilon_kwh)


def build_station_capacity_table(
    station_metadata: pd.DataFrame,
    *,
    connector_table: pd.DataFrame | None = None,
    config: QueueModelConfig | None = None,
) -> pd.DataFrame:
    """Build per-station finite service capacity.

    Connector rows are preferred when supplied. Without connector-level data,
    the fallback keeps station total kW and derives a synthetic connector count
    from ``fallback_connector_power_kw`` (or uses ``fallback_connector_count``).
    The source is recorded per station.
    """

    cfg = config or QueueModelConfig()
    cfg.validate()

    metadata = _normalise_station_metadata(station_metadata)
    connector_capacity = _capacity_from_connector_table(connector_table)
    connector_by_station = {
        str(row.station_id): row._asdict()
        for row in connector_capacity.itertuples(index=False)
    }

    rows: list[dict] = []
    seen: set[str] = set()
    for row in metadata.itertuples(index=False):
        station_id = str(row.station_id)
        seen.add(station_id)
        connector_row = connector_by_station.get(station_id)
        if connector_row is not None:
            rows.append(connector_row)
            continue
        rows.append(
            _fallback_capacity_row(
                station_id,
                total_capacity_kw=row.total_capacity_kw,
                config=cfg,
            )
        )

    for station_id, connector_row in connector_by_station.items():
        if station_id not in seen:
            rows.append(connector_row)

    return pd.DataFrame(rows, columns=CAPACITY_COLUMNS)


def run_queue_model(
    sessions: pd.DataFrame,
    station_capacity: pd.DataFrame,
    *,
    config: QueueModelConfig | None = None,
) -> pd.DataFrame:
    """Schedule public charging sessions through deterministic station queues."""

    cfg = config or QueueModelConfig()
    cfg.validate()
    demand = normalise_session_demand(
        sessions,
        energy_epsilon_kwh=cfg.energy_epsilon_kwh,
    )
    if demand.empty:
        return pd.DataFrame(columns=QUEUE_SESSION_COLUMNS)

    capacity = _normalise_capacity_table(station_capacity, cfg)
    capacity_map = {
        str(row.station_id): row._asdict()
        for row in capacity.itertuples(index=False)
    }
    demand = demand.sort_values(
        ["station_id", "arrival_time", "session_id"],
        kind="mergesort",
    ).reset_index(drop=True)

    rows: list[dict] = []
    for station_id, group in demand.groupby("station_id", sort=False):
        cap = capacity_map.get(str(station_id)) or _fallback_capacity_row(
            str(station_id),
            total_capacity_kw=np.nan,
            config=cfg,
        )
        rows.extend(_schedule_station_group(group, cap, cfg))

    return pd.DataFrame(rows, columns=QUEUE_SESSION_COLUMNS)


def aggregate_queue_curve_15min(
    queue_sessions: pd.DataFrame,
    *,
    config: QueueModelConfig | None = None,
) -> pd.DataFrame:
    """Aggregate scheduled queue sessions to station-level 15-minute load bins."""

    cfg = config or QueueModelConfig()
    cfg.validate()
    if queue_sessions.empty:
        return pd.DataFrame(columns=QUEUE_CURVE_15MIN_COLUMNS)

    service_rows: list[dict] = []
    waiting_rows: list[dict] = []
    step_h = cfg.time_resolution_minutes / 60.0

    for row in queue_sessions.itertuples(index=False):
        station_id = str(row.station_id)
        if pd.notna(row.scheduled_service_start_time) and pd.notna(row.scheduled_service_end_time):
            start = pd.Timestamp(row.scheduled_service_start_time)
            end = pd.Timestamp(row.scheduled_service_end_time)
            power_kw = float(row.delivered_energy_after_queue_kwh) / max(
                _duration_h(start, end),
                1e-12,
            )
            for bin_start, bin_end, overlap_h in _iter_interval_bins(
                start,
                end,
                cfg.time_resolution_minutes,
            ):
                service_rows.append(
                    {
                        "station_id": station_id,
                        "time_bin_start": bin_start,
                        "time_bin_end": bin_end,
                        "vehicle_id": str(row.vehicle_id),
                        "session_id": str(row.session_id),
                        "connector_id": str(row.connector_id),
                        "energy_kwh": power_kw * overlap_h,
                        "occupied_connector_time_h": overlap_h,
                    }
                )

        wait_start = pd.Timestamp(row.arrival_time)
        wait_end = pd.Timestamp(row.queue_wait_end_time)
        if wait_end > wait_start:
            for bin_start, bin_end, overlap_h in _iter_interval_bins(
                wait_start,
                wait_end,
                cfg.time_resolution_minutes,
            ):
                waiting_rows.append(
                    {
                        "station_id": station_id,
                        "time_bin_start": bin_start,
                        "time_bin_end": bin_end,
                        "session_id": str(row.session_id),
                        "waiting_session_time_h": overlap_h,
                    }
                )

    service = pd.DataFrame(service_rows)
    waiting = pd.DataFrame(waiting_rows)
    if service.empty and waiting.empty:
        return pd.DataFrame(columns=QUEUE_CURVE_15MIN_COLUMNS)

    service_group = _aggregate_service_bins(service)
    waiting_group = _aggregate_waiting_bins(waiting)
    curve = service_group.merge(
        waiting_group,
        on=["station_id", "time_bin_start", "time_bin_end"],
        how="outer",
    )
    for column, default in {
        "energy_kwh": 0.0,
        "active_vehicle_count": 0,
        "charging_session_count": 0,
        "occupied_connector_count": 0,
        "occupied_connector_time_h": 0.0,
        "queued_session_count": 0,
        "waiting_session_time_h": 0.0,
    }.items():
        if column not in curve.columns:
            curve[column] = default
        curve[column] = curve[column].fillna(default)

    capacity = (
        queue_sessions.loc[
            :,
            ["station_id", "connector_count", "station_capacity_kw"],
        ]
        .drop_duplicates("station_id")
        .copy()
    )
    capacity["station_id"] = capacity["station_id"].astype(str)
    curve = curve.merge(capacity, on="station_id", how="left")
    curve["date"] = pd.to_datetime(curve["time_bin_start"]).dt.strftime("%Y-%m-%d")
    curve["avg_power_kw"] = curve["energy_kwh"] / step_h
    curve["bin_utilization_rate"] = np.divide(
        curve["occupied_connector_time_h"].astype(float),
        curve["connector_count"].astype(float) * step_h,
        out=np.zeros(len(curve), dtype=float),
        where=curve["connector_count"].astype(float).to_numpy() > 0.0,
    )

    for column in [
        "active_vehicle_count",
        "charging_session_count",
        "occupied_connector_count",
        "queued_session_count",
        "connector_count",
    ]:
        curve[column] = curve[column].fillna(0).astype(int)

    curve = curve.sort_values(["station_id", "time_bin_start"]).reset_index(drop=True)
    return curve[QUEUE_CURVE_15MIN_COLUMNS]


def aggregate_queue_curve_hourly(queue_curve_15min: pd.DataFrame) -> pd.DataFrame:
    """Aggregate queue-aware 15-minute station load to hourly station load."""

    if queue_curve_15min.empty:
        return pd.DataFrame(columns=QUEUE_CURVE_HOURLY_COLUMNS)
    curve = queue_curve_15min.copy()
    curve["hour_start_time"] = pd.to_datetime(curve["time_bin_start"]).dt.floor("h")
    grouped = (
        curve.groupby(["station_id", "hour_start_time"], as_index=False)
        .agg(
            energy_kwh=("energy_kwh", "sum"),
            peak_active_vehicle_count=("active_vehicle_count", "max"),
            peak_queued_session_count=("queued_session_count", "max"),
            mean_bin_utilization_rate=("bin_utilization_rate", "mean"),
            connector_count=("connector_count", "first"),
            station_capacity_kw=("station_capacity_kw", "first"),
        )
        .sort_values(["station_id", "hour_start_time"])
        .reset_index(drop=True)
    )
    grouped["hour_end_time"] = grouped["hour_start_time"] + pd.to_timedelta(1, unit="h")
    grouped["date"] = grouped["hour_start_time"].dt.strftime("%Y-%m-%d")
    grouped["avg_power_kw"] = grouped["energy_kwh"]
    return grouped[QUEUE_CURVE_HOURLY_COLUMNS]


def build_station_queue_summary(
    queue_sessions: pd.DataFrame,
    *,
    year: int | None = None,
    study_start_time: pd.Timestamp | None = None,
    study_end_time: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Build station-level waiting, rejection, unmet, and utilization metrics."""

    if queue_sessions.empty:
        return pd.DataFrame(columns=QUEUE_SUMMARY_COLUMNS)
    period_h = _study_period_h(queue_sessions, year, study_start_time, study_end_time)
    frame = queue_sessions.copy()
    frame["service_duration_h"] = frame["service_duration_min"].astype(float) / 60.0
    frame["rejected_energy_kwh"] = np.where(
        frame["rejected"].astype(bool),
        frame["requested_energy_kwh"].astype(float),
        0.0,
    )

    summary = (
        frame.groupby("station_id", as_index=False)
        .agg(
            session_count=("session_id", "nunique"),
            delayed_session_count=("delayed", "sum"),
            rejected_session_count=("rejected", "sum"),
            unmet_session_count=("unmet", "sum"),
            requested_energy_kwh=("requested_energy_kwh", "sum"),
            delivered_energy_after_queue_kwh=("delivered_energy_after_queue_kwh", "sum"),
            unmet_energy_kwh=("unmet_energy_kwh", "sum"),
            rejected_energy_kwh=("rejected_energy_kwh", "sum"),
            average_waiting_time_min=("waiting_time_min", "mean"),
            median_waiting_time_min=("waiting_time_min", "median"),
            p95_waiting_time_min=("waiting_time_min", lambda values: float(values.quantile(0.95))),
            max_waiting_time_min=("waiting_time_min", "max"),
            average_queue_length_on_arrival=("queue_length_on_arrival", "mean"),
            max_queue_length=("queue_length_on_arrival", "max"),
            service_duration_h=("service_duration_h", "sum"),
            connector_count=("connector_count", "first"),
            connector_power_kw=("connector_power_kw", "first"),
            station_capacity_kw=("station_capacity_kw", "first"),
            capacity_source=("capacity_source", "first"),
        )
        .reset_index(drop=True)
    )
    summary["study_period_h"] = float(period_h)
    utilization_where = (
        (summary["connector_count"].astype(float).to_numpy() > 0.0)
        & (float(period_h) > 0.0)
    )
    summary["station_utilization_rate"] = np.divide(
        summary["service_duration_h"].astype(float),
        summary["connector_count"].astype(float) * float(period_h),
        out=np.zeros(len(summary), dtype=float),
        where=utilization_where,
    )
    int_columns = [
        "session_count",
        "delayed_session_count",
        "rejected_session_count",
        "unmet_session_count",
        "max_queue_length",
        "connector_count",
    ]
    for column in int_columns:
        summary[column] = summary[column].fillna(0).astype(int)
    return summary[QUEUE_SUMMARY_COLUMNS]


def build_queue_baseline_comparison(
    baseline_station_curve: pd.DataFrame,
    queue_curve_15min: pd.DataFrame,
    queue_summary: pd.DataFrame,
) -> pd.DataFrame:
    """Compare existing no-queue station load with queue-aware load."""

    baseline = _station_load_rollup(
        baseline_station_curve,
        energy_name="baseline_energy_kwh",
        peak_name="baseline_peak_power_kw",
    )
    queue = _station_load_rollup(
        queue_curve_15min,
        energy_name="queue_energy_kwh",
        peak_name="queue_peak_power_kw",
    )
    comparison = baseline.merge(queue, on="station_id", how="outer")
    summary_columns = [
        "station_id",
        "unmet_energy_kwh",
        "session_count",
        "delayed_session_count",
        "rejected_session_count",
        "unmet_session_count",
        "average_waiting_time_min",
        "median_waiting_time_min",
        "p95_waiting_time_min",
        "max_queue_length",
        "station_utilization_rate",
    ]
    comparison = comparison.merge(
        queue_summary.loc[:, [col for col in summary_columns if col in queue_summary.columns]],
        on="station_id",
        how="outer",
    )
    for column in QUEUE_COMPARISON_COLUMNS:
        if column not in comparison.columns:
            comparison[column] = 0.0
    comparison["baseline_energy_kwh"] = comparison["baseline_energy_kwh"].fillna(0.0)
    comparison["queue_energy_kwh"] = comparison["queue_energy_kwh"].fillna(0.0)
    comparison["baseline_peak_power_kw"] = comparison["baseline_peak_power_kw"].fillna(0.0)
    comparison["queue_peak_power_kw"] = comparison["queue_peak_power_kw"].fillna(0.0)
    comparison["energy_delta_kwh"] = comparison["queue_energy_kwh"] - comparison["baseline_energy_kwh"]
    comparison["peak_power_delta_kw"] = (
        comparison["queue_peak_power_kw"] - comparison["baseline_peak_power_kw"]
    )
    int_columns = [
        "session_count",
        "delayed_session_count",
        "rejected_session_count",
        "unmet_session_count",
        "max_queue_length",
    ]
    for column in int_columns:
        comparison[column] = comparison[column].fillna(0).astype(int)
    float_columns = set(QUEUE_COMPARISON_COLUMNS) - {"station_id"} - set(int_columns)
    for column in float_columns:
        comparison[column] = comparison[column].fillna(0.0).astype(float)
    return comparison.sort_values("station_id").reset_index(drop=True)[QUEUE_COMPARISON_COLUMNS]


def run_queue_model_for_events(
    charging_events: pd.DataFrame,
    station_metadata: pd.DataFrame,
    *,
    baseline_station_curve: pd.DataFrame | None = None,
    connector_table: pd.DataFrame | None = None,
    config: QueueModelConfig | None = None,
    year: int | None = None,
) -> dict[str, pd.DataFrame]:
    """Run the full queue post-processing stack from charging events."""

    cfg = config or QueueModelConfig()
    cfg.validate()
    demand = build_public_session_demand_from_events(
        charging_events,
        energy_epsilon_kwh=cfg.energy_epsilon_kwh,
    )
    capacity = build_station_capacity_table(
        station_metadata,
        connector_table=connector_table,
        config=cfg,
    )
    queue_sessions = run_queue_model(demand, capacity, config=cfg)
    queue_curve_15min = aggregate_queue_curve_15min(queue_sessions, config=cfg)
    queue_curve_hourly = aggregate_queue_curve_hourly(queue_curve_15min)
    queue_summary = build_station_queue_summary(queue_sessions, year=year)
    baseline = baseline_station_curve if baseline_station_curve is not None else pd.DataFrame()
    queue_comparison = build_queue_baseline_comparison(
        baseline,
        queue_curve_15min,
        queue_summary,
    )
    return {
        "queue_session_demand": demand,
        "station_capacity": capacity,
        "queue_sessions": queue_sessions,
        "queue_curve_15min": queue_curve_15min,
        "queue_curve_hourly": queue_curve_hourly,
        "queue_summary": queue_summary,
        "queue_comparison": queue_comparison,
    }


def export_queue_outputs(
    output_dir: Path | str,
    *,
    year: int,
    config: QueueModelConfig,
    station_capacity: pd.DataFrame,
    queue_sessions: pd.DataFrame,
    queue_curve_15min: pd.DataFrame,
    queue_curve_hourly: pd.DataFrame,
    queue_summary: pd.DataFrame,
    queue_comparison: pd.DataFrame,
) -> None:
    """Write queue-aware private-car station outputs without touching baseline files."""

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    _write_frame(queue_curve_15min, out / f"station_charging_curve_15min_queue_aware_{year}.parquet")
    queue_curve_15min.to_csv(out / f"station_charging_curve_15min_queue_aware_{year}.csv", index=False)
    _write_frame(queue_curve_hourly, out / f"station_charging_curve_hourly_queue_aware_{year}.parquet")
    _write_frame(queue_sessions, out / "private_car_public_charging_sessions_queue_aware.parquet")
    _write_frame(station_capacity, out / f"station_queue_capacity_{year}.parquet")
    _write_frame(queue_summary, out / f"station_queue_summary_{year}.parquet")
    queue_summary.to_csv(out / f"station_queue_summary_{year}.csv", index=False)
    _write_frame(queue_comparison, out / f"station_queue_comparison_{year}.parquet")
    queue_comparison.to_csv(out / f"station_queue_comparison_{year}.csv", index=False)
    payload = {
        "schema_version": QUEUE_SCHEMA_VERSION,
        "queue_model": QUEUE_MODEL_NAME,
        "config": asdict(config),
        "outputs": {
            "station_charging_curve_15min_queue_aware": f"station_charging_curve_15min_queue_aware_{year}.parquet",
            "station_charging_curve_hourly_queue_aware": f"station_charging_curve_hourly_queue_aware_{year}.parquet",
            "private_car_public_charging_sessions_queue_aware": "private_car_public_charging_sessions_queue_aware.parquet",
            "station_queue_capacity": f"station_queue_capacity_{year}.parquet",
            "station_queue_summary": f"station_queue_summary_{year}.parquet",
            "station_queue_comparison": f"station_queue_comparison_{year}.parquet",
        },
    }
    (out / f"queue_model_config_{year}.json").write_text(
        json.dumps(payload, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )


def write_queue_model_report(
    output_dir: Path | str,
    *,
    year: int,
    config: QueueModelConfig,
    station_capacity: pd.DataFrame,
    queue_sessions: pd.DataFrame,
    queue_summary: pd.DataFrame,
    queue_comparison: pd.DataFrame,
) -> None:
    """Write a compact provenance report for queue-aware outputs."""

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    total_requested = float(queue_sessions["requested_energy_kwh"].sum()) if not queue_sessions.empty else 0.0
    total_delivered = (
        float(queue_sessions["delivered_energy_after_queue_kwh"].sum())
        if not queue_sessions.empty
        else 0.0
    )
    total_unmet = float(queue_sessions["unmet_energy_kwh"].sum()) if not queue_sessions.empty else 0.0
    capacity_sources = (
        station_capacity["capacity_source"].value_counts().sort_index().to_dict()
        if not station_capacity.empty
        else {}
    )
    lines = [
        f"# Private Car Station Queue Model {year}",
        "",
        "## Scope",
        "",
        f"- queue_model: `{QUEUE_MODEL_NAME}`",
        f"- schema_version: `{QUEUE_SCHEMA_VERSION}`",
        f"- time_resolution_minutes: `{config.time_resolution_minutes}`",
        f"- availability_model: `{config.availability_model}`",
        f"- allow_service_after_window: `{config.allow_service_after_window}`",
        f"- max_delay_min: `{config.max_delay_min}`",
        "",
        "## Capacity Assumptions",
        "",
        "- Connector-level rows are used when a connector table is supplied.",
        "- Without connector-level rows, connector count is a configurable fallback derived from station total capacity.",
        "- The fallback does not claim observed connector counts; every affected station records `capacity_source`.",
        f"- fallback_connector_power_kw: `{config.fallback_connector_power_kw}`",
        f"- fallback_connector_count: `{config.fallback_connector_count}`",
        f"- capacity_source_counts: `{capacity_sources}`",
        "",
        "## Queue Metrics",
        "",
        f"- public sessions modelled: `{len(queue_sessions)}`",
        f"- stations with modelled capacity: `{station_capacity['station_id'].nunique() if not station_capacity.empty else 0}`",
        f"- requested_energy_kwh: `{total_requested:.9f}`",
        f"- delivered_energy_after_queue_kwh: `{total_delivered:.9f}`",
        f"- unmet_energy_kwh: `{total_unmet:.9f}`",
        f"- delayed sessions: `{int(queue_sessions['delayed'].sum()) if not queue_sessions.empty else 0}`",
        f"- rejected sessions: `{int(queue_sessions['rejected'].sum()) if not queue_sessions.empty else 0}`",
        f"- station summary rows: `{len(queue_summary)}`",
        f"- comparison rows: `{len(queue_comparison)}`",
        "",
        "## Limitations",
        "",
        "- This is a deterministic FCFS queue; stochastic outage/availability is an extension point, not active here.",
        "- It is a post-processing layer over requested public charging demand and does not feed delayed charging back into vehicle SOC or trip feasibility.",
        "- By default service cannot continue after the original parking window; unmet energy is reported rather than silently extending the vehicle dwell.",
        "- Full national runs may need streaming/sharded queue aggregation to avoid keeping all public sessions in memory.",
    ]
    (out / f"queue_model_report_{year}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_station_metadata_json(path: Path | str) -> pd.DataFrame:
    """Load station metadata written by the station curve pipeline."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    records = payload.get("stations", payload if isinstance(payload, list) else [])
    return pd.DataFrame(records)


def _schedule_station_group(
    group: pd.DataFrame,
    capacity: Mapping[str, object],
    config: QueueModelConfig,
) -> list[dict]:
    connector_count = max(1, int(capacity["connector_count"]))
    connector_power_kw = max(0.0, float(capacity["connector_power_kw"]))
    station_capacity_kw = max(0.0, float(capacity["station_capacity_kw"]))
    capacity_source = str(capacity["capacity_source"])
    server_count = min(connector_count, max(1, len(group)))
    first_arrival = pd.Timestamp(group["arrival_time"].min())
    connectors = [
        {
            "connector_id": f"{capacity['station_id']}_conn_{idx + 1}",
            "available_time": first_arrival,
        }
        for idx in range(server_count)
    ]
    queued_wait_end_times: list[pd.Timestamp] = []
    rows: list[dict] = []

    for demand in group.itertuples(index=False):
        arrival = pd.Timestamp(demand.arrival_time)
        window_end = pd.Timestamp(demand.window_end_time)
        while queued_wait_end_times and min(queued_wait_end_times) <= arrival:
            queued_wait_end_times.remove(min(queued_wait_end_times))

        connector = min(connectors, key=lambda item: (item["available_time"], item["connector_id"]))
        first_available = pd.Timestamp(connector["available_time"])
        start_candidate = max(arrival, first_available)
        latest_end = _latest_service_end(window_end, config)
        requested_energy = max(0.0, float(demand.requested_energy_kwh))
        requested_power = max(0.0, float(demand.requested_power_kw))
        service_power = min(requested_power, connector_power_kw)

        service_start = pd.NaT
        service_end = pd.NaT
        delivered = 0.0
        service_duration_min = 0.0
        queue_wait_end = min(start_candidate, latest_end)

        if service_power > 0.0 and start_candidate < latest_end:
            requested_duration_h = requested_energy / service_power if service_power > 0.0 else 0.0
            full_end = start_candidate + pd.to_timedelta(requested_duration_h, unit="h")
            actual_end = min(full_end, latest_end)
            actual_duration_h = max(0.0, _duration_h(start_candidate, actual_end))
            delivered = min(requested_energy, service_power * actual_duration_h)
            if delivered > config.energy_epsilon_kwh:
                service_start = start_candidate
                service_end = actual_end
                service_duration_min = actual_duration_h * 60.0
                queue_wait_end = start_candidate
                connector["available_time"] = actual_end

        unmet_energy = max(0.0, requested_energy - delivered)
        waiting_min = max(0.0, _duration_h(arrival, queue_wait_end) * 60.0)
        delayed = waiting_min > 1e-9
        rejected = delivered <= config.energy_epsilon_kwh and requested_energy > config.energy_epsilon_kwh
        unmet = unmet_energy > config.energy_epsilon_kwh
        if rejected:
            status = "rejected"
        elif unmet:
            status = "partial_unmet"
        elif delayed:
            status = "delayed_served"
        else:
            status = "served"

        if queue_wait_end > arrival:
            queued_wait_end_times.append(queue_wait_end)

        rows.append(
            {
                "session_id": str(demand.session_id),
                "vehicle_id": str(demand.vehicle_id),
                "station_id": str(demand.station_id),
                "arrival_time": arrival,
                "window_end_time": window_end,
                "requested_energy_kwh": requested_energy,
                "requested_power_kw": requested_power,
                "connector_id": connector["connector_id"] if not rejected else pd.NA,
                "connector_count": connector_count,
                "connector_power_kw": connector_power_kw,
                "station_capacity_kw": station_capacity_kw,
                "capacity_source": capacity_source,
                "queue_length_on_arrival": int(len(queued_wait_end_times)),
                "first_available_connector_time": first_available,
                "queue_wait_end_time": queue_wait_end,
                "scheduled_service_start_time": service_start,
                "scheduled_service_end_time": service_end,
                "waiting_time_min": waiting_min,
                "service_duration_min": service_duration_min,
                "delivered_energy_after_queue_kwh": delivered,
                "unmet_energy_kwh": unmet_energy,
                "delayed": bool(delayed),
                "rejected": bool(rejected),
                "unmet": bool(unmet),
                "queue_status": status,
            }
        )

    return rows


def _clean_session_demand(
    frame: pd.DataFrame,
    *,
    energy_epsilon_kwh: float,
) -> pd.DataFrame:
    result = frame.copy()
    result["station_id"] = result["station_id"].astype(str)
    result = result.loc[
        result["station_id"].notna()
        & (result["station_id"].str.strip() != "")
        & result["arrival_time"].notna()
        & result["window_end_time"].notna()
    ].copy()
    result["window_end_time"] = result["window_end_time"].where(
        result["window_end_time"] >= result["arrival_time"],
        result["arrival_time"],
    )
    result = result.loc[result["requested_energy_kwh"].astype(float) > energy_epsilon_kwh].copy()
    result = result.loc[result["requested_power_kw"].astype(float) > 0.0].copy()
    return result[SESSION_DEMAND_COLUMNS].reset_index(drop=True)


def _normalise_station_metadata(station_metadata: pd.DataFrame) -> pd.DataFrame:
    if station_metadata.empty:
        return pd.DataFrame(columns=["station_id", "total_capacity_kw"])
    station_col = _first_existing_column(station_metadata, ["station_id", "StationID"])
    if station_col is None:
        raise KeyError("station_metadata must contain station_id or StationID.")
    capacity_col = _first_existing_column(
        station_metadata,
        ["total_capacity_kw", "capacity_kw", "TotalCapacity_kW"],
    )
    result = pd.DataFrame(
        {
            "station_id": station_metadata[station_col].astype(str),
            "total_capacity_kw": pd.to_numeric(
                station_metadata[capacity_col], errors="coerce"
            )
            if capacity_col is not None
            else np.nan,
        }
    )
    return result.drop_duplicates("station_id").reset_index(drop=True)


def _capacity_from_connector_table(connector_table: pd.DataFrame | None) -> pd.DataFrame:
    if connector_table is None or connector_table.empty:
        return pd.DataFrame(columns=CAPACITY_COLUMNS)

    station_col = _first_existing_column(connector_table, ["station_id", "StationID"])
    if station_col is None:
        raise KeyError("connector_table must contain station_id or StationID.")
    quantity_col = _first_existing_column(connector_table, ["connector_count", "Quantity", "quantity"])
    power_col = _first_existing_column(
        connector_table,
        ["connector_power_kw", "Power_kW", "power_kw"],
    )
    capacity_col = _first_existing_column(
        connector_table,
        ["station_capacity_kw", "Capacity_kW", "capacity_kw"],
    )
    if power_col is None and capacity_col is None:
        raise KeyError("connector_table must contain connector power or capacity in kW.")

    frame = pd.DataFrame({"station_id": connector_table[station_col].astype(str)})
    if quantity_col is not None:
        frame["connector_count"] = pd.to_numeric(connector_table[quantity_col], errors="coerce").fillna(1.0)
    else:
        frame["connector_count"] = 1.0
    frame["connector_count"] = frame["connector_count"].clip(lower=1.0)

    if power_col is not None:
        frame["connector_power_kw"] = pd.to_numeric(connector_table[power_col], errors="coerce")
        frame["station_capacity_kw"] = frame["connector_power_kw"] * frame["connector_count"]
    else:
        frame["station_capacity_kw"] = pd.to_numeric(connector_table[capacity_col], errors="coerce")
        frame["connector_power_kw"] = frame["station_capacity_kw"] / frame["connector_count"]

    frame = frame.replace([np.inf, -np.inf], np.nan).dropna(
        subset=["connector_count", "connector_power_kw", "station_capacity_kw"]
    )
    frame = frame.loc[(frame["connector_count"] > 0.0) & (frame["station_capacity_kw"] > 0.0)].copy()
    if frame.empty:
        return pd.DataFrame(columns=CAPACITY_COLUMNS)

    grouped = (
        frame.groupby("station_id", as_index=False)
        .agg(
            connector_count=("connector_count", "sum"),
            station_capacity_kw=("station_capacity_kw", "sum"),
        )
        .reset_index(drop=True)
    )
    grouped["connector_count"] = grouped["connector_count"].round().clip(lower=1).astype(int)
    grouped["connector_power_kw"] = grouped["station_capacity_kw"] / grouped["connector_count"]
    grouped["capacity_source"] = "connector_table"
    return grouped[CAPACITY_COLUMNS]


def _normalise_capacity_table(
    station_capacity: pd.DataFrame,
    config: QueueModelConfig,
) -> pd.DataFrame:
    if station_capacity.empty:
        return pd.DataFrame(columns=CAPACITY_COLUMNS)
    required = {"station_id", "connector_count", "connector_power_kw", "station_capacity_kw"}
    missing = required.difference(station_capacity.columns)
    if missing:
        raise KeyError(f"station_capacity missing columns: {sorted(missing)}")
    capacity = station_capacity.copy()
    capacity["station_id"] = capacity["station_id"].astype(str)
    capacity["connector_count"] = pd.to_numeric(capacity["connector_count"], errors="coerce").fillna(1)
    capacity["connector_count"] = capacity["connector_count"].round().clip(lower=1).astype(int)
    capacity["connector_power_kw"] = pd.to_numeric(
        capacity["connector_power_kw"], errors="coerce"
    ).fillna(config.fallback_connector_power_kw)
    capacity["connector_power_kw"] = capacity["connector_power_kw"].clip(lower=1e-9)
    capacity["station_capacity_kw"] = pd.to_numeric(
        capacity["station_capacity_kw"], errors="coerce"
    ).fillna(capacity["connector_count"] * capacity["connector_power_kw"])
    if "capacity_source" not in capacity.columns:
        capacity["capacity_source"] = "provided_capacity_table"
    return capacity[CAPACITY_COLUMNS].drop_duplicates("station_id").reset_index(drop=True)


def _fallback_capacity_row(
    station_id: str,
    *,
    total_capacity_kw: object,
    config: QueueModelConfig,
) -> dict:
    total = _positive_float(total_capacity_kw)
    if total is None:
        count = config.fallback_connector_count or 1
        power = float(config.fallback_connector_power_kw)
        source = "fallback_missing_station_total_capacity"
        total = count * power
    else:
        if config.fallback_connector_count is not None:
            count = int(config.fallback_connector_count)
        else:
            count = max(1, int(math.floor(total / config.fallback_connector_power_kw)))
        power = total / count
        source = "fallback_from_station_total_capacity_kw"
    return {
        "station_id": str(station_id),
        "connector_count": int(count),
        "connector_power_kw": float(power),
        "station_capacity_kw": float(total),
        "capacity_source": source,
    }


def _aggregate_service_bins(service: pd.DataFrame) -> pd.DataFrame:
    if service.empty:
        return pd.DataFrame(
            columns=[
                "station_id",
                "time_bin_start",
                "time_bin_end",
                "energy_kwh",
                "active_vehicle_count",
                "charging_session_count",
                "occupied_connector_count",
                "occupied_connector_time_h",
            ]
        )
    return (
        service.groupby(["station_id", "time_bin_start", "time_bin_end"], as_index=False)
        .agg(
            energy_kwh=("energy_kwh", "sum"),
            active_vehicle_count=("vehicle_id", "nunique"),
            charging_session_count=("session_id", "nunique"),
            occupied_connector_count=("connector_id", "nunique"),
            occupied_connector_time_h=("occupied_connector_time_h", "sum"),
        )
        .reset_index(drop=True)
    )


def _aggregate_waiting_bins(waiting: pd.DataFrame) -> pd.DataFrame:
    if waiting.empty:
        return pd.DataFrame(
            columns=[
                "station_id",
                "time_bin_start",
                "time_bin_end",
                "queued_session_count",
                "waiting_session_time_h",
            ]
        )
    return (
        waiting.groupby(["station_id", "time_bin_start", "time_bin_end"], as_index=False)
        .agg(
            queued_session_count=("session_id", "nunique"),
            waiting_session_time_h=("waiting_session_time_h", "sum"),
        )
        .reset_index(drop=True)
    )


def _station_load_rollup(
    station_curve: pd.DataFrame,
    *,
    energy_name: str,
    peak_name: str,
) -> pd.DataFrame:
    if station_curve.empty:
        return pd.DataFrame(columns=["station_id", energy_name, peak_name])
    return (
        station_curve.assign(station_id=station_curve["station_id"].astype(str))
        .groupby("station_id", as_index=False)
        .agg(**{energy_name: ("energy_kwh", "sum"), peak_name: ("avg_power_kw", "max")})
        .reset_index(drop=True)
    )


def _study_period_h(
    queue_sessions: pd.DataFrame,
    year: int | None,
    study_start_time: pd.Timestamp | None,
    study_end_time: pd.Timestamp | None,
) -> float:
    if study_start_time is not None and study_end_time is not None:
        return max(0.0, _duration_h(study_start_time, study_end_time))
    if year is not None:
        start = pd.Timestamp(dt.datetime(year, 1, 1))
        end = pd.Timestamp(dt.datetime(year + 1, 1, 1))
        return _duration_h(start, end)
    if queue_sessions.empty:
        return 0.0
    start = pd.Timestamp(queue_sessions["arrival_time"].min())
    end = pd.Timestamp(queue_sessions["window_end_time"].max())
    return max(STEP_HOURS, _duration_h(start, end))


def _latest_service_end(window_end: pd.Timestamp, config: QueueModelConfig) -> pd.Timestamp:
    if not config.allow_service_after_window:
        return window_end
    if config.max_delay_min is None:
        return pd.Timestamp.max
    return window_end + pd.to_timedelta(float(config.max_delay_min), unit="min")


def _iter_interval_bins(
    start: pd.Timestamp,
    end: pd.Timestamp,
    resolution_minutes: int,
) -> Iterable[tuple[pd.Timestamp, pd.Timestamp, float]]:
    if pd.isna(start) or pd.isna(end) or end <= start:
        return
    bin_start = start.floor(f"{resolution_minutes}min")
    delta = pd.to_timedelta(resolution_minutes, unit="min")
    while bin_start < end:
        bin_end = bin_start + delta
        overlap_start = max(start, bin_start)
        overlap_end = min(end, bin_end)
        if overlap_end > overlap_start:
            yield bin_start, bin_end, _duration_h(overlap_start, overlap_end)
        bin_start = bin_end


def _duration_h(start: pd.Timestamp, end: pd.Timestamp) -> float:
    return (pd.Timestamp(end) - pd.Timestamp(start)).total_seconds() / 3600.0


def _positive_float(value: object) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(numeric) or numeric <= 0.0:
        return None
    return numeric


def _column_or_index(frame: pd.DataFrame, column: str) -> pd.Series:
    if column in frame.columns:
        return frame[column].astype(str)
    return pd.Series([f"session_{idx}" for idx in frame.index], index=frame.index)


def _first_existing_column(frame: pd.DataFrame, candidates: list[str]) -> str | None:
    for candidate in candidates:
        if candidate in frame.columns:
            return candidate
    return None


def _write_frame(frame: pd.DataFrame, path: Path) -> None:
    out = frame.copy()
    for column in out.columns:
        if column.endswith("_time") or column in {
            "time_bin_start",
            "time_bin_end",
            "hour_start_time",
            "hour_end_time",
        }:
            out[column] = pd.to_datetime(out[column], errors="coerce")
    out.to_parquet(path, index=False)

"""Queue-aware private-car station charging tests."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from mobility.cars.station_queue import (
    QueueModelConfig,
    aggregate_queue_curve_15min,
    aggregate_queue_curve_hourly,
    build_public_session_demand_from_events,
    build_queue_baseline_comparison,
    build_station_capacity_table,
    build_station_queue_summary,
    export_queue_outputs,
    run_queue_model,
)


def _ts(value: str) -> pd.Timestamp:
    return pd.Timestamp(value)


def test_fcfs_queue_delays_second_session_and_preserves_energy() -> None:
    sessions = pd.DataFrame(
        {
            "session_id": ["s1", "s2"],
            "vehicle_id": ["v1", "v2"],
            "station_id": ["101", "101"],
            "arrival_time": [_ts("2025-01-01 10:00"), _ts("2025-01-01 10:15")],
            "window_end_time": [_ts("2025-01-01 11:00"), _ts("2025-01-01 12:00")],
            "requested_energy_kwh": [7.0, 3.5],
            "requested_power_kw": [7.0, 7.0],
        }
    )
    capacity = pd.DataFrame(
        {
            "station_id": ["101"],
            "connector_count": [1],
            "connector_power_kw": [7.0],
            "station_capacity_kw": [7.0],
            "capacity_source": ["test"],
        }
    )

    queued = run_queue_model(sessions, capacity)

    second = queued.loc[queued["session_id"] == "s2"].iloc[0]
    assert second["scheduled_service_start_time"] == _ts("2025-01-01 11:00")
    assert second["scheduled_service_end_time"] == _ts("2025-01-01 11:30")
    assert second["waiting_time_min"] == 45.0
    assert second["queue_length_on_arrival"] == 1
    assert second["delayed"] is True or second["delayed"] == True
    assert second["rejected"] is False or second["rejected"] == False
    assert queued["delivered_energy_after_queue_kwh"].sum() == 10.5

    curve = aggregate_queue_curve_15min(queued)
    summary = build_station_queue_summary(queued)
    comparison = build_queue_baseline_comparison(
        pd.DataFrame(
            {
                "station_id": ["101"],
                "time_bin_start": [_ts("2025-01-01 10:00")],
                "time_bin_end": [_ts("2025-01-01 10:15")],
                "energy_kwh": [10.5],
                "avg_power_kw": [42.0],
            }
        ),
        curve,
        summary,
    )

    assert curve["energy_kwh"].sum() == 10.5
    assert curve["queued_session_count"].max() == 1
    assert summary.loc[0, "delayed_session_count"] == 1
    assert summary.loc[0, "max_queue_length"] == 1
    assert summary.loc[0, "station_utilization_rate"] == 0.75
    assert comparison.loc[0, "queue_energy_kwh"] == 10.5
    assert comparison.loc[0, "delayed_session_count"] == 1


def test_queue_rejects_when_connector_available_after_parking_window() -> None:
    sessions = pd.DataFrame(
        {
            "session_id": ["s1", "s2"],
            "vehicle_id": ["v1", "v2"],
            "station_id": ["101", "101"],
            "arrival_time": [_ts("2025-01-01 10:00"), _ts("2025-01-01 10:15")],
            "window_end_time": [_ts("2025-01-01 11:00"), _ts("2025-01-01 10:45")],
            "requested_energy_kwh": [7.0, 3.5],
            "requested_power_kw": [7.0, 7.0],
        }
    )
    capacity = pd.DataFrame(
        {
            "station_id": ["101"],
            "connector_count": [1],
            "connector_power_kw": [7.0],
            "station_capacity_kw": [7.0],
            "capacity_source": ["test"],
        }
    )

    queued = run_queue_model(sessions, capacity)

    rejected = queued.loc[queued["session_id"] == "s2"].iloc[0]
    assert rejected["queue_status"] == "rejected"
    assert rejected["waiting_time_min"] == 30.0
    assert rejected["delivered_energy_after_queue_kwh"] == 0.0
    assert rejected["unmet_energy_kwh"] == 3.5
    assert queued["delivered_energy_after_queue_kwh"].sum() == 7.0


def test_station_capacity_uses_connector_table_then_configurable_fallback() -> None:
    metadata = pd.DataFrame(
        {
            "station_id": ["101", "202"],
            "total_capacity_kw": [21.0, 50.0],
        }
    )
    connectors = pd.DataFrame(
        {
            "StationID": [202, 202],
            "Power_kW": [50.0, 22.0],
            "Quantity": [2, 1],
        }
    )
    config = QueueModelConfig(fallback_connector_power_kw=7.0)

    capacity = build_station_capacity_table(
        metadata,
        connector_table=connectors,
        config=config,
    ).set_index("station_id")

    assert capacity.loc["101", "connector_count"] == 3
    assert capacity.loc["101", "connector_power_kw"] == 7.0
    assert capacity.loc["101", "capacity_source"] == "fallback_from_station_total_capacity_kw"
    assert capacity.loc["202", "connector_count"] == 3
    assert capacity.loc["202", "station_capacity_kw"] == 122.0
    assert capacity.loc["202", "capacity_source"] == "connector_table"



def test_expanded_connector_rows_without_quantity_count_as_one_device_each() -> None:
    metadata = pd.DataFrame({"station_id": ["796"], "total_capacity_kw": [28.0]})
    connectors = pd.DataFrame(
        {
            "ConnectorID": ["conn_1", "conn_2", "conn_3", "conn_4"],
            "StationID": [796, 796, 796, 796],
            "Power_kW": [7.0, 7.0, 7.0, 7.0],
            "lsoa_code": ["S01013566"] * 4,
        }
    )

    capacity = build_station_capacity_table(metadata, connector_table=connectors).set_index("station_id")

    assert capacity.loc["796", "connector_count"] == 4
    assert capacity.loc["796", "connector_power_kw"] == 7.0
    assert capacity.loc["796", "station_capacity_kw"] == 28.0
    assert capacity.loc["796", "capacity_source"] == "connector_table"


def test_public_session_demand_filters_home_and_failed_events() -> None:
    events = pd.DataFrame(
        {
            "event_id": ["home", "public", "failed"],
            "ev_id": ["v1", "v2", "v3"],
            "charging_type": ["home", "public_current_lsoa", "failed_public_charging"],
            "station_id": [None, "101", None],
            "charging_start_time": [_ts("2025-01-01 01:00")] * 3,
            "charging_end_time": [_ts("2025-01-01 02:00")] * 3,
            "charged_energy_kwh": [5.0, 7.0, 0.0],
            "charging_power_kw": [7.0, 7.0, 0.0],
        }
    )

    demand = build_public_session_demand_from_events(events)

    assert demand["session_id"].tolist() == ["public"]
    assert demand["station_id"].tolist() == ["101"]
    assert demand["requested_energy_kwh"].tolist() == [7.0]


def test_queue_exports_expected_files(tmp_path: Path) -> None:
    sessions = pd.DataFrame(
        {
            "session_id": ["s1"],
            "vehicle_id": ["v1"],
            "station_id": ["101"],
            "arrival_time": [_ts("2025-01-01 10:00")],
            "window_end_time": [_ts("2025-01-01 11:00")],
            "requested_energy_kwh": [7.0],
            "requested_power_kw": [7.0],
        }
    )
    capacity = pd.DataFrame(
        {
            "station_id": ["101"],
            "connector_count": [1],
            "connector_power_kw": [7.0],
            "station_capacity_kw": [7.0],
            "capacity_source": ["test"],
        }
    )
    config = QueueModelConfig()
    queued = run_queue_model(sessions, capacity, config=config)
    curve = aggregate_queue_curve_15min(queued, config=config)
    hourly = aggregate_queue_curve_hourly(curve)
    summary = build_station_queue_summary(queued)
    comparison = build_queue_baseline_comparison(pd.DataFrame(), curve, summary)

    export_queue_outputs(
        tmp_path,
        year=2025,
        config=config,
        station_capacity=capacity,
        queue_sessions=queued,
        queue_curve_15min=curve,
        queue_curve_hourly=hourly,
        queue_summary=summary,
        queue_comparison=comparison,
    )

    assert (tmp_path / "station_charging_curve_15min_queue_aware_2025.parquet").exists()
    assert (tmp_path / "station_charging_curve_hourly_queue_aware_2025.parquet").exists()
    assert (tmp_path / "private_car_public_charging_sessions_queue_aware.parquet").exists()
    assert (tmp_path / "station_queue_summary_2025.parquet").exists()
    assert (tmp_path / "station_queue_comparison_2025.csv").exists()
    assert (tmp_path / "queue_model_config_2025.json").exists()

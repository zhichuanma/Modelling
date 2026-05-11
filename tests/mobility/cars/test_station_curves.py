"""Private-car station curve export tests."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from mobility.cars.station_curves import (
    _apply_vehicle_shard,
    _normalise_vehicle_shard,
    aggregate_station_curves_15min,
    build_privatecar_person_week_integrity_report,
    build_session_time_bins_for_ev,
    build_station_index_json,
    build_station_day_json,
    export_web_json_files,
)
from mobility.core.data_structures import DailySchedule, ParkingEvent
from mobility.core.simulator import STEP_HOURS, STEPS_PER_DAY


def test_vehicle_shard_selects_round_robin_rows_and_aligns_ev_fleet() -> None:
    person_fleet = pd.DataFrame(
        {
            "ev_id": [f"cars_{idx}" for idx in range(10)],
            "person_id": [f"person_{idx}" for idx in range(10)],
        }
    )
    ev_fleet = pd.DataFrame(
        {
            "EV_ID": [f"cars_{idx}" for idx in reversed(range(10))],
            "home_lsoa": [f"E{idx:08d}" for idx in reversed(range(10))],
        }
    )

    person_shard, ev_shard, metadata = _apply_vehicle_shard(
        person_fleet,
        ev_fleet,
        vehicle_shard_index=1,
        vehicle_shard_count=3,
    )

    assert person_shard["ev_id"].tolist() == ["cars_1", "cars_4", "cars_7"]
    assert ev_shard["EV_ID"].tolist() == ["cars_1", "cars_4", "cars_7"]
    assert metadata == {
        "vehicle_count_before_shard": 10,
        "vehicle_count_after_shard": 3,
        "vehicle_shard_index": 1,
        "vehicle_shard_count": 3,
    }


def test_vehicle_shard_arguments_must_be_complete_and_in_range() -> None:
    assert _normalise_vehicle_shard(None, None) == (None, None)
    assert _normalise_vehicle_shard(0, 2) == (0, 2)

    with pytest.raises(ValueError, match="supplied together"):
        _normalise_vehicle_shard(0, None)
    with pytest.raises(ValueError, match="positive"):
        _normalise_vehicle_shard(0, 0)
    with pytest.raises(ValueError, match="0 <= index"):
        _normalise_vehicle_shard(2, 2)


def test_station_bin_mapping_splits_public_session_to_15min_steps() -> None:
    schedule = DailySchedule(
        ev_id="cars_test",
        day=0,
        day_type="weekday",
        date=dt.date(2025, 1, 1),
        parking_events=[
            ParkingEvent(
                start_time=1.0,
                end_time=1.5,
                duration_hours=0.5,
                location_purpose="work",
                can_charge=True,
                matched_station_id=101,
                charge_power_kw=7.0,
            )
        ],
    )
    load = np.zeros(STEPS_PER_DAY)
    load[4:6] = 7.0

    bins, sessions, metrics = build_session_time_bins_for_ev("cars_test", [schedule], load)
    curve = aggregate_station_curves_15min(bins)

    assert metrics["invalid_session_time_count"] == 0
    assert len(bins) == 2
    assert len(sessions) == 1
    assert bins["energy_kwh"].sum() == 7.0 * 0.5
    assert curve["avg_power_kw"].tolist() == [7.0, 7.0]
    assert curve["active_vehicle_count"].tolist() == [1, 1]
    assert curve["charging_session_count"].tolist() == [1, 1]


def test_station_bin_mapping_allocates_shared_step_by_event_weight() -> None:
    schedule = DailySchedule(
        ev_id="cars_test",
        day=0,
        day_type="weekday",
        date=dt.date(2025, 1, 1),
        parking_events=[
            ParkingEvent(
                start_time=0.0,
                end_time=0.125,
                duration_hours=0.125,
                location_purpose="home",
                can_charge=True,
                matched_station_id=None,
                charge_power_kw=7.0,
            ),
            ParkingEvent(
                start_time=0.125,
                end_time=0.25,
                duration_hours=0.125,
                location_purpose="work",
                can_charge=True,
                matched_station_id=202,
                charge_power_kw=7.0,
            ),
        ],
    )
    load = np.zeros(STEPS_PER_DAY)
    load[0] = 7.0

    bins, sessions, _metrics = build_session_time_bins_for_ev("cars_test", [schedule], load)

    assert len(bins) == 1
    assert len(sessions) == 1
    assert bins.loc[0, "station_id"] == "202"
    assert bins.loc[0, "energy_kwh"] == (7.0 * STEP_HOURS) / 2.0
    assert sessions.loc[0, "delivered_energy_kwh"] == (7.0 * STEP_HOURS) / 2.0


def test_station_day_json_is_complete_96_point_payload() -> None:
    station_curve = pd.DataFrame(
        {
            "station_id": ["101"],
            "time_bin_start": [pd.Timestamp("2025-01-01T01:00:00")],
            "time_bin_end": [pd.Timestamp("2025-01-01T01:15:00")],
            "date": ["2025-01-01"],
            "energy_kwh": [1.75],
            "avg_power_kw": [7.0],
            "active_vehicle_count": [1],
            "charging_session_count": [1],
        }
    )
    station_day_counts = pd.DataFrame(
        {
            "station_id": ["101"],
            "date": ["2025-01-01"],
            "unique_vehicles": [1],
            "total_sessions": [1],
        }
    )
    metadata = {
        "101": {
            "station_id": "101",
            "station_name": "Station 101",
            "latitude": 51.5,
            "longitude": -0.1,
        }
    }

    payload = build_station_day_json(
        "101",
        "2025-01-01",
        station_curve,
        metadata,
        station_day_counts,
        year=2025,
    )

    assert len(payload["curve"]) == 96
    assert payload["scope"] == "private_car_public_charging_only"
    assert payload["year"] == 2025
    assert payload["station_id"] == "101"
    assert payload["station_name"] == "Station 101"
    assert payload["curve"][0]["time_label"] == "00:00"
    assert payload["curve"][0]["time"] == "00:00"
    assert payload["curve"][-1]["time_label"] == "23:45"
    assert payload["curve"][4]["avg_power_kw"] == 7.0
    assert payload["summary"]["daily_energy_kwh"] == 1.75
    assert payload["summary"]["daily_active_vehicle_count"] == 1


def test_station_index_json_contains_frontend_lookup_fields() -> None:
    station_curve = pd.DataFrame(
        {
            "station_id": ["101"],
            "time_bin_start": [pd.Timestamp("2025-01-01T01:00:00")],
            "time_bin_end": [pd.Timestamp("2025-01-01T01:15:00")],
            "date": ["2025-01-01"],
            "energy_kwh": [1.75],
            "avg_power_kw": [7.0],
            "active_vehicle_count": [1],
            "charging_session_count": [1],
        }
    )
    station_summary = pd.DataFrame(
        {
            "station_id": ["101"],
            "station_name": ["Station 101"],
            "latitude": [51.5],
            "longitude": [-0.1],
            "total_energy_kwh_2025": [1.75],
            "peak_power_kw_2025": [7.0],
            "peak_time_2025": ["2025-01-01T01:00:00"],
        }
    )
    metadata = {
        "101": {
            "station_id": "101",
            "station_name": "Station 101",
            "latitude": 51.5,
            "longitude": -0.1,
        }
    }

    payload = build_station_index_json(station_curve, station_summary, metadata, year=2025)
    station = payload["stations"][0]

    assert payload["scope"] == "private_car_public_charging_only"
    assert payload["year"] == 2025
    assert station["station_id"] == "101"
    assert station["available_dates"] == ["2025-01-01"]
    assert station["total_energy_kwh"] == 1.75
    assert station["peak_power_kw"] == 7.0
    assert station["peak_time"] == "2025-01-01T01:00:00"


def test_export_web_json_retries_transient_write_timeout(monkeypatch, tmp_path: Path) -> None:
    station_curve = pd.DataFrame(
        {
            "station_id": ["101"],
            "time_bin_start": [pd.Timestamp("2025-01-01T01:00:00")],
            "time_bin_end": [pd.Timestamp("2025-01-01T01:15:00")],
            "date": ["2025-01-01"],
            "energy_kwh": [1.75],
            "avg_power_kw": [7.0],
            "active_vehicle_count": [1],
            "charging_session_count": [1],
        }
    )
    station_summary = pd.DataFrame(
        {
            "station_id": ["101"],
            "station_name": ["Station 101"],
            "latitude": [51.5],
            "longitude": [-0.1],
            "total_energy_kwh_2025": [1.75],
            "peak_power_kw_2025": [7.0],
            "peak_time_2025": ["2025-01-01T01:00:00"],
        }
    )
    station_metadata = station_summary.loc[:, ["station_id", "station_name", "latitude", "longitude"]]
    station_day_counts = pd.DataFrame(
        {
            "station_id": ["101"],
            "date": ["2025-01-01"],
            "unique_vehicles": [1],
            "total_sessions": [1],
        }
    )
    original_write_text = Path.write_text
    failures = {"remaining": 1}

    def flaky_write_text(self: Path, *args, **kwargs):
        if self.name.endswith(".tmp") and failures["remaining"] > 0:
            failures["remaining"] -= 1
            raise TimeoutError(60, "Operation timed out", str(self))
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", flaky_write_text)

    metrics = export_web_json_files(
        station_curve,
        station_summary,
        station_metadata,
        station_day_counts,
        tmp_path,
        year=2025,
        json_indent=None,
    )

    daily_path = tmp_path / "web" / "daily_curves" / "101" / "2025-01-01.json"
    assert metrics["json_file_count"] == 2
    assert metrics["json_write_retry_count"] == 1
    assert failures["remaining"] == 0
    assert json.loads(daily_path.read_text(encoding="utf-8"))["station_id"] == "101"
    assert not list((tmp_path / "web").rglob("*.tmp"))


def test_preflight_integrity_report_normalizes_ids_and_groups_missing() -> None:
    person_fleet = pd.DataFrame(
        {
            "ev_id": ["cars_1", "cars_2"],
            "person_id": ["100", "200"],
            "nts_household_id": ["hh1", "hh2"],
            "nts_region": ["london", "south_west"],
        }
    )
    library_df = pd.DataFrame({"person_id": [100, "300"]})
    ev_fleet = pd.DataFrame(
        {
            "EV_ID": ["cars_1", "cars_2"],
            "LSOA_code": ["E01000001", "E01000002"],
            "LAD": ["E09000001", "E09000002"],
            "Model": ["Model A", "Model B"],
            "vehicle_subtype": ["cars", "cars"],
            "allocation_method": ["population", "population"],
            "EV_ID_in_row": [1, 2],
        }
    )

    report = build_privatecar_person_week_integrity_report(
        person_fleet,
        library_df,
        ev_fleet,
        scope="test",
    )

    assert report["summary"]["private_car_unique_person_ids"] == 2
    assert report["summary"]["person_week_library_unique_person_ids"] == 2
    assert report["summary"]["missing_person_id_count"] == 1
    assert report["summary"]["missing_person_id_count_before_type_normalization"] == 2
    assert report["summary"]["dtype_mismatch_reduced_by_normalization"] is True
    assert report["samples"]["missing_person_id_sample"] == ["200"]
    assert report["missing_person_ids"].loc[0, "sample_ev_id"] == "cars_2"

    by_home_lsoa = report["concentration"]["home_lsoa"]
    assert by_home_lsoa.loc[0, "home_lsoa"] == "E01000002"
    assert by_home_lsoa.loc[0, "missing_vehicle_rows"] == 1

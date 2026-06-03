from __future__ import annotations

import pandas as pd

from mobility.bus.annual_depot_preflight import run_preflight, summarize_block_input


def _blocks() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ("t1", "OP", "S1", "B1", "native", 8.0, 9.0, 51.0, -1.0, 51.1, -1.1, 10.0),
            ("t2", "OP", "S1", "B1", "native", 10.0, 11.0, 51.1, -1.1, 51.2, -1.2, 12.0),
        ],
        columns=[
            "trip_id",
            "agency_id",
            "service_id",
            "block_id",
            "block_source",
            "start_h",
            "end_h",
            "start_lat",
            "start_lon",
            "end_lat",
            "end_lon",
            "distance_km",
        ],
    )


def _inventory() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "EV_ID": ["bus_1", "bus_2", "car_1", "bad_bus"],
            "LSOA_code": ["E0101", "E0102", "E0103", "E0104"],
            "Model": ["A", "B", "C", "D"],
            "count": [5, 5, 99, 1],
            "Energy_kWh": [300.0, 90.0, 75.0, 100.0],
            "AC_Power_kW": [80.0, 22.0, 11.0, 50.0],
            "DC_Power_kW": [150.0, 50.0, 124.0, 50.0],
            "vehicle_subtype": ["bus", "minibus", "cars", "bus"],
            "efficiency_wh_per_km": [1200.0, 900.0, 150.0, 5000.0],
        }
    )


def test_preflight_detects_trip_level_blocks() -> None:
    summary = summarize_block_input(_blocks())
    assert summary["input_appears_trip_level"] is True
    assert summary["n_trip_rows"] == 2


def test_preflight_does_not_require_lsoa_in_raw_all_blocks() -> None:
    summary, _ = run_preflight(_blocks(), _inventory(), calendar_available=True, lsoa_attach_available=True)
    assert summary["has_start_lsoa"] is False
    assert summary["missing_block_columns"] == []


def test_preflight_filters_bus_minibus_only() -> None:
    summary, _ = run_preflight(_blocks(), _inventory(), calendar_available=True, lsoa_attach_available=True)
    assert summary["n_ev_rows_bus_minibus"] == 3
    assert summary["minibus_row_count"] == 1


def test_preflight_does_not_expand_count() -> None:
    summary, _ = run_preflight(_blocks(), _inventory(), calendar_available=True, lsoa_attach_available=True)
    assert summary["count_column_interpretation"] == "audit_only_not_expanded"
    assert summary["n_ev_specs_valid_after_sanity"] == 2


def test_preflight_reports_sanity_drops() -> None:
    summary, diagnostics = run_preflight(_blocks(), _inventory(), calendar_available=True, lsoa_attach_available=True)
    assert summary["n_ev_specs_dropped_by_sanity"] == 1
    assert "invalid_consumption_kwh_per_km" in set(diagnostics["drop_reason"])

from __future__ import annotations

import pandas as pd
import pytest

from mobility.bus.depot_only_preflight import attach_lsoas_to_templates, build_or_validate_block_templates
from mobility.bus.ev_bus_instances import build_ev_bus_instances


def _trip_blocks() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ("t1", "OP", "R1", "S1", "B1", "native", 8.0, 9.0, 10.0, "A", "B", 51.0, -1.0, 51.1, -1.1, "shape1"),
            ("t2", "OP", "R1", "S1", "B1", "native", 10.0, 11.0, 12.0, "B", "C", 51.1, -1.1, 51.2, -1.2, "shape2"),
        ],
        columns=[
            "trip_id",
            "agency_id",
            "route_id",
            "service_id",
            "block_id",
            "block_source",
            "start_h",
            "end_h",
            "distance_km",
            "start_stop",
            "end_stop",
            "start_lat",
            "start_lon",
            "end_lat",
            "end_lon",
            "shape_id",
        ],
    )


def _centroids() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "lsoa_code": ["E0001", "E0002", "E0003"],
            "easting_m": [0.0, 1.0, 2.0],
            "northing_m": [0.0, 1.0, 2.0],
            "lat": [51.0, 51.1, 51.2],
            "lon": [-1.0, -1.1, -1.2],
        }
    )


def _inventory() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "EV_ID": ["bus_1", "bus_2", "car_1", "bad_low"],
            "LSOA_code": ["E1", "E1", "E1", "E2"],
            "Model": ["A", "A", "C", "D"],
            "count": [2, 2, 1, 1],
            "Energy_kWh": [300.0, 300.0, 75.0, 250.0],
            "AC_Power_kW": [80.0, 80.0, 11.0, 50.0],
            "DC_Power_kW": [150.0, 150.0, 124.0, 100.0],
            "vehicle_subtype": ["bus", "minibus", "cars", "bus"],
            "efficiency_wh_per_km": [1000.0, 900.0, 150.0, 500.0],
        }
    )


def test_trip_level_input_builds_block_templates() -> None:
    templates, diag, summary = build_or_validate_block_templates(_trip_blocks())
    assert len(templates) == 1
    assert templates.loc[0, "passenger_distance_km"] == 22.0
    assert summary["block_template_build_mode"] == "trip_level_aggregated"
    assert diag.loc[0, "n_trip_rows"] == 2


def test_lsoa_attach_diagnostics() -> None:
    templates, _, _ = build_or_validate_block_templates(_trip_blocks())
    attached, diag = attach_lsoas_to_templates(templates, centroids=_centroids(), max_distance_km=1.0)
    assert attached.loc[0, "start_lsoa"] == "E0001"
    assert attached.loc[0, "end_lsoa"] == "E0003"
    assert diag.loc[0, "both_endpoint_lsoa_hit_rate"] == 1.0
    assert diag.loc[0, "centroid_fallback_endpoint_count"] >= 2


def test_missing_block_columns_detection() -> None:
    with pytest.raises(ValueError, match="missing required columns"):
        build_or_validate_block_templates(_trip_blocks().drop(columns=["distance_km"]))


def test_bus_minibus_filtering_each_row_and_no_count_expansion() -> None:
    instances, _, summary = build_ev_bus_instances(_inventory())
    assert len(instances) == 2
    assert summary["n_ev_rows_bus_minibus"] == 3
    assert summary["count_column_interpretation"] == "audit_only_not_expanded"


def test_low_consumption_drops_and_minibus_note() -> None:
    _, invalid, summary = build_ev_bus_instances(_inventory())
    assert "low_consumption_kwh_per_km" in set(invalid["drop_reason"])
    assert summary["minibus_valid_count"] == 1

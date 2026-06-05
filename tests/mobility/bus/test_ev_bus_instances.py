from __future__ import annotations

import pandas as pd

from mobility.bus.ev_bus_instances import build_ev_bus_instances


def _inventory() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "EV_ID": ["bus_1", "bus_2", "coach_1", "bad_low", "bad_batt"],
            "LSOA_code": ["E1", "E1", "E2", "E3", "E4"],
            "Model": ["A", "A", "C", "D", "E"],
            "count": [2, 2, 1, 1, 1],
            "Energy_kWh": [300.0, 300.0, 400.0, 250.0, 0.0],
            "AC_Power_kW": [80.0, 80.0, 80.0, 50.0, 50.0],
            "DC_Power_kW": [150.0, 150.0, 150.0, 100.0, 100.0],
            "vehicle_subtype": ["bus", "minibus", "coach", "bus", "bus"],
            "efficiency_wh_per_km": [1000.0, 900.0, 1000.0, 500.0, 1000.0],
            "energy_kWh_per_100km": [999.0, 999.0, 999.0, 999.0, 999.0],
        }
    )


def test_each_row_is_one_vehicle_instance_no_count_expansion() -> None:
    instances, _, summary = build_ev_bus_instances(_inventory())
    assert len(instances) == 2
    assert set(instances["vehicle_id"]) == {"bus_1", "bus_2"}
    assert summary["count_column_interpretation"] == "audit_only_not_expanded"
    assert summary["count_matches_lsoa_model_group_size"] is True
    assert instances["vehicle_instance_weight"].eq(1.0).all()


def test_filters_bus_minibus_and_excludes_coach() -> None:
    instances, _, summary = build_ev_bus_instances(_inventory())
    assert set(instances["vehicle_subtype"]) == {"bus", "minibus"}
    assert summary["n_ev_rows_bus_minibus"] == 4


def test_uses_efficiency_wh_per_km_before_100km_fallback() -> None:
    instances, _, _ = build_ev_bus_instances(_inventory())
    assert instances.loc[instances["vehicle_id"].eq("bus_1"), "consumption_kwh_per_km"].iloc[0] == 1.0


def test_invalid_rows_report_low_consumption_and_battery() -> None:
    _, invalid, summary = build_ev_bus_instances(_inventory())
    assert {"low_consumption_kwh_per_km", "invalid_battery_kwh"}.issubset(set(invalid["drop_reason"]))
    assert summary["low_consumption_filtered_count"] == 1
    assert summary["invalid_battery_vehicle_count"] == 1


def test_minibus_count_note() -> None:
    _, _, summary = build_ev_bus_instances(_inventory().assign(vehicle_subtype=["bus", "bus", "coach", "bus", "bus"]))
    assert summary["minibus_count_note"] == "minibus count is 0"

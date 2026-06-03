from __future__ import annotations

import pandas as pd

from mobility.bus.annual_ev_specs import build_ev_bus_specs


def _inventory() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "EV_ID": ["bus_1", "mini_1", "car_1", "bad_1"],
            "LSOA_code": ["E1", "E2", "E3", "E4"],
            "Model": ["A", "B", "C", "D"],
            "count": [10, 20, 30, 40],
            "Energy_kWh": [300.0, 120.0, 75.0, -1.0],
            "AC_Power_kW": [80.0, 22.0, 11.0, 50.0],
            "DC_Power_kW": [150.0, 50.0, 124.0, 50.0],
            "vehicle_subtype": ["bus", "minibus", "cars", "bus"],
            "efficiency_wh_per_km": [1200.0, 900.0, 150.0, 1000.0],
        }
    )


def test_ev_specs_filter_bus_minibus_only() -> None:
    specs, diagnostics = build_ev_bus_specs(_inventory())
    assert set(diagnostics["vehicle_subtype"]) == {"bus", "minibus"}
    assert set(specs["vehicle_subtype"]) == {"bus", "minibus"}


def test_ev_specs_do_not_expand_count() -> None:
    specs, _ = build_ev_bus_specs(_inventory())
    assert len(specs) == 2


def test_consumption_unit_conversion() -> None:
    specs, _ = build_ev_bus_specs(_inventory())
    assert specs.set_index("source_ev_id").loc["bus_1", "consumption_kwh_per_km"] == 1.2


def test_invalid_specs_are_dropped() -> None:
    specs, diagnostics = build_ev_bus_specs(_inventory())
    assert "bad_1" not in set(specs["source_ev_id"])
    assert diagnostics.set_index("source_ev_id").loc["bad_1", "drop_reason"] == "invalid_battery_kwh"


def test_spec_fields_present() -> None:
    specs, _ = build_ev_bus_specs(_inventory())
    required = {"vehicle_spec_id", "battery_kwh", "consumption_kwh_per_km", "ac_charge_kw_max", "dc_charge_kw_max"}
    assert required.issubset(specs.columns)

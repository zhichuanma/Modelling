from __future__ import annotations

import pandas as pd
import pytest

from mobility.bus.depot_only_soc import apply_depot_only_soc


def _events(distance: float = 10.0) -> pd.DataFrame:
    base = {
        "simulation_case_id": "case_1",
        "service_date": "2026-06-03",
        "vehicle_id": "bus_1",
        "vehicle_model": "A",
        "vehicle_subtype": "bus",
        "block_template_id": "bt1",
        "block_id": "B1",
        "agency_id": "OP",
        "depot_id": "opdepot_OP_E1",
        "operational_depot_lsoa": "E1",
        "region_key": "London",
        "sample_mode": "full_ev_inventory",
        "weighting_mode": "unweighted_ev_stock_scenario",
        "battery_kwh": 100.0,
        "consumption_kwh_per_km": 1.0,
        "ac_charge_kw_max": 40.0,
        "dc_charge_kw_max": 150.0,
        "usable_soc_min": 0.10,
        "usable_soc_max": 0.95,
        "depot_power_kw": 100.0,
        "depot_power_source": "fixed_default_100kw",
    }
    return pd.DataFrame(
        [
            {
                **base,
                "event_seq": 0,
                "event_type": "passenger_block",
                "start_datetime": pd.Timestamp("2026-06-03 08:00"),
                "end_datetime": pd.Timestamp("2026-06-03 09:00"),
                "duration_min": 60.0,
                "distance_km": distance,
                "can_charge": False,
                "charge_power_kw": 0.0,
            },
            {
                **base,
                "event_seq": 1,
                "event_type": "depot_parking_post",
                "start_datetime": pd.Timestamp("2026-06-03 23:00"),
                "end_datetime": pd.Timestamp("2026-06-04 06:00"),
                "duration_min": 420.0,
                "distance_km": 0.0,
                "can_charge": True,
                "charge_power_kw": 100.0,
            },
        ]
    )


def test_initial_soc_usable_upper_and_ac_power_cap() -> None:
    events, _ = apply_depot_only_soc(_events(distance=10.0), depot_power_kw=100.0)
    assert events.loc[0, "soc_start_kwh"] == 95.0
    assert events.loc[1, "charge_power_kw"] == 40.0


def test_driving_energy_and_depot_charge() -> None:
    events, summary = apply_depot_only_soc(_events(distance=20.0), depot_power_kw=100.0)
    assert events.loc[0, "energy_kwh"] == 20.0
    assert summary.loc[0, "total_charge_kwh"] == pytest.approx(20.0)


def test_soc_not_clamped_and_shortfall_reported() -> None:
    _, summary = apply_depot_only_soc(_events(distance=140.0), depot_power_kw=100.0)
    assert summary.loc[0, "min_soc_kwh"] < 0.0
    assert summary.loc[0, "energy_shortfall_kwh"] > 0.0
    assert bool(summary.loc[0, "breaches_zero_soc"])


def test_depot_only_feasible_uses_usable_min_threshold() -> None:
    _, summary = apply_depot_only_soc(_events(distance=90.0), depot_power_kw=100.0)
    assert bool(summary.loc[0, "depot_only_feasible"]) is False
    assert bool(summary.loc[0, "breaches_usable_min_soc"]) is True


def test_infeasible_case_retained_with_reason() -> None:
    _, summary = apply_depot_only_soc(_events(distance=140.0), depot_power_kw=100.0)
    assert len(summary) == 1
    assert summary.loc[0, "infeasibility_reason"] != ""

from __future__ import annotations

import pandas as pd
import pytest

from mobility.bus.annual_depot_soc import apply_depot_only_soc


def _events(distance: float = 400.0) -> pd.DataFrame:
    base = {
        "service_date": "2026-04-17",
        "vehicle_day_id": "vd1",
        "vehicle_spec_id": "ev1",
        "block_instance_id": "bi1",
        "block_template_id": "bt1",
        "depot_id": "opdepot_OP_E1",
        "depot_lsoa": "E1",
        "battery_kwh": 100.0,
        "consumption_kwh_per_km": 1.0,
        "ac_charge_kw_max": 40.0,
        "usable_soc_min": 0.10,
        "usable_soc_max": 0.95,
        "scenario_mode": "ev_stock_scale",
    }
    return pd.DataFrame(
        [
            {**base, "event_seq": 0, "event_type": "passenger_block", "start_datetime": pd.Timestamp("2026-04-17 08:00"), "end_datetime": pd.Timestamp("2026-04-17 09:00"), "duration_min": 60.0, "distance_km": distance, "can_charge": False, "charge_power_kw": 0.0},
            {**base, "event_seq": 1, "event_type": "depot_parking_overnight", "start_datetime": pd.Timestamp("2026-04-17 23:00"), "end_datetime": pd.Timestamp("2026-04-18 06:00"), "duration_min": 420.0, "distance_km": 0.0, "can_charge": True, "charge_power_kw": 100.0},
        ]
    )


def test_depot_charging_uses_ac_power() -> None:
    events, _ = apply_depot_only_soc(_events(distance=10.0), depot_power_kw=100.0)
    charge = events.loc[events["event_type"].eq("depot_parking_overnight"), "charge_power_kw"].iloc[0]
    assert charge == 40.0


def test_no_public_charging_used() -> None:
    events, _ = apply_depot_only_soc(_events(distance=10.0), depot_power_kw=100.0)
    assert "public_charger_event" not in set(events["event_type"])


def test_soc_not_clamped_below_zero() -> None:
    _, summary = apply_depot_only_soc(_events(distance=400.0), depot_power_kw=100.0)
    assert summary.loc[0, "min_soc_kwh"] < 0.0


def test_overnight_charging_adds_energy_after_midnight() -> None:
    events, _ = apply_depot_only_soc(_events(distance=10.0), depot_power_kw=100.0)
    charging = events.loc[events["event_type"].eq("depot_parking_overnight")].iloc[0]
    assert charging["charge_kwh_added"] == pytest.approx(10.0)
    assert charging["charging_end_datetime"] > charging["start_datetime"]


def test_infeasible_vehicle_day_reports_shortfall() -> None:
    _, summary = apply_depot_only_soc(_events(distance=400.0), depot_power_kw=100.0)
    assert bool(summary.loc[0, "depot_only_feasible"]) is False
    assert summary.loc[0, "energy_shortfall_kwh"] > 0.0


def test_vehicle_day_soc_summary_has_required_columns() -> None:
    _, summary = apply_depot_only_soc(_events(distance=10.0), depot_power_kw=100.0)
    required = {"min_soc_kwh", "min_soc_pct", "energy_shortfall_kwh", "depot_only_feasible", "infeasibility_reason"}
    assert required.issubset(summary.columns)

from __future__ import annotations

import pandas as pd
import pytest

from mobility.bus.annual_depot_load import aggregate_depot_load_15min, depot_load_energy_matches_events


def _registry() -> pd.DataFrame:
    return pd.DataFrame({"depot_id": ["D1"], "depot_lat": [51.0], "depot_lon": [-1.0], "depot_confidence": ["high"]})


def _events() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "service_date": "2026-04-17",
                "vehicle_day_id": "vd1",
                "depot_id": "D1",
                "depot_lsoa": "E1",
                "event_type": "depot_parking_overnight",
                "start_datetime": pd.Timestamp("2026-04-17 23:30"),
                "end_datetime": pd.Timestamp("2026-04-18 00:30"),
                "charging_end_datetime": pd.NaT,
                "charge_kwh_added": 60.0,
                "scenario_mode": "ev_stock_scale",
            },
            {
                "service_date": "2026-04-17",
                "vehicle_day_id": "vd2",
                "depot_id": "D1",
                "depot_lsoa": "E1",
                "event_type": "public_charger_event",
                "start_datetime": pd.Timestamp("2026-04-17 12:00"),
                "end_datetime": pd.Timestamp("2026-04-17 12:30"),
                "charging_end_datetime": pd.NaT,
                "charge_kwh_added": 999.0,
                "scenario_mode": "ev_stock_scale",
            },
        ]
    )


def test_15min_load_only_uses_depot_charging_events() -> None:
    load, _ = aggregate_depot_load_15min(_events(), _registry())
    assert load["charge_kwh"].sum() == pytest.approx(60.0)


def test_charging_event_split_across_slots_by_overlap() -> None:
    event = _events().iloc[:1].copy()
    event.loc[:, "start_datetime"] = pd.Timestamp("2026-04-17 18:10")
    event.loc[:, "end_datetime"] = pd.Timestamp("2026-04-17 18:40")
    event.loc[:, "charge_kwh_added"] = 30.0
    load, _ = aggregate_depot_load_15min(event, _registry())
    assert len(load) == 3
    assert sorted(load["charge_kwh"].round(6).tolist()) == [5.0, 10.0, 15.0]


def test_cross_midnight_slots_have_correct_slot_date() -> None:
    load, _ = aggregate_depot_load_15min(_events(), _registry())
    assert {"2026-04-17", "2026-04-18"}.issubset(set(load["slot_date"]))


def test_depot_load_energy_matches_event_ledger() -> None:
    load, _ = aggregate_depot_load_15min(_events(), _registry())
    assert depot_load_energy_matches_events(load, _events())


def test_depot_load_energy_match_allows_relative_float_noise() -> None:
    events = _events().iloc[:1].copy()
    events.loc[:, "charge_kwh_added"] = 32_583.0
    load = pd.DataFrame({"charge_kwh": [32_583.0 - 1.4e-6]})

    assert depot_load_energy_matches_events(load, events)


def test_depot_load_energy_match_rejects_real_mismatch() -> None:
    events = _events().iloc[:1].copy()
    load = pd.DataFrame({"charge_kwh": [59.0]})

    assert not depot_load_energy_matches_events(load, events)


def test_depot_daily_summary_has_required_columns() -> None:
    load, daily = aggregate_depot_load_15min(_events(), _registry())
    required = {"daily_charge_kwh", "daily_peak_kw", "n_charging_vehicles", "share_infeasible_vehicle_days"}
    assert not load.empty
    assert required.issubset(daily.columns)

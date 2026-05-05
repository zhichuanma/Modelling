from __future__ import annotations

from pathlib import Path

import numpy as np

from mobility.coach.coach_fleet import load_coach_fleet, sample_coach_ev


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "coach_fleet_minimal.csv"


def test_load_coach_fleet_filters_to_simulatable_coaches() -> None:
    fleet = load_coach_fleet(FIXTURE)

    models = set(fleet["Model"])
    assert {"YUTONG TC12", "YUTONG GTE14"}.issubset(models)
    assert "YUTONG TC9" not in models
    assert "vehicle_subtype" not in fleet.columns
    assert (fleet["Energy_kWh"] > 0.0).all()
    assert (fleet["consumption_kwh_per_km"] > 0.0).all()


def test_sample_coach_ev_uses_simulatable_weighted_rows() -> None:
    fleet = load_coach_fleet(FIXTURE)
    rng_a = np.random.default_rng(20260501)
    rng_b = np.random.default_rng(20260501)

    sample_a = sample_coach_ev(fleet, rng_a, weight_by_count=True)
    sample_b = sample_coach_ev(fleet, rng_b, weight_by_count=True)

    assert sample_a.equals(sample_b)
    assert sample_a["Model"] in set(fleet["Model"])
    assert sample_a["Energy_kWh"] > 0.0
    assert sample_a["consumption_kwh_per_km"] > 0.0
    assert "count" in sample_a

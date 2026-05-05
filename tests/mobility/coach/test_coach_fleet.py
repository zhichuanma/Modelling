from __future__ import annotations

import numpy as np

from mobility.coach.coach_fleet import load_coach_fleet, sample_coach_ev


def test_load_coach_fleet_includes_yutong_coach_models() -> None:
    fleet = load_coach_fleet()

    models = set(fleet["model"])
    assert {"YUTONG TC12", "YUTONG GTE14", "YUTONG TC9"}.issubset(models)
    assert set(fleet["vehicle_subtype"]) == {"coach"}
    assert fleet["is_simulatable"].any()


def test_sample_coach_ev_uses_simulatable_weighted_rows() -> None:
    fleet = load_coach_fleet()
    rng_a = np.random.default_rng(20260501)
    rng_b = np.random.default_rng(20260501)

    sample_a = sample_coach_ev(rng_a, weight_by_count=True, fleet=fleet)
    sample_b = sample_coach_ev(rng_b, weight_by_count=True, fleet=fleet)

    assert sample_a == sample_b
    assert sample_a["model"] in set(fleet.loc[fleet["is_simulatable"], "model"])
    assert sample_a["battery_kwh"] > 0.0
    assert sample_a["consumption_kwh_per_km"] > 0.0
    assert "count" in sample_a

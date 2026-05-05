from __future__ import annotations

import numpy as np
import pandas as pd

from mobility.coach.selection import (
    render_journey_identity_card,
    sample_contrast_journey,
    sample_protagonist_journey,
)


def _journeys() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "journey_id": ["a", "b", "c", "d", "e", "f", "g", "h"],
            "vehicle_journey_code": ["A", "B", "C", "D", "E", "F", "G", "H"],
            "operator_name": ["BHAT", "FLIX", "MEGA", "NATX", "PKOH", "SCLK", "BHAT", "FLIX"],
            "operator_code": ["BHAT", "FLIX", "MEGA", "NATX", "PKOH", "SCLK", "BHAT", "FLIX"],
            "line_name": ["1", "2", "3", "4", "5", "6", "7", "8"],
            "departure_time": ["08:00:00"] * 8,
            "arrival_time": ["09:00:00"] * 8,
            "runtime_min": [60, 120, 180, 240, 300, 360, 45, 90],
            "distance_km": [50.0, 150.0, 300.0, 500.0, 80.0, 220.0, 70.0, None],
            "distance_source": [
                "haversine_x_detour",
                "haversine_x_detour",
                "haversine_x_detour",
                "haversine_x_detour",
                "haversine_x_detour",
                "haversine_x_detour",
                "haversine_x_detour",
                "unknown",
            ],
            "has_cross_midnight": [False, False, False, False, False, False, True, False],
        }
    )


def test_protagonist_selection_applies_runtime_distance_and_midnight_filters() -> None:
    rng = np.random.default_rng(20260501)

    protagonist = sample_protagonist_journey(_journeys(), rng)
    contrast = sample_contrast_journey(_journeys(), rng, protagonist)

    assert 1.0 <= protagonist["runtime_min"] / 60.0 <= 8.0
    assert protagonist["distance_source"] == "haversine_x_detour"
    assert not protagonist["has_cross_midnight"]
    assert contrast["journey_id"] != protagonist["journey_id"]
    distance_gap = abs(contrast["distance_km"] - protagonist["distance_km"]) / max(protagonist["distance_km"], 1.0)
    assert distance_gap >= 0.5


def test_selection_keeps_all_operators_eligible_over_repeated_draws() -> None:
    journeys = _journeys()
    rng = np.random.default_rng(20260501)
    operators = {
        sample_protagonist_journey(journeys, rng)["operator_code"]
        for _ in range(200)
    }

    assert len(operators) >= 5


def test_render_identity_card_contains_required_fields() -> None:
    row = _journeys().iloc[0]
    ev = {"Model": "YUTONG TC12", "Energy_kWh": 281.0, "consumption_kwh_per_km": 0.9}

    card = render_journey_identity_card(row, ev, wall_clock_s=0.2)

    assert card.shape[1] >= 13
    assert card.loc[0, "operator"] == "BHAT"
    assert card.loc[0, "EV model"] == "YUTONG TC12"
    assert "feasible_single_charge" in card.columns
    assert "wall-clock time" in card.columns

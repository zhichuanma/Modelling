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
            "journey_id": ["a", "b", "c", "d"],
            "vehicle_journey_code": ["A", "B", "C", "D"],
            "operator_name": ["Op"] * 4,
            "line_name": ["1", "2", "3", "4"],
            "departure_time": ["08:00:00"] * 4,
            "arrival_time": ["09:00:00"] * 4,
            "runtime_min": [60, 60, 60, 60],
            "distance_km": [50.0, None, 70.0, 80.0],
            "distance_source": ["haversine_x1.30", "unknown", "haversine_x1.30", "haversine_x1.30"],
            "has_cross_midnight": [False, False, True, False],
        }
    )


def test_random_selection_only_keeps_known_non_cross_midnight() -> None:
    rng = np.random.default_rng(10)

    protagonist = sample_protagonist_journey(_journeys(), rng)
    contrast = sample_contrast_journey(_journeys(), rng, protagonist)

    assert protagonist["journey_id"] in {"a", "d"}
    assert contrast["journey_id"] in {"a", "d"}
    assert contrast["journey_id"] != protagonist["journey_id"]


def test_render_identity_card_contains_required_fields() -> None:
    row = _journeys().iloc[0]
    ev = {"model": "YUTONG TC12", "battery_kwh": 281.0, "consumption_kwh_per_km": 0.9}

    card = render_journey_identity_card(row, ev, wall_clock_s=0.2)

    assert card.shape[1] >= 13
    assert card.loc[0, "operator"] == "Op"
    assert card.loc[0, "EV model"] == "YUTONG TC12"
    assert "feasible_single_charge" in card.columns
    assert "wall-clock time" in card.columns

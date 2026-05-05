from __future__ import annotations

import pandas as pd
import pytest

from mobility.coach.distance import haversine_km, vehicle_journey_distance_km


def test_haversine_distance_estimate_uses_detour() -> None:
    stops = pd.DataFrame(
        {
            "stop_point_ref": ["A", "B", "C"],
            "lat": [51.5, 52.0, 52.5],
            "lon": [-0.1, -0.2, -0.3],
        }
    )
    seq = pd.DataFrame({"stop_sequence": [1, 2, 3], "stop_point_ref": ["A", "B", "C"]})

    distance, source = vehicle_journey_distance_km(seq, stops, road_detour_factor=1.3)

    expected = (haversine_km(51.5, -0.1, 52.0, -0.2) + haversine_km(52.0, -0.2, 52.5, -0.3)) * 1.3
    assert distance == pytest.approx(expected)
    assert source == "haversine_x1.30"


def test_missing_coordinate_marks_unknown_distance() -> None:
    stops = pd.DataFrame({"stop_point_ref": ["A", "B"], "lat": [51.5, None], "lon": [-0.1, -0.2]})
    seq = pd.DataFrame({"stop_sequence": [1, 2], "stop_point_ref": ["A", "B"]})

    distance, source = vehicle_journey_distance_km(seq, stops)

    assert distance is None
    assert source == "unknown"

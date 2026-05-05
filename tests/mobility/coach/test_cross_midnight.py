from __future__ import annotations

import pandas as pd
import pytest

from mobility.coach.trip_chain_coach import journey_to_daily_schedules


def test_cross_midnight_journey_splits_energy_by_duration() -> None:
    row = pd.Series(
        {
            "vehicle_journey_code": "VJX",
            "start_h": 23.0,
            "end_h": 25.0,
            "distance_km": 120.0,
            "distance_source": "haversine_x_detour",
        }
    )
    stops = pd.DataFrame({"stop_sequence": [1, 2], "stop_point_ref": ["A", "B"]})

    schedules = journey_to_daily_schedules(row, stops, consumption_kwh_per_km=1.5, terminus_charge_kw=50.0)

    assert [schedule.day for schedule in schedules] == [0, 1]
    assert schedules[0].trips[0].distance_km == pytest.approx(60.0)
    assert schedules[1].trips[0].distance_km == pytest.approx(60.0)
    assert schedules[1].trips[0].energy_consumed_kwh == pytest.approx(90.0)
    assert any(event.location_purpose == "terminus_dwell" for event in schedules[0].parking_events)
    assert any(event.location_purpose == "terminus_dwell" for event in schedules[1].parking_events)

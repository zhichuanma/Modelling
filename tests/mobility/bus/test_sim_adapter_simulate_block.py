from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from mobility.bus.sim_adapter import simulate_block
from mobility.core.constants import STEP_HOURS_DECISION


def _five_trip_block() -> pd.DataFrame:
    rows = [(7, 8), (9, 10), (11, 12), (14, 15), (16, 17)]
    return pd.DataFrame(
        [
            (f"t{i}", "OP", "R1", "S1", 0, "B1", "native", start, end, 8.0, f"S{i}", f"S{i+1}", 51.0, -1.0, 51.1, -1.1, "shape")
            for i, (start, end) in enumerate(rows)
        ],
        columns=[
            "trip_id", "agency_id", "route_id", "service_id", "direction_id", "block_id",
            "block_source", "start_h", "end_h", "distance_km", "start_stop", "end_stop",
            "start_lat", "start_lon", "end_lat", "end_lon", "shape_id",
        ],
    )


def _slice(values: np.ndarray, start_h: float, end_h: float) -> np.ndarray:
    start = int(np.floor(start_h / STEP_HOURS_DECISION))
    end = int(np.ceil(end_h / STEP_HOURS_DECISION))
    return values[start:end]


def test_simulate_block_energy_accounting_and_soc_direction() -> None:
    result = simulate_block(
        _five_trip_block(),
        battery_kwh=120.0,
        consumption_kwh_per_km=1.0,
        depot_charge_kw=60.0,
        soc_init=0.6,
        allow_layover_charging=True,
        layover_charge_kw=30.0,
        min_layover_for_charging_h=0.5,
    )
    soc = result["soc"]

    assert result["energy_charged_kwh"] == pytest.approx(
        result["depot_kwh"] + result["layover_kwh"],
        abs=1e-3,
    )
    assert result["soc_min"] == float(soc.min())

    schedule = result["schedules"][0]
    for trip in schedule.trips:
        segment = _slice(soc, trip.departure_time, trip.arrival_time)
        assert np.all(np.diff(segment) <= 1e-9)
    for event in schedule.parking_events:
        if event.can_charge and event.duration_hours > 0:
            segment = _slice(soc, event.start_time, event.end_time)
            assert np.all(np.diff(segment) >= -1e-9)

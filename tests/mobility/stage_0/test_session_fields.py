"""Stage 0 coverage for session-level parking-event fields."""

from __future__ import annotations

from copy import deepcopy
import importlib
import importlib.util
from pathlib import Path

import numpy as np
import pytest

data_structures = importlib.import_module("mobility.core.data_structures")
simulator = importlib.import_module("mobility.core.simulator")

DailySchedule = data_structures.DailySchedule
ParkingEvent = data_structures.ParkingEvent
Trip = data_structures.Trip
compute_next_trip_soc_floor = simulator.compute_next_trip_soc_floor
simulate_single_ev = simulator.simulate_single_ev

bit_identical_spec = importlib.util.spec_from_file_location(
    "stage0_bit_identical_helpers",
    Path(__file__).with_name("test_bit_identical.py"),
)
bit_identical_module = importlib.util.module_from_spec(bit_identical_spec)
assert bit_identical_spec is not None
assert bit_identical_spec.loader is not None
bit_identical_spec.loader.exec_module(bit_identical_module)
build_minimal_fleet = bit_identical_module.build_minimal_fleet


def _interp_soc(day_soc: np.ndarray, soc_start: float, time_hours: float) -> float:
    soc_points = np.concatenate((np.array([soc_start], dtype=float), day_soc))
    grid_minutes = np.linspace(0.0, 24.0 * 60.0, num=soc_points.shape[0])
    return float(np.interp(time_hours * 60.0, grid_minutes, soc_points))


def test_session_fields_are_populated_and_energy_is_conserved() -> None:
    fleet = build_minimal_fleet()

    for payload in fleet.values():
        schedules = deepcopy(payload["schedules"])
        soc_all, load_all, _soc_after_warmup = simulate_single_ev(
            schedules,
            payload["battery_capacity_kwh"],
            warm_up_days=0,
        )
        day_steps = 96

        for day_index, schedule in enumerate(schedules):
            start = day_index * day_steps
            end = start + day_steps
            day_soc = soc_all[start:end]
            day_load = load_all[start:end]
            soc_start = 1.0 if day_index == 0 else float(soc_all[start - 1])
            load_step_hours = 24.0 / len(day_load)

            total_charged_kwh = sum(
                parking_event.energy_charged_kwh
                for parking_event in schedule.parking_events
            )
            total_load_kwh = float(np.sum(day_load) * load_step_hours)
            assert abs(total_charged_kwh - total_load_kwh) < 1e-6

            for parking_event in schedule.parking_events:
                assert 0.0 <= parking_event.soc_on_arrival <= 1.0
                assert 0.0 <= parking_event.soc_on_departure <= 1.0
                assert parking_event.energy_charged_kwh >= 0.0
                assert 0.0 <= parking_event.soc_min_required <= 1.0
                assert parking_event.soc_on_departure >= parking_event.soc_on_arrival

                expected_arrival_soc = _interp_soc(
                    day_soc,
                    soc_start,
                    parking_event.start_time,
                )
                assert abs(parking_event.soc_on_arrival - expected_arrival_soc) < 1e-9


def test_next_trip_soc_floor_stops_at_next_home_event() -> None:
    schedule = DailySchedule(
        ev_id="floor_case",
        day=0,
        day_type="weekday",
        trips=[
            Trip(
                trip_id="t0",
                departure_time=8.0,
                arrival_time=8.5,
                distance_km=10.0,
                origin_purpose="home",
                destination_purpose="work",
                energy_consumed_kwh=5.0,
            ),
            Trip(
                trip_id="t1",
                departure_time=12.0,
                arrival_time=12.5,
                distance_km=8.0,
                origin_purpose="work",
                destination_purpose="shopping",
                energy_consumed_kwh=4.0,
            ),
            Trip(
                trip_id="t2",
                departure_time=17.0,
                arrival_time=17.5,
                distance_km=6.0,
                origin_purpose="shopping",
                destination_purpose="home",
                energy_consumed_kwh=3.0,
            ),
        ],
        parking_events=[
            ParkingEvent(
                start_time=0.0,
                end_time=8.0,
                duration_hours=8.0,
                location_purpose="home",
            ),
            ParkingEvent(
                start_time=8.5,
                end_time=12.0,
                duration_hours=3.5,
                location_purpose="work",
            ),
            ParkingEvent(
                start_time=12.5,
                end_time=17.0,
                duration_hours=4.5,
                location_purpose="shopping",
            ),
            ParkingEvent(
                start_time=17.5,
                end_time=24.0,
                duration_hours=6.5,
                location_purpose="home",
            ),
        ],
    )

    compute_next_trip_soc_floor(schedule, battery_kwh=20.0, safety=0.05)

    expected_floors = [0.65, 0.4, 0.2, 0.05]
    actual_floors = [parking_event.soc_min_required for parking_event in schedule.parking_events]
    assert actual_floors == pytest.approx(expected_floors)

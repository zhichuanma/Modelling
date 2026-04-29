"""Stage 3 coverage for SOC warm-up burn-in and output trimming."""

from __future__ import annotations

from copy import deepcopy
import importlib

import numpy as np
import pandas as pd
import pytest

constants = importlib.import_module("mobility.core.constants")
data_structures = importlib.import_module("mobility.core.data_structures")
simulator = importlib.import_module("mobility.core.simulator")

DailySchedule = data_structures.DailySchedule
ParkingEvent = data_structures.ParkingEvent
Trip = data_structures.Trip
STEPS_PER_DAY = constants.STEPS_PER_DAY_DECISION
simulate_fleet = simulator.simulate_fleet
simulate_single_day = simulator.simulate_single_day
simulate_single_ev = simulator.simulate_single_ev


def _build_daily_schedule(day: int) -> DailySchedule:
    consumption_kwh_per_km = 0.20
    out_distance_km = 25.0
    back_distance_km = 25.0

    trips = [
        Trip(
            trip_id=f"ev_template_d{day}_t0",
            departure_time=8.0,
            arrival_time=9.0,
            distance_km=out_distance_km,
            origin_purpose="home",
            destination_purpose="work",
            energy_consumed_kwh=out_distance_km * consumption_kwh_per_km,
        ),
        Trip(
            trip_id=f"ev_template_d{day}_t1",
            departure_time=17.0,
            arrival_time=18.0,
            distance_km=back_distance_km,
            origin_purpose="work",
            destination_purpose="home",
            energy_consumed_kwh=back_distance_km * consumption_kwh_per_km,
        ),
    ]
    parking_events = [
        ParkingEvent(
            start_time=0.0,
            end_time=8.0,
            duration_hours=8.0,
            location_purpose="home",
            can_charge=True,
            charge_power_kw=7.0,
        ),
        ParkingEvent(
            start_time=9.0,
            end_time=17.0,
            duration_hours=8.0,
            location_purpose="work",
            can_charge=False,
            charge_power_kw=0.0,
        ),
        ParkingEvent(
            start_time=18.0,
            end_time=24.0,
            duration_hours=6.0,
            location_purpose="home",
            can_charge=True,
            charge_power_kw=7.0,
        ),
    ]
    return DailySchedule(
        ev_id="ev_template",
        day=day,
        day_type="weekend" if (day % 7) >= 5 else "weekday",
        trips=trips,
        parking_events=parking_events,
    )


def _build_schedules(num_days: int = 20, *, ev_id: str = "ev_template") -> list[DailySchedule]:
    template = _build_daily_schedule(day=0)
    schedules: list[DailySchedule] = []
    for day in range(num_days):
        schedule = deepcopy(template)
        schedule.ev_id = ev_id
        schedule.day = day
        schedule.day_type = "weekend" if (day % 7) >= 5 else "weekday"
        for trip_index, trip in enumerate(schedule.trips):
            trip.trip_id = f"{ev_id}_d{day}_t{trip_index}"
        schedules.append(schedule)
    return schedules


def _manual_multi_day_run(
    schedules: list[DailySchedule],
    battery_capacity_kwh: float,
    *,
    soc_init: float,
) -> tuple[np.ndarray, np.ndarray]:
    soc_all = np.empty(len(schedules) * STEPS_PER_DAY)
    load_all = np.empty(len(schedules) * STEPS_PER_DAY)
    soc = soc_init

    for day_index, schedule in enumerate(schedules):
        start = day_index * STEPS_PER_DAY
        end = start + STEPS_PER_DAY
        soc_day, load_day, soc = simulate_single_day(
            schedule,
            battery_capacity_kwh,
            soc_start=soc,
        )
        soc_all[start:end] = soc_day
        load_all[start:end] = load_day

    return soc_all, load_all


def test_warmup_zero_passthrough() -> None:
    battery_capacity_kwh = 60.0
    soc_init = 1.0

    schedules_manual = _build_schedules(20)
    expected_soc, expected_load = _manual_multi_day_run(
        schedules_manual,
        battery_capacity_kwh,
        soc_init=soc_init,
    )

    schedules_sim = _build_schedules(20)
    observed_soc, observed_load, soc_after_warmup = simulate_single_ev(
        schedules_sim,
        battery_capacity_kwh,
        soc_init=soc_init,
        warm_up_days=0,
    )

    assert len(observed_soc) == 20 * STEPS_PER_DAY
    assert len(observed_load) == 20 * STEPS_PER_DAY
    assert np.allclose(observed_soc, expected_soc, atol=1e-12)
    assert np.allclose(observed_load, expected_load, atol=1e-12)
    assert soc_after_warmup == soc_init


def test_warmup_trims_output() -> None:
    soc_post, load_post, _soc_after_warmup = simulate_single_ev(
        _build_schedules(20),
        60.0,
        warm_up_days=14,
    )

    assert len(soc_post) == 6 * STEPS_PER_DAY
    assert len(load_post) == 6 * STEPS_PER_DAY


def test_soc_after_warmup_matches_profile() -> None:
    battery_capacity_kwh = 60.0
    schedules_full = _build_schedules(20)
    soc_all_full, _load_all_full = _manual_multi_day_run(
        schedules_full,
        battery_capacity_kwh,
        soc_init=1.0,
    )

    schedules_warm = _build_schedules(20)
    _soc_post, _load_post, soc_after_warmup = simulate_single_ev(
        schedules_warm,
        battery_capacity_kwh,
        warm_up_days=14,
    )

    assert soc_after_warmup == float(soc_all_full[(14 * STEPS_PER_DAY) - 1])


def test_warmup_convergence() -> None:
    battery_capacity_kwh = 60.0
    soc_after_values = []

    for soc_init in [0.3, 0.5, 1.0]:
        _soc_post, _load_post, soc_after_warmup = simulate_single_ev(
            _build_schedules(20),
            battery_capacity_kwh,
            soc_init=soc_init,
            warm_up_days=14,
        )
        soc_after_values.append(soc_after_warmup)

    assert float(np.std(soc_after_values)) < 0.05


def test_warmup_exceeds_schedules() -> None:
    schedules = _build_schedules(20)

    with pytest.raises(ValueError):
        simulate_single_ev(schedules, 60.0, warm_up_days=20)

    with pytest.raises(ValueError):
        simulate_single_ev(schedules, 60.0, warm_up_days=-1)


def test_fleet_returns_soc_after_warmup() -> None:
    fleet_schedules = {
        "ev_1": _build_schedules(20, ev_id="ev_1"),
        "ev_2": _build_schedules(20, ev_id="ev_2"),
    }
    ev_fleet = pd.DataFrame(
        {
            "EV_ID": ["ev_1", "ev_2"],
            "battery_capacity_kwh": [60.0, 75.0],
        }
    )

    results = simulate_fleet(
        fleet_schedules,
        ev_fleet,
        warm_up_days=14,
    )

    for ev_id in ["ev_1", "ev_2"]:
        assert set(results[ev_id]) == {"soc", "load", "soc_after_warmup"}
        assert len(results[ev_id]["soc"]) == 6 * STEPS_PER_DAY
        assert len(results[ev_id]["load"]) == 6 * STEPS_PER_DAY
        assert isinstance(results[ev_id]["soc_after_warmup"], float)
        assert 0.0 <= results[ev_id]["soc_after_warmup"] <= 1.0


def test_session_fields_filled_including_warmup() -> None:
    schedules = _build_schedules(20)
    warmup_schedule = schedules[0]
    chargeable_event = warmup_schedule.parking_events[0]
    assert chargeable_event.soc_on_arrival == 0.0

    _soc_post, _load_post, _soc_after_warmup = simulate_single_ev(
        schedules,
        60.0,
        soc_init=0.5,
        warm_up_days=14,
    )

    assert chargeable_event.soc_on_arrival != 0.0
    assert chargeable_event.soc_on_departure != 0.0
    assert chargeable_event.energy_charged_kwh >= 0.0
    assert chargeable_event.soc_min_required >= 0.0

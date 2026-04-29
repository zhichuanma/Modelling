"""Stage 0 regression coverage for zero behavior change."""

from __future__ import annotations

from copy import deepcopy
import importlib
from typing import Dict, List

import numpy as np

data_structures = importlib.import_module("mobility.core.data_structures")
simulator = importlib.import_module("mobility.core.simulator")

DailySchedule = data_structures.DailySchedule
ParkingEvent = data_structures.ParkingEvent
Trip = data_structures.Trip
simulate_single_ev = simulator.simulate_single_ev


LEGACY_STEPS_PER_DAY = 96
LEGACY_STEP_HOURS = 24.0 / LEGACY_STEPS_PER_DAY
LEGACY_CV_THRESHOLD = 0.8
LEGACY_STEP_STARTS = np.arange(LEGACY_STEPS_PER_DAY, dtype=float) * LEGACY_STEP_HOURS
LEGACY_STEP_ENDS = LEGACY_STEP_STARTS + LEGACY_STEP_HOURS


def build_minimal_fleet(seed: int = 20260421) -> Dict[str, dict]:
    """Build a deterministic 3-EV, 7-day fleet fixture."""
    rng = np.random.default_rng(seed)
    capacities_kwh = [48.0, 64.0, 82.0]
    consumptions_kwh_per_km = [0.17, 0.19, 0.21]
    home_charge_powers_kw = [7.0, 7.0, 11.0]

    fleet: Dict[str, dict] = {}
    for ev_index, (capacity_kwh, consumption_kwh_per_km, home_charge_power_kw) in enumerate(
        zip(capacities_kwh, consumptions_kwh_per_km, home_charge_powers_kw),
        start=1,
    ):
        ev_id = f"ev_{ev_index}"
        schedules: List[DailySchedule] = []
        for day in range(7):
            dep_1 = 7.0 + (0.25 * ((ev_index + day) % 3))
            arr_1 = dep_1 + 1.0 + (0.25 * int(rng.integers(0, 2)))
            dep_2 = arr_1 + 3.0 + (0.25 * int(rng.integers(0, 2)))
            arr_2 = dep_2 + 0.5 + (0.25 * int(rng.integers(0, 2)))
            dep_3 = arr_2 + 3.5 + (0.25 * int(rng.integers(0, 2)))
            arr_3 = dep_3 + 1.0 + (0.25 * int(rng.integers(0, 2)))

            distances_km = [
                12.0 + day + (0.5 * ev_index),
                8.0 + (0.75 * day) + (0.25 * ev_index),
                18.0 + (0.5 * day) + ev_index,
            ]

            trips = [
                Trip(
                    trip_id=f"{ev_id}_d{day}_t0",
                    departure_time=dep_1,
                    arrival_time=arr_1,
                    distance_km=distances_km[0],
                    origin_purpose="home",
                    destination_purpose="work",
                    energy_consumed_kwh=distances_km[0] * consumption_kwh_per_km,
                ),
                Trip(
                    trip_id=f"{ev_id}_d{day}_t1",
                    departure_time=dep_2,
                    arrival_time=arr_2,
                    distance_km=distances_km[1],
                    origin_purpose="work",
                    destination_purpose="shopping",
                    energy_consumed_kwh=distances_km[1] * consumption_kwh_per_km,
                ),
                Trip(
                    trip_id=f"{ev_id}_d{day}_t2",
                    departure_time=dep_3,
                    arrival_time=arr_3,
                    distance_km=distances_km[2],
                    origin_purpose="shopping",
                    destination_purpose="home",
                    energy_consumed_kwh=distances_km[2] * consumption_kwh_per_km,
                ),
            ]

            work_can_charge = ((ev_index + day) % 2) == 0
            shop_can_charge = ((ev_index + day) % 3) == 0
            parking_events = [
                ParkingEvent(
                    start_time=0.0,
                    end_time=dep_1,
                    duration_hours=dep_1,
                    location_purpose="home",
                    can_charge=True,
                    charge_power_kw=home_charge_power_kw,
                ),
                ParkingEvent(
                    start_time=arr_1,
                    end_time=dep_2,
                    duration_hours=dep_2 - arr_1,
                    location_purpose="work",
                    can_charge=work_can_charge,
                    charge_power_kw=7.4 if work_can_charge else 0.0,
                ),
                ParkingEvent(
                    start_time=arr_2,
                    end_time=dep_3,
                    duration_hours=dep_3 - arr_2,
                    location_purpose="shopping",
                    can_charge=shop_can_charge,
                    charge_power_kw=3.6 if shop_can_charge else 0.0,
                ),
                ParkingEvent(
                    start_time=arr_3,
                    end_time=24.0,
                    duration_hours=24.0 - arr_3,
                    location_purpose="home",
                    can_charge=True,
                    charge_power_kw=home_charge_power_kw,
                ),
            ]

            schedules.append(
                DailySchedule(
                    ev_id=ev_id,
                    day=day,
                    day_type="weekend" if day >= 5 else "weekday",
                    trips=trips,
                    parking_events=parking_events,
                )
            )

        fleet[ev_id] = {
            "battery_capacity_kwh": capacity_kwh,
            "schedules": schedules,
        }

    return fleet


def legacy_simulate_single_day(
    schedule: DailySchedule,
    battery_capacity_kwh: float,
    soc_start: float = 1.0,
) -> tuple[np.ndarray, np.ndarray, float]:
    """Reference implementation copied from the pre-Stage-0 simulator."""
    soc_profile = np.zeros(LEGACY_STEPS_PER_DAY)
    load_profile = np.zeros(LEGACY_STEPS_PER_DAY)

    trip_energy_per_step = np.zeros(LEGACY_STEPS_PER_DAY)
    for trip in schedule.trips:
        dep = trip.departure_time
        arr = trip.arrival_time
        duration_hours = max(arr - dep, 0.01)
        rate_per_hour = trip.energy_consumed_kwh / duration_hours
        overlap = np.maximum(
            0.0,
            np.minimum(LEGACY_STEP_ENDS, arr) - np.maximum(LEGACY_STEP_STARTS, dep),
        )
        trip_energy_per_step += overlap * rate_per_hour

    park_power_per_step = np.zeros(LEGACY_STEPS_PER_DAY)
    for parking_event in schedule.parking_events:
        if not parking_event.can_charge or parking_event.charge_power_kw <= 0.0:
            continue
        overlap = np.maximum(
            0.0,
            np.minimum(LEGACY_STEP_ENDS, parking_event.end_time)
            - np.maximum(LEGACY_STEP_STARTS, parking_event.start_time),
        )
        fraction = overlap / LEGACY_STEP_HOURS
        park_power_per_step += parking_event.charge_power_kw * fraction

    inv_cap = 1.0 / battery_capacity_kwh if battery_capacity_kwh > 0.0 else 0.0
    soc = soc_start
    for step in range(LEGACY_STEPS_PER_DAY):
        soc -= trip_energy_per_step[step] * inv_cap
        if soc < 0.0:
            soc = 0.0

        park_power = park_power_per_step[step]
        if park_power > 0.0:
            if soc < LEGACY_CV_THRESHOLD:
                eff_power = park_power
            else:
                factor = (1.0 - soc) / (1.0 - LEGACY_CV_THRESHOLD)
                if factor < 0.0:
                    factor = 0.0
                eff_power = park_power * factor

            if eff_power > 0.0:
                headroom_kwh = (1.0 - soc) * battery_capacity_kwh
                max_charge_kwh = eff_power * LEGACY_STEP_HOURS
                actual_charge_kwh = (
                    max_charge_kwh if max_charge_kwh <= headroom_kwh else headroom_kwh
                )
                soc += actual_charge_kwh * inv_cap
                load_profile[step] = eff_power * (actual_charge_kwh / max_charge_kwh)

        soc_profile[step] = soc

    return soc_profile, load_profile, float(soc_profile[-1])


def legacy_simulate_single_ev(
    daily_schedules: List[DailySchedule],
    battery_capacity_kwh: float,
    soc_init: float = 1.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Reference multi-day wrapper copied from the pre-Stage-0 simulator."""
    num_days = len(daily_schedules)
    soc_all = np.empty(num_days * LEGACY_STEPS_PER_DAY)
    load_all = np.empty(num_days * LEGACY_STEPS_PER_DAY)
    soc = soc_init

    for day_index, schedule in enumerate(daily_schedules):
        start = day_index * LEGACY_STEPS_PER_DAY
        end = start + LEGACY_STEPS_PER_DAY
        soc_day, load_day, soc = legacy_simulate_single_day(
            schedule,
            battery_capacity_kwh,
            soc_start=soc,
        )
        soc_all[start:end] = soc_day
        load_all[start:end] = load_day

    return soc_all, load_all


def test_simulate_single_ev_is_bit_identical_to_legacy_baseline() -> None:
    """Stage 0 must not change the simulated SOC or load arrays."""
    fleet = build_minimal_fleet()

    for ev_id, payload in fleet.items():
        battery_capacity_kwh = payload["battery_capacity_kwh"]
        soc_ref, load_ref = legacy_simulate_single_ev(
            deepcopy(payload["schedules"]),
            battery_capacity_kwh,
        )
        soc_new, load_new, _soc_after_warmup = simulate_single_ev(
            deepcopy(payload["schedules"]),
            battery_capacity_kwh,
            warm_up_days=0,
        )

        assert np.array_equal(soc_ref, soc_new), ev_id
        assert np.array_equal(load_ref, load_new), ev_id

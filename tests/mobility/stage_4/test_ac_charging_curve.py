"""Stage 4 coverage for chemistry-specific AC charging taper thresholds."""

from __future__ import annotations

from copy import deepcopy
import importlib

import numpy as np
import pandas as pd

constants = importlib.import_module("mobility.core.constants")
data_structures = importlib.import_module("mobility.core.data_structures")
simulator = importlib.import_module("mobility.core.simulator")

DailySchedule = data_structures.DailySchedule
ParkingEvent = data_structures.ParkingEvent
Trip = data_structures.Trip
STEP_HOURS = simulator.STEP_HOURS
STEPS_PER_DAY = constants.STEPS_PER_DAY_DECISION
simulate_fleet = simulator.simulate_fleet
simulate_single_ev = simulator.simulate_single_ev


def _build_charge_window_day(
    *,
    ev_id: str = "ev_1",
    day: int = 0,
    soc_trip_kwh: float = 0.0,
    charge_start_h: float = 18.0,
    charge_end_h: float = 24.0,
) -> DailySchedule:
    trips = []
    parking_events = []

    if soc_trip_kwh > 0.0:
        trips.append(
            Trip(
                trip_id=f"{ev_id}_d{day}_t0",
                departure_time=8.0,
                arrival_time=8.5,
                distance_km=0.0,
                origin_purpose="home",
                destination_purpose="home",
                energy_consumed_kwh=soc_trip_kwh,
            )
        )
        parking_events.append(
            ParkingEvent(
                start_time=0.0,
                end_time=8.0,
                duration_hours=8.0,
                location_purpose="home",
                can_charge=False,
                charge_power_kw=0.0,
            )
        )
        parking_events.append(
            ParkingEvent(
                start_time=8.5,
                end_time=charge_start_h,
                duration_hours=charge_start_h - 8.5,
                location_purpose="home",
                can_charge=False,
                charge_power_kw=0.0,
            )
        )
    else:
        parking_events.append(
            ParkingEvent(
                start_time=0.0,
                end_time=charge_start_h,
                duration_hours=charge_start_h,
                location_purpose="home",
                can_charge=False,
                charge_power_kw=0.0,
            )
        )

    parking_events.append(
        ParkingEvent(
            start_time=charge_start_h,
            end_time=charge_end_h,
            duration_hours=charge_end_h - charge_start_h,
            location_purpose="home",
            can_charge=True,
            charge_power_kw=7.0,
        )
    )

    if charge_end_h < 24.0:
        parking_events.append(
            ParkingEvent(
                start_time=charge_end_h,
                end_time=24.0,
                duration_hours=24.0 - charge_end_h,
                location_purpose="home",
                can_charge=False,
                charge_power_kw=0.0,
            )
        )

    return DailySchedule(
        ev_id=ev_id,
        day=day,
        day_type="weekend" if (day % 7) >= 5 else "weekday",
        trips=trips,
        parking_events=parking_events,
    )


def _build_repeated_schedules(
    num_days: int,
    *,
    ev_id: str,
    soc_trip_kwh: float,
    charge_start_h: float,
    charge_end_h: float,
) -> list[DailySchedule]:
    schedules = []
    for day in range(num_days):
        schedules.append(
            _build_charge_window_day(
                ev_id=ev_id,
                day=day,
                soc_trip_kwh=soc_trip_kwh,
                charge_start_h=charge_start_h,
                charge_end_h=charge_end_h,
            )
        )
    return schedules


def _assert_result_payload_equal(left: dict, right: dict) -> None:
    assert set(left) == set(right)
    for ev_id in left:
        assert np.array_equal(left[ev_id]["soc"], right[ev_id]["soc"])
        assert np.array_equal(left[ev_id]["load"], right[ev_id]["load"])
        assert left[ev_id]["soc_after_warmup"] == right[ev_id]["soc_after_warmup"]


def test_constants_values() -> None:
    assert constants.CV_THRESHOLD == {"NMC": 0.80, "LFP": 0.88}
    assert constants.DEFAULT_CHEMISTRY == "NMC"


def test_default_matches_explicit_nmc() -> None:
    schedules = [_build_charge_window_day()]

    observed_default = simulate_single_ev(
        deepcopy(schedules),
        60.0,
        soc_init=0.82,
        warm_up_days=0,
    )
    observed_nmc = simulate_single_ev(
        deepcopy(schedules),
        60.0,
        soc_init=0.82,
        warm_up_days=0,
        chemistry="NMC",
    )

    assert np.array_equal(observed_default[0], observed_nmc[0])
    assert np.array_equal(observed_default[1], observed_nmc[1])
    assert observed_default[2] == observed_nmc[2]


def test_lfp_later_taper() -> None:
    schedules = [
        _build_charge_window_day(
            charge_start_h=18.0,
            charge_end_h=24.0,
        )
    ]
    soc_nmc, load_nmc, _soc_after_nmc = simulate_single_ev(
        deepcopy(schedules),
        60.0,
        soc_init=0.82,
        warm_up_days=0,
        chemistry="NMC",
    )
    soc_lfp, load_lfp, _soc_after_lfp = simulate_single_ev(
        deepcopy(schedules),
        60.0,
        soc_init=0.82,
        warm_up_days=0,
        chemistry="LFP",
    )

    charge_end_step = int(24.0 / STEP_HOURS) - 1
    assert soc_lfp[charge_end_step] > soc_nmc[charge_end_step]

    charge_start_step = int(18.0 / STEP_HOURS)
    energy_nmc_30min_kwh = float(np.sum(load_nmc[charge_start_step : charge_start_step + 2]) * STEP_HOURS)
    energy_lfp_30min_kwh = float(np.sum(load_lfp[charge_start_step : charge_start_step + 2]) * STEP_HOURS)
    assert energy_lfp_30min_kwh - energy_nmc_30min_kwh >= 0.5


def test_unknown_chemistry_raises_valueerror() -> None:
    schedules = [_build_charge_window_day()]

    try:
        simulate_single_ev(
            schedules,
            60.0,
            warm_up_days=0,
            chemistry="unobtainium",
        )
    except ValueError as exc:
        assert "Unknown chemistry" in str(exc)
    else:
        raise AssertionError("Expected ValueError for unknown chemistry")


def test_fleet_chemistry_column_drives_per_ev_threshold() -> None:
    fleet_schedules = {
        "ev_1": _build_repeated_schedules(
            20,
            ev_id="ev_1",
            soc_trip_kwh=3.0,
            charge_start_h=18.5,
            charge_end_h=19.0,
        ),
        "ev_2": _build_repeated_schedules(
            20,
            ev_id="ev_2",
            soc_trip_kwh=3.0,
            charge_start_h=18.5,
            charge_end_h=19.0,
        ),
    }
    ev_fleet = pd.DataFrame(
        {
            "EV_ID": ["ev_1", "ev_2"],
            "battery_capacity_kwh": [60.0, 60.0],
            "chemistry": ["NMC", "LFP"],
        }
    )

    results = simulate_fleet(
        fleet_schedules,
        ev_fleet,
        soc_init=0.82,
    )

    assert not np.array_equal(results["ev_1"]["soc"], results["ev_2"]["soc"])
    assert results["ev_2"]["soc_after_warmup"] >= results["ev_1"]["soc_after_warmup"]


def test_fleet_missing_chemistry_column_defaults_to_nmc() -> None:
    fleet_schedules = {
        "ev_1": _build_repeated_schedules(
            20,
            ev_id="ev_1",
            soc_trip_kwh=3.0,
            charge_start_h=18.5,
            charge_end_h=19.0,
        )
    }
    ev_fleet_missing = pd.DataFrame(
        {
            "EV_ID": ["ev_1"],
            "battery_capacity_kwh": [60.0],
        }
    )
    ev_fleet_explicit = pd.DataFrame(
        {
            "EV_ID": ["ev_1"],
            "battery_capacity_kwh": [60.0],
            "chemistry": ["NMC"],
        }
    )

    observed_missing = simulate_fleet(
        deepcopy(fleet_schedules),
        ev_fleet_missing,
        soc_init=0.82,
    )
    observed_explicit = simulate_fleet(
        deepcopy(fleet_schedules),
        ev_fleet_explicit,
        soc_init=0.82,
    )

    _assert_result_payload_equal(observed_missing, observed_explicit)


def test_fleet_chemistry_nan_falls_back_to_default() -> None:
    fleet_schedules = {
        "ev_1": _build_repeated_schedules(
            20,
            ev_id="ev_1",
            soc_trip_kwh=3.0,
            charge_start_h=18.5,
            charge_end_h=19.0,
        )
    }
    ev_fleet_nan = pd.DataFrame(
        {
            "EV_ID": ["ev_1"],
            "battery_capacity_kwh": [60.0],
            "chemistry": [pd.NA],
        }
    )
    ev_fleet_nmc = pd.DataFrame(
        {
            "EV_ID": ["ev_1"],
            "battery_capacity_kwh": [60.0],
            "chemistry": ["NMC"],
        }
    )

    observed_nan = simulate_fleet(
        deepcopy(fleet_schedules),
        ev_fleet_nan,
        soc_init=0.82,
    )
    observed_nmc = simulate_fleet(
        deepcopy(fleet_schedules),
        ev_fleet_nmc,
        soc_init=0.82,
    )

    _assert_result_payload_equal(observed_nan, observed_nmc)

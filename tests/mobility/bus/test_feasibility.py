from __future__ import annotations

import numpy as np

from mobility.bus.feasibility import block_preflight, scan_block_infeasibility
from mobility.bus.sim_adapter import simulate_block
from mobility.core.data_structures import DailySchedule, ParkingEvent, Trip
from mobility.core.simulator import simulate_single_ev


def _block(rows: list[tuple[str, float, float, float]]) -> "pd.DataFrame":
    import pandas as pd

    return pd.DataFrame(
        [
            (trip_id, "OP", "R1", "S1", 0, "B1", "native", start_h, end_h, distance_km, "A", "A", 51.0, -0.1, 51.0, -0.1, "shape")
            for trip_id, start_h, end_h, distance_km in rows
        ],
        columns=[
            "trip_id",
            "agency_id",
            "route_id",
            "service_id",
            "direction_id",
            "block_id",
            "block_source",
            "start_h",
            "end_h",
            "distance_km",
            "start_stop",
            "end_stop",
            "start_lat",
            "start_lon",
            "end_lat",
            "end_lon",
            "shape_id",
        ],
    )


def _schedule(trips: list[Trip], parking: list[ParkingEvent]) -> list[DailySchedule]:
    return [DailySchedule(ev_id="bus_B1", day=0, day_type="representative_service_day", trips=trips, parking_events=parking)]


def _trip(trip_id: str, dep: float, arr: float, energy_kwh: float) -> Trip:
    return Trip(
        trip_id=trip_id,
        departure_time=dep,
        arrival_time=arr,
        distance_km=energy_kwh,
        origin_purpose="bus_stop",
        destination_purpose="bus_stop",
        energy_consumed_kwh=energy_kwh,
    )


def test_single_trip_exceeds_battery_reason() -> None:
    result = simulate_block(
        _block([("t0", 8.0, 10.0, 200.0)]),
        battery_kwh=50.0,
        consumption_kwh_per_km=1.2,
        depot_charge_kw=50.0,
    )

    assert result["infeasible"] is True
    assert result["infeasibility_reason"] == "single_trip_exceeds_battery"
    assert result["shortfall_kwh"] > 0.0


def test_depot_only_insufficient_reason() -> None:
    schedules = _schedule(
        [_trip(f"t{i}", 4.0 + i, 4.5 + i, 30.0) for i in range(10)],
        [ParkingEvent(0.0, 4.0, 4.0, "depot_terminus", can_charge=True, charge_power_kw=50.0)],
    )

    result = scan_block_infeasibility(
        np.zeros(96),
        schedules,
        50.0,
        soc_init=1.0,
        depot_charge_kw=50.0,
        layover_charge_kw=0.0,
        allow_layover_charging=False,
    )

    assert result["infeasibility_reason"] == "depot_only_insufficient"


def test_midday_depletion_reason_when_energy_total_is_physically_recoverable() -> None:
    schedules = _schedule(
        [_trip("t0", 8.0, 9.0, 40.0), _trip("t1", 9.5, 10.5, 40.0)],
        [ParkingEvent(12.0, 14.0, 2.0, "depot_terminus", can_charge=True, charge_power_kw=100.0)],
    )
    soc, _, _ = simulate_single_ev(schedules, 100.0, soc_init=0.5, warm_up_days=0)

    result = scan_block_infeasibility(
        soc,
        schedules,
        100.0,
        soc_init=0.5,
        depot_charge_kw=100.0,
        layover_charge_kw=0.0,
        allow_layover_charging=False,
    )

    assert result["infeasibility_reason"] == "midday_depletion"
    assert result["first_floor_trip_id"] == "t1"


def test_starts_below_min_required_reason() -> None:
    result = simulate_block(
        _block([("t0", 0.0, 1.0, 50.0)]),
        battery_kwh=100.0,
        consumption_kwh_per_km=1.0,
        depot_charge_kw=0.0,
        soc_init=0.05,
    )

    assert result["infeasibility_reason"] == "starts_below_min_required"


def test_starts_below_min_required_includes_pre_first_trip_charge() -> None:
    schedules = _schedule(
        [_trip("t0", 2.0, 3.0, 60.0)],
        [ParkingEvent(0.0, 2.0, 2.0, "depot_terminus", can_charge=True, charge_power_kw=50.0)],
    )

    result = block_preflight(
        schedules,
        battery_kwh=300.0,
        consumption_kwh_per_km=1.0,
        depot_charge_kw=50.0,
        layover_charge_kw=0.0,
        allow_layover_charging=False,
        soc_init=0.05,
    )

    assert result["infeasibility_reason"] is None


def test_starts_below_min_required_when_pre_first_trip_charge_insufficient() -> None:
    schedules = _schedule(
        [_trip("t0", 2.0, 3.0, 60.0)],
        [ParkingEvent(0.0, 2.0, 2.0, "depot_terminus", can_charge=True, charge_power_kw=10.0)],
    )

    result = block_preflight(
        schedules,
        battery_kwh=300.0,
        consumption_kwh_per_km=1.0,
        depot_charge_kw=10.0,
        layover_charge_kw=0.0,
        allow_layover_charging=False,
        soc_init=0.05,
    )

    assert result["infeasibility_reason"] == "starts_below_min_required"


def test_feasible_case_has_no_infeasibility_reason() -> None:
    result = simulate_block(
        _block([("t0", 8.0, 9.0, 20.0), ("t1", 10.0, 11.0, 20.0)]),
        battery_kwh=345.0,
        consumption_kwh_per_km=1.0,
        depot_charge_kw=100.0,
        soc_init=1.0,
    )

    assert result["infeasible"] is False
    assert result["infeasibility_reason"] is None
    assert result["shortfall_kwh"] == 0.0

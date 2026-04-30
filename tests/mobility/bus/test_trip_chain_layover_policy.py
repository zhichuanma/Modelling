from __future__ import annotations

import pandas as pd

from mobility.bus.trip_chain_bus import block_to_daily_schedules


def _layover_block() -> pd.DataFrame:
    rows = [
        ("t0", 8.0, 9.0, "A", "B"),
        ("t1", 9.3, 10.0, "B", "C"),
        ("t2", 11.0, 12.0, "C", "D"),
    ]
    return pd.DataFrame(
        [
            (tid, "OP", "R1", "S1", 0, "B1", "native", start, end, 10.0, a, b, 51.0, -1.0, 51.1, -1.1, "shape")
            for tid, start, end, a, b in rows
        ],
        columns=[
            "trip_id", "agency_id", "route_id", "service_id", "direction_id", "block_id",
            "block_source", "start_h", "end_h", "distance_km", "start_stop", "end_stop",
            "start_lat", "start_lon", "end_lat", "end_lon", "shape_id",
        ],
    )


def test_layover_charging_respects_min_duration() -> None:
    schedule = block_to_daily_schedules(
        _layover_block(),
        "bus_B1",
        consumption_kwh_per_km=1.0,
        depot_charge_kw=100.0,
        allow_layover_charging=True,
        layover_charge_kw=50.0,
        min_layover_for_charging_h=0.5,
    )[0]
    layovers = [event for event in schedule.parking_events if event.location_purpose == "layover"]

    assert [event.can_charge for event in layovers] == [False, True]
    assert [event.charge_power_kw for event in layovers] == [0.0, 50.0]


def test_disabling_layover_charging_overrides_threshold() -> None:
    schedule = block_to_daily_schedules(
        _layover_block(),
        "bus_B1",
        consumption_kwh_per_km=1.0,
        depot_charge_kw=100.0,
        allow_layover_charging=False,
        layover_charge_kw=50.0,
        min_layover_for_charging_h=0.0,
    )[0]
    layovers = [event for event in schedule.parking_events if event.location_purpose == "layover"]

    assert all(not event.can_charge for event in layovers)
    assert all(event.charge_power_kw == 0.0 for event in layovers)

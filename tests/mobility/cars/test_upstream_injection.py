from __future__ import annotations

import importlib

import numpy as np

trip_chain = importlib.import_module("mobility.cars.trip_chain")

chain_to_daily_schedule = trip_chain.chain_to_daily_schedule


class FakeSampler:
    def __init__(
        self,
        destinations: dict[tuple[str, str], str],
        distances: dict[tuple[str, str], float],
    ):
        self.destinations = destinations
        self.distances = distances
        self.calls: list[tuple[str, str, str]] = []

    def sample_destination_lsoa(
        self,
        origin_lsoa: str,
        purpose: str,
        rng: np.random.Generator,
        home_lsoa: str,
    ) -> str:
        _ = rng
        self.calls.append((origin_lsoa, purpose, home_lsoa))
        return self.destinations[(origin_lsoa, purpose)]

    def distance_km(self, a: str, b: str) -> float:
        return self.distances[(a, b)]


def test_chain_with_non_home_start_uses_start_lsoa() -> None:
    sampler = FakeSampler(
        destinations={("X", "work"): "Y"},
        distances={("X", "Y"): 12.0},
    )
    schedule = chain_to_daily_schedule(
        [(8.0, 9.0, 10.0, "holiday", "work")],
        "ev_1",
        0,
        "weekday",
        consumption_kwh_per_km=0.2,
        jitter_minutes=0.0,
        home_lsoa="HOME",
        start_lsoa="X",
        sampler=sampler,
        rng=np.random.default_rng(0),
    )

    assert schedule.trips[0].origin_lsoa == "X"
    assert sampler.calls == [("X", "work", "HOME")]
    assert schedule.parking_events[0].location_lsoa == "X"


def test_chain_with_home_start_ignores_start_lsoa() -> None:
    sampler = FakeSampler(
        destinations={("HOME", "work"): "Y"},
        distances={("HOME", "Y"): 15.0},
    )
    schedule = chain_to_daily_schedule(
        [(8.0, 9.0, 10.0, "home", "work")],
        "ev_1",
        0,
        "weekday",
        consumption_kwh_per_km=0.2,
        jitter_minutes=0.0,
        home_lsoa="HOME",
        start_lsoa="X",
        sampler=sampler,
        rng=np.random.default_rng(0),
    )

    assert schedule.trips[0].origin_lsoa == "HOME"
    assert schedule.parking_events[0].location_lsoa == "HOME"


def test_chain_with_empty_start_lsoa_falls_back_to_home() -> None:
    sampler = FakeSampler(
        destinations={("HOME", "work"): "Y"},
        distances={("HOME", "Y"): 15.0},
    )
    schedule = chain_to_daily_schedule(
        [(8.0, 9.0, 10.0, "holiday", "work")],
        "ev_1",
        0,
        "weekday",
        consumption_kwh_per_km=0.2,
        jitter_minutes=0.0,
        home_lsoa="HOME",
        start_lsoa="",
        sampler=sampler,
        rng=np.random.default_rng(0),
    )

    assert schedule.trips[0].origin_lsoa == "HOME"
    assert schedule.parking_events[0].location_lsoa == "HOME"


def test_chain_to_daily_schedule_signature_backward_compatible() -> None:
    schedule_without_start = chain_to_daily_schedule(
        [(8.0, 9.0, 10.0, "holiday", "work")],
        "ev_1",
        0,
        "weekday",
        consumption_kwh_per_km=0.2,
        jitter_minutes=0.0,
        home_lsoa="HOME",
        sampler=FakeSampler(
            destinations={("HOME", "work"): "Y"},
            distances={("HOME", "Y"): 15.0},
        ),
        rng=np.random.default_rng(0),
    )
    schedule_with_empty_start = chain_to_daily_schedule(
        [(8.0, 9.0, 10.0, "holiday", "work")],
        "ev_1",
        0,
        "weekday",
        consumption_kwh_per_km=0.2,
        jitter_minutes=0.0,
        home_lsoa="HOME",
        start_lsoa="",
        sampler=FakeSampler(
            destinations={("HOME", "work"): "Y"},
            distances={("HOME", "Y"): 15.0},
        ),
        rng=np.random.default_rng(0),
    )

    assert schedule_without_start == schedule_with_empty_start

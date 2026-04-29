"""Stage 8 coverage for the jitter clamp upper bound."""

from __future__ import annotations

import importlib

import numpy as np

trip_chain = importlib.import_module("mobility.cars.trip_chain")
_add_time_jitter = trip_chain._add_time_jitter


class _FixedRng:
    def __init__(self, value: float):
        self._value = value

    def uniform(self, low: float, high: float) -> float:
        return self._value


def test_add_time_jitter_stays_within_0_to_23_75() -> None:
    rng = np.random.default_rng(0)

    for _ in range(10_000):
        value = float(rng.uniform(0.0, 24.0))
        jittered = _add_time_jitter(value)
        assert 0.0 <= jittered <= 23.75


def test_add_time_jitter_clamps_large_positive_noise_to_23_75() -> None:
    fixed_rng = _FixedRng(10.0)
    assert _add_time_jitter(23.9, rng=fixed_rng) == 23.75


def test_add_time_jitter_clamps_large_negative_noise_to_zero() -> None:
    fixed_rng = _FixedRng(-10.0)
    assert _add_time_jitter(0.1, rng=fixed_rng) == 0.0

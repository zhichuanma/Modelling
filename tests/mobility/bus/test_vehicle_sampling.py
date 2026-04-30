from __future__ import annotations

import numpy as np
import pandas as pd

from mobility.bus.vehicle_sampling import load_bus_vehicle_params, sample_bus_vehicle_specs


def _params() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "make": ["A", "B"],
            "gen_model": ["Alpha", "Beta"],
            "stock_2025_q2": [10.0, 90.0],
            "battery_kwh": [200.0, 500.0],
            "consumption_kwh_per_km": [0.7, 1.1],
            "depot_charge_kw": [80.0, 150.0],
        }
    )


def test_sample_bus_vehicle_specs_is_seeded_and_weighted() -> None:
    rng_a = np.random.default_rng(20260430)
    rng_b = np.random.default_rng(20260430)

    sample_a = sample_bus_vehicle_specs(_params(), rng_a, n=8)
    sample_b = sample_bus_vehicle_specs(_params(), rng_b, n=8)

    pd.testing.assert_frame_equal(sample_a, sample_b)
    assert set(sample_a["battery_kwh"]).issubset({200.0, 500.0})
    assert sample_a["gen_model"].nunique() > 1


def test_load_bus_vehicle_params_exposes_real_table_coverage() -> None:
    params = load_bus_vehicle_params()

    assert {"make", "gen_model", "battery_kwh", "consumption_kwh_per_km", "depot_charge_kw"}.issubset(params.columns)
    assert params["battery_kwh"].gt(0).all()
    assert params["consumption_kwh_per_km"].gt(0).all()
    assert params.attrs["stock_coverage_pct"] > 90.0

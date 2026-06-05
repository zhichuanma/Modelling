from __future__ import annotations

import pandas as pd
import pytest

from mobility.bus.depot_only_assignment import ASSIGNMENT_METHOD, build_simulation_cases


def _vehicles(n: int = 3) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "vehicle_id": [f"bus_{i}" for i in range(n)],
            "source_row_id": list(range(n)),
            "vehicle_model": ["A"] * n,
            "vehicle_subtype": ["bus"] * n,
            "source_lsoa": [f"SRC{i}" for i in range(n)],
            "battery_kwh": [300.0] * n,
            "consumption_kwh_per_km": [1.0] * n,
            "ac_charge_kw_max": [80.0] * n,
            "dc_charge_kw_max": [150.0] * n,
            "usable_soc_min": [0.1] * n,
            "usable_soc_max": [0.95] * n,
            "vehicle_instance_weight": [1.0] * n,
        }
    )


def _blocks(n: int = 3) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "block_template_id": [f"bt{i}" for i in range(n)],
            "block_id": [f"B{i}" for i in range(n)],
            "agency_id": ["OP"] * n,
            "service_id": ["S"] * n,
            "block_source": ["native"] * n,
            "start_h": [8.0] * n,
            "end_h": [10.0] * n,
            "duration_h": [2.0] * n,
            "passenger_distance_km": [50.0] * n,
            "start_lat": [51.0] * n,
            "start_lon": [-1.0] * n,
            "end_lat": [51.0] * n,
            "end_lon": [-1.0] * n,
            "start_lsoa": ["E1"] * n,
            "end_lsoa": ["E1"] * n,
            "region_key": ["London"] * n,
            "depot_id": ["opdepot_OP_E1"] * n,
            "operational_depot_lsoa": ["E1"] * n,
            "depot_confidence": ["high"] * n,
            "depot_lat": [51.0] * n,
            "depot_lon": [-1.0] * n,
        }
    )


def test_full_mode_one_to_one_counts_and_uniqueness() -> None:
    cases, diag = build_simulation_cases(_vehicles(4), _blocks(4), seed=1)
    assert len(cases) == 4
    assert cases["vehicle_id"].is_unique
    assert cases["block_template_id"].is_unique
    assert diag.loc[0, "n_simulation_cases_created"] == 4


def test_vehicle_source_lsoa_not_used_for_matching() -> None:
    cases, diag = build_simulation_cases(_vehicles(3), _blocks(3), seed=2)
    assert not bool(cases["vehicle_source_lsoa_used_for_matching"].any())
    assert bool(diag.loc[0, "vehicle_source_lsoa_used_for_matching"]) is False
    assert set(cases["source_lsoa"]) == {"SRC0", "SRC1", "SRC2"}


def test_assignment_method_label() -> None:
    cases, _ = build_simulation_cases(_vehicles(2), _blocks(2), seed=3)
    assert set(cases["assignment_method"]) == {ASSIGNMENT_METHOD}


def test_missing_depot_block_shortage_requires_topup_in_full_mode() -> None:
    blocks = _blocks(3)
    blocks.loc[0, "operational_depot_lsoa"] = ""
    with pytest.raises(ValueError, match="one non-missing-depot sampled block"):
        build_simulation_cases(_vehicles(3), blocks)


def test_pilot_mode_can_create_debug_subset() -> None:
    cases, _ = build_simulation_cases(_vehicles(5), _blocks(2), sample_mode="pilot", seed=4)
    assert len(cases) == 2
    assert cases["assignment_method"].str.startswith("pilot_debug_").all()

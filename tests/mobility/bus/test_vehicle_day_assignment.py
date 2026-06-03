from __future__ import annotations

import pandas as pd

from mobility.bus.annual_vehicle_day_assignment import build_vehicle_day_assignments


def _instances(n: int = 3) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "service_date": ["2026-04-17"] * n,
            "block_instance_id": [f"bi{i}" for i in range(n)],
            "block_template_id": [f"bt{i}" for i in range(n)],
            "agency_id": ["OP"] * n,
            "service_id": ["S1"] * n,
            "block_id": [f"B{i}" for i in range(n)],
            "depot_id": ["opdepot_OP_E1"] * n,
            "region_key": ["London"] * n,
            "passenger_distance_km": [10.0, 50.0, 100.0][:n],
            "duration_h": [1.0, 5.0, 10.0][:n],
        }
    )


def _specs(n: int = 2) -> pd.DataFrame:
    return pd.DataFrame({"vehicle_spec_id": [f"ev{i}" for i in range(n)], "source_lsoa": [f"X{i}" for i in range(n)]})


def test_daily_assignment_count_matches_ev_specs_when_enough_blocks() -> None:
    out = build_vehicle_day_assignments(_instances(3), _specs(2), seed=1)
    assert len(out) == 2


def test_each_vehicle_spec_used_once_per_day() -> None:
    out = build_vehicle_day_assignments(_instances(3), _specs(2), seed=1)
    assert out["vehicle_spec_id"].is_unique


def test_each_block_instance_used_once_in_default_mode() -> None:
    out = build_vehicle_day_assignments(_instances(3), _specs(2), seed=1)
    assert out["block_instance_id"].is_unique


def test_assignment_does_not_use_vehicle_source_lsoa_by_default() -> None:
    out = build_vehicle_day_assignments(_instances(3), _specs(2), seed=1)
    assert "source_lsoa" not in out.columns
    assert out["assignment_method"].str.contains("representative_duty").all()


def test_assignment_is_deterministic_by_date() -> None:
    a = build_vehicle_day_assignments(_instances(3), _specs(2), seed=99)
    b = build_vehicle_day_assignments(_instances(3), _specs(2), seed=99)
    pd.testing.assert_frame_equal(a, b)


def test_assignment_reports_daily_unassigned_blocks() -> None:
    out = build_vehicle_day_assignments(_instances(3), _specs(2), seed=1)
    assert out["n_available_block_instances_for_service_date"].eq(3).all()
    assert out["n_assigned_block_instances_for_service_date"].eq(2).all()
    assert out["n_unassigned_block_instances_for_service_date"].eq(1).all()
    assert out["daily_assignment_coverage_share"].eq(2 / 3).all()

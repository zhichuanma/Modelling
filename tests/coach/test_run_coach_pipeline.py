"""Smoke test for ``scripts/run_coach_pipeline.py`` with ``--limit 3``."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import run_coach_pipeline  # noqa: E402


def _build_synthetic_journeys() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "journey_id": [f"smoke::VJ{i}" for i in range(5)],
            "vehicle_journey_code": [f"VJ{i}" for i in range(5)],
            "operator_code": ["BHAT"] * 5,
            "operator_name": ["BHAT"] * 5,
            "line_name": [str(i) for i in range(5)],
            "departure_time": ["08:00:00"] * 5,
            "arrival_time": ["10:00:00"] * 5,
            "start_h": [8.0] * 5,
            "end_h": [10.0] * 5,
            "duration_h": [2.0] * 5,
            "distance_km": [60.0, 80.0, 120.0, 150.0, 200.0],
            "distance_source": ["haversine_x_detour"] * 5,
            "road_detour_factor": [1.3] * 5,
            "has_cross_midnight": [False] * 5,
        }
    )


def test_run_coach_pipeline_smoke(tmp_path: Path) -> None:
    journeys_path = tmp_path / "journeys.parquet"
    output_path = tmp_path / "out.parquet"
    _build_synthetic_journeys().to_parquet(journeys_path, index=False)

    exit_code = run_coach_pipeline.main(
        [
            "--journeys-parquet",
            str(journeys_path),
            "--output-parquet",
            str(output_path),
            "--limit",
            "3",
            "--seed",
            "42",
        ]
    )

    assert exit_code == 0
    assert output_path.exists()

    out = pd.read_parquet(output_path)
    assert len(out) == 3
    assert list(out.columns) == [
        "journey_id",
        "ev_id",
        "feasible",
        "total_kwh",
        "soc_floor_hit_h",
        "soc_clamped_to_zero",
    ]

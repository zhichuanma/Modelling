"""Smoke test for ``scripts/run_coach_annual_pipeline.py`` with ``--limit 2``."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import run_coach_annual_pipeline  # noqa: E402


def _journeys() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "journey_id": ["J1", "J2", "J3", "J4"],
            "file_name": ["missing.xml"] * 4,
            "vehicle_journey_code": ["VJ1", "VJ2", "VJ3", "VJ4"],
            "operator_code": ["OP"] * 4,
            "operator_name": ["Operator"] * 4,
            "line_name": ["L1"] * 4,
            "departure_time": ["08:00:00", "11:00:00", "08:30:00", "14:00:00"],
            "arrival_time": ["10:00:00", "13:00:00", "09:30:00", "15:00:00"],
            "start_h": [8.0, 11.0, 8.5, 14.0],
            "end_h": [10.0, 13.0, 9.5, 15.0],
            "duration_h": [2.0, 2.0, 1.0, 1.0],
            "distance_km": [80.0, 90.0, 40.0, 30.0],
            "distance_source": ["haversine_x_detour"] * 4,
            "road_detour_factor": [1.3] * 4,
            "has_cross_midnight": [False] * 4,
            "start_lat": [51.50, 51.501, 51.51, 51.502],
            "start_lon": [-0.10, -0.101, -0.12, -0.102],
            "end_lat": [51.501, 51.502, 51.511, 51.503],
            "end_lon": [-0.101, -0.102, -0.121, -0.103],
            "start_lsoa": ["E01000001"] * 4,
            "end_lsoa": ["E01000002"] * 4,
        }
    )


def _fleet_csv(path: Path) -> None:
    pd.DataFrame(
        {
            "EV_ID": ["EV1"],
            "Model": ["Coach EV"],
            "Energy_kWh": [400.0],
            "DC_Power_kW": [150.0],
            "AC_Power_kW": [22.0],
            "efficiency_wh_per_km": [800.0],
            "LSOA_code": ["E01000001"],
            "count": [1.0],
            "vehicle_subtype": ["coach"],
        }
    ).to_csv(path, index=False)


def test_run_coach_annual_pipeline_smoke(tmp_path: Path) -> None:
    journeys_path = tmp_path / "journeys.parquet"
    stops_path = tmp_path / "stops.parquet"
    fleet_path = tmp_path / "fleet.csv"
    per_chain_out = tmp_path / "per_chain.parquet"
    load_out = tmp_path / "load.parquet"
    _journeys().to_parquet(journeys_path, index=False)
    pd.DataFrame({"journey_id": []}).to_parquet(stops_path, index=False)
    _fleet_csv(fleet_path)

    exit_code = run_coach_annual_pipeline.main(
        [
            "--journeys-parquet",
            str(journeys_path),
            "--stop-sequences-parquet",
            str(stops_path),
            "--fleet-path",
            str(fleet_path),
            "--per-chain-out",
            str(per_chain_out),
            "--load-profile-out",
            str(load_out),
            "--limit",
            "2",
            "--warm-up-days",
            "0",
            "--seed",
            "42",
        ]
    )

    assert exit_code == 0
    assert per_chain_out.exists()
    assert load_out.exists()
    per_chain = pd.read_parquet(per_chain_out)
    assert len(per_chain) == 2

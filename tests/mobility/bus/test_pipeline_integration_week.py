from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import pytest

from scripts.run_bus_pipeline import DEFAULT_BLOCKS, DEFAULT_EV_LSOA_PATH, DEFAULT_GTFS_DIR, DEFAULT_OLD_PER_BLOCK, run_pipeline


pytestmark = pytest.mark.slow


def _required_data_available() -> bool:
    return all(
        Path(path).exists()
        for path in (
            DEFAULT_BLOCKS,
            DEFAULT_GTFS_DIR / "agency.txt",
            DEFAULT_GTFS_DIR / "calendar.txt",
            DEFAULT_EV_LSOA_PATH,
        )
    )


def test_full_pipeline_on_one_week_runs_without_error(tmp_path: Path) -> None:
    if not _required_data_available():
        pytest.skip("Real bus M1 input data is not available in this checkout.")

    output_dir = tmp_path / "m1_week"
    summary = run_pipeline(
        argparse.Namespace(
            blocks=DEFAULT_BLOCKS,
            gtfs_dir=DEFAULT_GTFS_DIR,
            txc_dir=DEFAULT_GTFS_DIR.parent,
            ev_lsoa=DEFAULT_EV_LSOA_PATH,
            output_dir=output_dir,
            start_date="2026-04-13",
            end_date="2026-04-19",
            limit_blocks=200,
            skip_txc=True,
            max_chains_resolve=0,
            progress_interval=0,
            old_per_block=DEFAULT_OLD_PER_BLOCK,
        )
    )

    block_instances = pd.read_parquet(summary["block_instances"])
    assignments = pd.read_parquet(summary["vehicle_assignments"])
    events = pd.read_parquet(summary["vehicle_day_events"])
    resolution = pd.read_parquet(summary["resolution_summary"])

    assert not block_instances.empty
    assert set(assignments["assignment_status"]) == {"assigned"}
    assert "uncovered" not in set(assignments["assignment_status"].astype(str))
    assert set(block_instances["block_instance_id"]) == set(assignments["block_instance_id"])

    depot_counts = assignments.groupby("chain_id")["depot_id"].nunique()
    assert int(depot_counts.max()) == 1

    assert set(events["chain_id"].unique()) == set(resolution["chain_id"].unique())
    assert resolution["resolution_level"].between(0, 4).all()
    assert 5 not in set(resolution["resolution_level"])

    passenger_km_instances = float(block_instances["passenger_distance_km"].sum())
    passenger_km_events = float(events.loc[events["event_type"].eq("passenger_block"), "distance_km"].sum())
    assert passenger_km_events == pytest.approx(passenger_km_instances)
